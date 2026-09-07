import argparse
import ast
import importlib
import io
import json
import os
import tempfile
import urllib.error
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from llmproxy.core.command import CommandError


command = importlib.import_module("..commands.create_key", __package__)
CREDENTIALS = {"base_url": "http://litellm.test", "api_key": "dummy"}


def backend():
    return importlib.import_module("..src.key_create", __package__)


class KeyCreationSafetyTest(unittest.TestCase):
    def setUp(self):
        self.backend = backend()

    def test_create_user_transport_failure_is_concise_domain_error(self):
        with patch.object(self.backend, "request_json", side_effect=urllib.error.URLError("offline")):
            with self.assertRaisesRegex(CommandError, "Failed to create user"):
                self.backend.create_user("user@example.com", **CREDENTIALS)

    def test_create_key_transport_failure_is_concise_domain_error(self):
        with patch.object(self.backend, "request_json", side_effect=OSError("offline")):
            with self.assertRaisesRegex(CommandError, "Failed to create API key"):
                self.backend.create_api_key("user-1", "alias", **CREDENTIALS)

    def test_create_user_rejects_malformed_success_response(self):
        with patch.object(self.backend, "request_json", return_value=[]):
            with self.assertRaisesRegex(CommandError, "unexpected response"):
                self.backend.create_user("user@example.com", **CREDENTIALS)

    def test_create_key_rejects_malformed_success_response(self):
        with patch.object(self.backend, "request_json", return_value=[]):
            with self.assertRaisesRegex(CommandError, "unexpected response"):
                self.backend.create_api_key("user-1", "alias", **CREDENTIALS)

    def test_user_list_404_is_not_treated_as_definitive_absence(self):
        error = urllib.error.HTTPError("http://litellm/user/list", 404, "Not Found", Message(), None)
        with patch.object(self.backend, "request_json", side_effect=error):
            with self.assertRaisesRegex(CommandError, "Failed to check user"):
                self.backend.get_user_by_email("user@example.com", **CREDENTIALS)

    def test_duplicate_exact_users_are_rejected(self):
        with patch.object(self.backend, "request_json", return_value={"users": [
            {"user_email": "user@example.com", "user_id": "a"},
            {"user_email": "user@example.com", "user_id": "b"},
        ]}):
            with self.assertRaisesRegex(CommandError, "multiple exact users"):
                self.backend.get_user_by_email("user@example.com", **CREDENTIALS)

    def test_exact_user_without_id_is_rejected_before_key_creation(self):
        with (
            patch.object(self.backend, "get_user_by_email", return_value={"user_email": "user@example.com"}),
            patch.object(self.backend, "create_api_key") as create,
        ):
            with self.assertRaisesRegex(CommandError, "returned no user_id"):
                self.backend.create_key_for_user("user@example.com", **CREDENTIALS)
        create.assert_not_called()

    def test_lookup_network_failure_is_not_treated_as_user_not_found(self):
        with patch.object(self.backend, "request_json", side_effect=urllib.error.URLError("network unavailable")):
            with self.assertRaisesRegex(CommandError, "Failed to look up user"):
                self.backend.get_user_by_email("user@example.com", **CREDENTIALS)

    def test_lookup_http_404_is_not_treated_as_user_not_found(self):
        error = urllib.error.HTTPError("http://litellm/user/info", 404, "not found", Message(), io.BytesIO(b"{}"))
        with patch.object(self.backend, "request_json", side_effect=error):
            with self.assertRaisesRegex(CommandError, "Failed to check user"):
                self.backend.get_user_by_email("user@example.com", **CREDENTIALS)

    def test_flow_fails_closed_before_creating_user_after_lookup_error(self):
        with (
            patch.object(self.backend, "get_user_by_email", side_effect=CommandError("lookup failed")),
            patch.object(self.backend, "create_user") as create,
            self.assertRaisesRegex(CommandError, "lookup failed"),
        ):
            self.backend.create_key_for_user("user@example.com", **CREDENTIALS)
        create.assert_not_called()


class KeyCreationAdapterTest(unittest.TestCase):
    def test_run_delegates_explicit_arguments_and_presents_key(self):
        parser = argparse.ArgumentParser()
        command.configure(parser)
        args = parser.parse_args(["user@example.com", "--models", " General,Other ", "--alias", "alias", "--key", "sk-test", "--team-id", "team"])
        module = Mock()
        module.create_key_for_user.return_value = "sk-created"
        context = SimpleNamespace(root=Path("/synthetic-selected-root"))
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"LITELLM_API_KEY": "dummy", "LITELLM_BASE_URL": "http://litellm.test/"}),
            patch.object(command, "load_dotenv") as dotenv,
            patch.object(command, "load_component_module", create=True, return_value=module) as load,
            patch.object(command, "request_json", create=True, side_effect=AssertionError("adapter must not call HTTP")),
            patch("llmproxy.core.http.request_json", side_effect=AssertionError("adapter must not call HTTP")),
            redirect_stdout(stdout),
        ):
            self.assertEqual(command.run(args, context), 0)
        dotenv.assert_called_once_with(context.root / ".env")
        load.assert_called_once_with(context.root, "llmproxy/litellm", "src.key_create")
        call = module.create_key_for_user.call_args
        self.assertEqual(call.args, ("user@example.com",))
        kwargs = dict(call.kwargs)
        report = kwargs.pop("report")
        self.assertEqual(kwargs, dict(CREDENTIALS, alias="alias", key_value="sk-test", team_id="team", models=[" General", "Other "], rpm_limit=100, max_budget=700, budget_duration="7d"))
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            report("progress")
        self.assertEqual(stderr.getvalue(), "progress\n")
        self.assertEqual(stdout.getvalue(), "sk-created\n")


class KeyCreationContractTest(unittest.TestCase):
    def test_direct_key_creation_defaults_and_explicit_overrides(self):
        module = backend()
        for options, expected in (
            ({}, (100, 700, "7d")),
            ({"rpm_limit": 25, "max_budget": 250, "budget_duration": "1d"}, (25, 250, "1d")),
        ):
            with self.subTest(options=options), patch.object(module, "request_json", return_value={"key": "synthetic-key"}) as request:
                module.create_api_key("user-1", "alias", **CREDENTIALS, **options)
                payload = json.loads(request.call_args.kwargs["data"])
                self.assertEqual(
                    (payload["rpm_limit"], payload["max_budget"], payload["budget_duration"]),
                    expected,
                )

    def test_new_user_flow_preserves_payload_order_and_progress(self):
        module = backend()
        reports = []
        responses = [{"users": []}, {"user_id": "user-1"}, {"key": "sk-created"}]
        with patch.object(module, "request_json", side_effect=responses) as request:
            result = module.create_key_for_user("user+test@example.com", key_value="sk-custom", team_id="team", models=[" General ", "", " Other"], report=reports.append, **CREDENTIALS)
        self.assertEqual(result, "sk-created")
        self.assertEqual(reports, ["Processing user: user+test@example.com", "User created: user-1", "API key created"])
        self.assertEqual([call.args[0] for call in request.call_args_list], [
            "http://litellm.test/user/list?user_email=user%2Btest%40example.com&page=1&page_size=100",
            "http://litellm.test/user/new", "http://litellm.test/key/generate",
        ])
        self.assertEqual([call.kwargs["method"] for call in request.call_args_list], ["GET", "POST", "POST"])
        self.assertEqual(json.loads(request.call_args_list[1].kwargs["data"]), {"user_id": None, "user_email": "user+test@example.com", "user_role": "internal_user_viewer", "models": ["General", "Other"], "auto_create_key": False})
        self.assertEqual(json.loads(request.call_args_list[2].kwargs["data"]), {
            "user_id": "user-1", "team_id": "team", "key_alias": "user+test", "models": ["General", "Other"], "key_type": "llm_api", "rpm_limit": 100, "max_budget": 700, "budget_duration": "7d", "metadata": {}, "key": "sk-custom",
        })
        self.assertEqual(request.call_args_list[0].kwargs["headers"], {"Authorization": "Bearer dummy", "Accept": "application/json"})
        for call in request.call_args_list[1:]:
            self.assertEqual(call.kwargs["headers"], {"Authorization": "Bearer dummy", "Content-Type": "application/json", "Accept": "*/*"})

    def test_existing_user_is_reused_without_creation(self):
        module = backend()
        reports = []
        with patch.object(module, "request_json", side_effect=[{"users": [{"user_email": "other@example.com", "user_id": "other"}, {"user_email": "user@example.com", "user_id": "user-1"}]}, {"key": "sk-created"}]) as request:
            self.assertEqual(module.create_key_for_user("user@example.com", alias="explicit", report=reports.append, **CREDENTIALS), "sk-created")
        self.assertEqual(request.call_count, 2)
        payload = json.loads(request.call_args_list[1].kwargs["data"])
        self.assertEqual(payload["models"], [])
        self.assertEqual(payload["key_alias"], "explicit")
        self.assertNotIn("key", payload)
        self.assertEqual(reports[1], "User already exists: user-1")

    def test_default_user_models_and_missing_creation_id_fail_closed(self):
        module = backend()
        with patch.object(module, "request_json", side_effect=[{"users": []}, {}]) as request:
            with self.assertRaisesRegex(CommandError, "LiteLLM user creation returned no user_id"):
                module.create_key_for_user("user@example.com", **CREDENTIALS)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(json.loads(request.call_args.kwargs["data"])["models"], ["General"])

    def test_missing_created_key_does_not_report_success(self):
        module = backend()
        reports = []
        with patch.object(module, "request_json", side_effect=[{"users": []}, {"user_id": "user-1"}, {}]):
            with self.assertRaisesRegex(CommandError, "LiteLLM API-key creation returned no key"):
                module.create_key_for_user("user@example.com", report=reports.append, **CREDENTIALS)
        self.assertEqual(reports, ["Processing user: user@example.com", "User created: user-1"])

    def test_malformed_lookup_inventory_is_rejected(self):
        module = backend()
        for response in ([], {}, {"users": {}}):
            with self.subTest(response=response), patch.object(module, "request_json", return_value=response):
                with self.assertRaisesRegex(CommandError, "User lookup returned an unexpected response"):
                    module.get_user_by_email("user@example.com", **CREDENTIALS)


class KeyCreationIsolationTest(unittest.TestCase):
    def test_backend_is_library_shaped_and_adapter_has_no_business_helpers(self):
        module = backend()
        source = Path(module.__file__).read_text()
        tree = ast.parse(source)
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        self.assertFalse(any(name in {"argparse", "os", "sys"} or name.startswith("llmproxy.core.context") for name in imports))
        self.assertFalse({"COMMAND", "ALIASES", "DESCRIPTION", "MUTATING", "run", "main", "configure"}.intersection(vars(module)))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                self.assertFalse({"args", "context"}.intersection(arg.arg for arg in node.args.args + node.args.kwonlyargs))
        self.assertFalse({"request_json", "get_user_by_email", "create_user", "create_api_key"}.intersection(vars(command)))
        adapter = ast.parse(Path(command.__file__).read_text())
        self.assertFalse(any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(adapter)))

    def test_configure_does_not_load_backend_environment_or_data(self):
        parser = argparse.ArgumentParser()
        with (
            patch.object(command, "load_component_module", side_effect=AssertionError("backend loaded")),
            patch.object(command, "load_dotenv", side_effect=AssertionError("environment loaded")),
            patch("builtins.open", side_effect=AssertionError("data read")),
        ):
            command.configure(parser)
            args = parser.parse_args(["user@example.com"])
            self.assertEqual((args.rpm_limit, args.max_budget, args.budget_duration), (100, 700, "7d"))
            self.assertIn("User email address", parser.format_help())

    def test_selected_root_backend_is_used_instead_of_wrapper_checkout(self):
        from llmproxy.core.component_modules import load_component_module
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component = root / "components" / "llmproxy" / "litellm"
            (component / "src").mkdir(parents=True)
            (component / "compose.yaml").write_text("services: {}\n")
            (component / "src" / "key_create.py").write_text("def create_key_for_user(*args, **kwargs):\n    raise AssertionError('unmocked synthetic backend')\n")
            selected = load_component_module(root, "llmproxy/litellm", "src.key_create")
            parser = argparse.ArgumentParser()
            command.configure(parser)
            with (
                patch.dict(os.environ, {"LITELLM_API_KEY": "dummy", "LITELLM_BASE_URL": "http://litellm.test"}),
                patch.object(command, "load_dotenv"),
                patch.object(selected, "create_key_for_user", return_value="selected-root-key") as create,
                redirect_stdout(io.StringIO()) as stdout,
            ):
                self.assertEqual(command.run(parser.parse_args(["user@example.com"]), SimpleNamespace(root=root)), 0)
            create.assert_called_once()
            self.assertEqual(stdout.getvalue(), "selected-root-key\n")

    def test_missing_management_environment_stops_before_loading_backend(self):
        parser = argparse.ArgumentParser()
        command.configure(parser)
        with patch.dict(os.environ, {}, clear=True), patch.object(command, "load_dotenv"), patch.object(command, "load_component_module") as load:
            with self.assertRaisesRegex(CommandError, "Missing management environment: LITELLM_API_KEY, LITELLM_BASE_URL"):
                command.run(parser.parse_args(["user@example.com"]), SimpleNamespace(root=Path("/synthetic")))
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
