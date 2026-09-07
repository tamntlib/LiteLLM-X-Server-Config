import argparse
import ast
import urllib.error
import importlib
from llmproxy.core.command import CommandError
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


DUMMY_MANAGEMENT_ENV = {
    "LITELLM_API_KEY": "dummy",
    "LITELLM_BASE_URL": "http://litellm.test",
}


command = importlib.import_module("..commands.key_limits", __package__)
update_api_key_limits = importlib.import_module("..src.key_limits", __package__)
CREDENTIALS = {"base_url": "http://litellm.test", "api_key": "dummy"}


class KeyLimitOverrideTest(unittest.TestCase):
    def test_canonical_limits_local_layer_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            limits = Path(tmpdir) / "configs" / "key-limits.json"
            limits.parent.mkdir()
            limits.write_text('{"$schema": "./key-limits.schema.json", "key-a": {"rpm_limit": 250}}')
            limits.with_name("key-limits.local.json").write_text('{"key-a": {"rpm_limit": 350}}')
            self.assertEqual(update_api_key_limits.load_key_limit_overrides(limits), {"key-a": {"rpm_limit": 350}})

    def test_run_reads_limits_from_the_selected_component_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component_dir = root / "selected-owner"
            limits = component_dir / "configs" / "key-limits.json"
            context = type("Context", (), {"root": root, "component_dir": component_dir})()
            parser = argparse.ArgumentParser()
            command.configure(parser)
            args = parser.parse_args([])
            module = Mock()
            module.set_key_limits.return_value = 0
            with (
                patch.dict(os.environ, DUMMY_MANAGEMENT_ENV),
                patch.object(command, "load_dotenv") as dotenv,
                patch.object(command, "load_component_module", create=True, return_value=module) as load,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(command.run(args, context), 0)
            dotenv.assert_called_once_with(root / ".env")
            load.assert_called_once_with(root, "llmproxy/litellm", "src.key_limits")
            kwargs = dict(module.set_key_limits.call_args.kwargs)
            report = kwargs.pop("report")
            self.assertEqual(kwargs, dict(CREDENTIALS, config_path=limits, rpm_limit=100, max_budget=700, budget_duration="7d", page_size=100, reset_spend=False, apply=False))
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                report("progress")
            self.assertEqual(stderr.getvalue(), "progress\n")

    def test_matching_override_replaces_rpm_and_max_budget(self):
        key_record = {
            "key_alias": "key-a",
            "token": "sk-a",
        }

        limits = update_api_key_limits.resolve_key_limits(
            key_record,
            100,
            3000,
            {
                "key-a": {
                    "rpm_limit": 4000,
                    "max_budget": 5000,
                }
            },
        )

        self.assertEqual(limits, (4000, 5000))

    def test_unmatched_key_uses_supplied_defaults(self):
        limits = update_api_key_limits.resolve_key_limits(
            {"key_alias": "key-b"},
            150,
            3500,
            {"key-a": {"rpm_limit": 4000, "max_budget": 5000}},
        )

        self.assertEqual(limits, (150, 3500))

    def test_partial_override_keeps_other_default(self):
        limits = update_api_key_limits.resolve_key_limits(
            {"key_alias": "key-a"},
            100,
            3000,
            {"key-a": {"max_budget": 5000}},
        )

        self.assertEqual(limits, (100, 5000))

    def test_multipliers_are_applied_to_supplied_defaults(self):
        limits = update_api_key_limits.resolve_key_limits(
            {"key_alias": "key-a"},
            100,
            3000,
            {
                "key-a": {
                    "rpm_limit_multiplier": 2.5,
                    "max_budget_multiplier": 1.5,
                }
            },
        )

        self.assertEqual(limits, (250, 4500))

    def test_explicit_limits_take_precedence_over_multipliers(self):
        limits = update_api_key_limits.resolve_key_limits(
            {"key_alias": "key-a"},
            100,
            3000,
            {
                "key-a": {
                    "rpm_limit": 400,
                    "rpm_limit_multiplier": 2,
                    "max_budget": 5000,
                    "max_budget_multiplier": 3,
                }
            },
        )

        self.assertEqual(limits, (400, 5000))

    def test_explicit_unlimited_limits_take_precedence_over_multipliers(self):
        limits = update_api_key_limits.resolve_key_limits(
            {"key_alias": "key-a"},
            100,
            3000,
            {
                "key-a": {
                    "rpm_limit": None,
                    "rpm_limit_multiplier": 2,
                    "max_budget": None,
                    "max_budget_multiplier": 3,
                }
            },
        )

        self.assertEqual(limits, (None, None))

    def test_rpm_multiplier_must_produce_an_integer_limit(self):
        with self.assertRaisesRegex(ValueError, "must produce an integer"):
            update_api_key_limits.resolve_key_limits(
                {"key_alias": "key-a"},
                50,
                3000,
                {"key-a": {"rpm_limit_multiplier": 0.333}},
            )

    def test_multiplier_cannot_be_applied_to_an_unlimited_default(self):
        with self.assertRaisesRegex(ValueError, "default max_budget is unlimited"):
            update_api_key_limits.resolve_key_limits(
                {"key_alias": "key-a"},
                50,
                None,
                {"key-a": {"max_budget_multiplier": 2}},
            )

    def test_override_matches_all_supported_key_fields(self):
        fields = (
            "key",
            "token",
            "key_name",
            "key_alias",
            "api_key",
            "user_id",
            "user_email",
        )

        for field in fields:
            with self.subTest(field=field):
                limits = update_api_key_limits.resolve_key_limits(
                    {field: "matching-value"},
                    100,
                    3000,
                    {"matching-value": {"rpm_limit": 4000}},
                )
                self.assertEqual(limits, (4000, 3000))

    def test_first_matching_override_wins(self):
        key_record = {
            "key_alias": "key-a",
            "user_id": "user-a",
        }

        limits = update_api_key_limits.resolve_key_limits(
            key_record,
            100,
            3000,
            {
                "key-a": {"rpm_limit": 4000, "max_budget": 5000},
                "user-a": {"rpm_limit": 6000, "max_budget": 7000},
            },
        )

        self.assertEqual(limits, (4000, 5000))

    def test_validation_rejects_invalid_override_configuration(self):
        invalid_configs = (
            [],
            {"": {"rpm_limit": 4000}},
            {"key-a": {}},
            {"key-a": 4000},
            {"key-a": {"rpm_limit": 0}},
            {"key-a": {"rpm_limit": 1.5}},
            {"key-a": {"rpm_limit": True}},
            {"key-a": {"max_budget": 0}},
            {"key-a": {"max_budget": "5000"}},
            {"key-a": {"rpm_limit_multiplier": 0}},
            {"key-a": {"rpm_limit_multiplier": True}},
            {"key-a": {"rpm_limit_multiplier": "2"}},
            {"key-a": {"max_budget_multiplier": -1}},
            {"key-a": {"max_budget_multiplier": None}},
            {"key-a": {"unknown": 1}},
        )

        for overrides in invalid_configs:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    update_api_key_limits.validate_key_limit_overrides(overrides)

    def test_load_overrides_without_local_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "key-limits.json"
            expected = {"key-a": {"rpm_limit": 4000}}
            config_path.write_text(json.dumps(expected))

            overrides = update_api_key_limits.load_key_limit_overrides(config_path)

        self.assertEqual(overrides, expected)

    def test_schema_metadata_is_ignored_when_loading_overrides(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "key-limits.json"
            config_path.write_text(
                json.dumps(
                    {
                        "$schema": "./key-limits.schema.json",
                        "key-a": {"rpm_limit_multiplier": 2},
                    }
                )
            )

            overrides = update_api_key_limits.load_key_limit_overrides(config_path)

        self.assertEqual(overrides, {"key-a": {"rpm_limit_multiplier": 2}})

    def test_local_overrides_are_deep_merged(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "key-limits.json"
            local_path = Path(tmp_dir) / "key-limits.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "key-a": {"rpm_limit": 4000, "max_budget": 5000},
                        "key-b": {"rpm_limit": 200},
                    }
                )
            )
            local_path.write_text(
                json.dumps(
                    {
                        "key-a": {"max_budget": 6000},
                        "key-c": {"rpm_limit": 300},
                    }
                )
            )

            overrides = update_api_key_limits.load_key_limit_overrides(config_path)

        self.assertEqual(
            overrides,
            {
                "key-a": {"rpm_limit": 4000, "max_budget": 6000},
                "key-b": {"rpm_limit": 200},
                "key-c": {"rpm_limit": 300},
            },
        )

    def test_load_overrides_rejects_malformed_or_invalid_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "key-limits.json"
            config_path.write_text("{")
            with self.assertRaises(json.JSONDecodeError):
                update_api_key_limits.load_key_limit_overrides(config_path)

            config_path.write_text(json.dumps({"key-a": {"rpm_limit": 0}}))
            with self.assertRaises(ValueError):
                update_api_key_limits.load_key_limit_overrides(config_path)

    def test_config_failure_stops_before_listing_keys(self):
        with (
            patch.object(
                update_api_key_limits,
                "load_key_limit_overrides",
                side_effect=ValueError("invalid limits"),
            ),
            patch.object(update_api_key_limits, "resolve_target_keys") as resolve,
            self.assertRaisesRegex(CommandError, "Invalid API key limits configuration"),
        ):
            update_api_key_limits.set_key_limits(config_path=Path("/synthetic/key-limits.json"), **CREDENTIALS)

        resolve.assert_not_called()

    def test_dry_run_prints_effective_limits_per_key(self):
        keys = [
            {"key_alias": "key-a", "token": "sk-a"},
            {"key_alias": "key-b", "token": "sk-b"},
        ]
        stderr = io.StringIO()

        with (
            patch.object(
                update_api_key_limits,
                "load_key_limit_overrides",
                return_value={
                    "key-a": {"rpm_limit": 4000, "max_budget": 5000}
                },
            ),
            patch.object(
                update_api_key_limits,
                "resolve_target_keys",
                return_value=keys,
            ),
            patch.object(update_api_key_limits, "update_api_key") as update,
            redirect_stderr(stderr),
        ):
            update_api_key_limits.set_key_limits(config_path=Path("/synthetic/key-limits.json"), rpm_limit=150, max_budget=3500, report=lambda message: print(message, file=sys.stderr), **CREDENTIALS)

        output = stderr.getvalue()
        self.assertIn(
            "Would update key-a (sk-a): rpm_limit=4000, max_budget=5000",
            output,
        )
        self.assertIn(
            "Would update key-b (sk-b): rpm_limit=150, max_budget=3500",
            output,
        )
        update.assert_not_called()

    def test_apply_passes_effective_limits_per_key(self):
        keys = [
            {"key_alias": "key-a", "token": "sk-a"},
            {"key_alias": "key-b", "token": "sk-b"},
        ]

        with (
            patch.object(
                update_api_key_limits,
                "load_key_limit_overrides",
                return_value={
                    "key-a": {"rpm_limit": 4000, "max_budget": 5000}
                },
            ),
            patch.object(
                update_api_key_limits,
                "resolve_target_keys",
                return_value=keys,
            ),
            patch.object(update_api_key_limits, "update_api_key") as update,
            redirect_stderr(io.StringIO()),
        ):
            update_api_key_limits.set_key_limits(config_path=Path("/synthetic/key-limits.json"), rpm_limit=150, max_budget=3500.0, apply=True, **CREDENTIALS)

        self.assertEqual(update.call_count, 2)
        self.assertEqual(update.call_args_list[0].args[1:3], (4000, 5000))
        self.assertEqual(update.call_args_list[1].args[1:3], (150, 3500.0))


class KeyLimitsContractTest(unittest.TestCase):
    def test_configured_selectors_are_defaults_but_explicit_empty_is_respected(self):
        keys = [{"token": "sk-a", "key_alias": "selected"}, {"token": "sk-b", "key_alias": "selected", "user_id": "excluded"}, {"token": "sk-c", "key_alias": "other"}]
        with patch.object(update_api_key_limits, "TARGET_KEYS", ["selected"], create=True), patch.object(update_api_key_limits, "EXCLUDED_KEYS", ["excluded"], create=True), patch.object(update_api_key_limits, "list_api_keys", return_value=keys):
            self.assertEqual(update_api_key_limits.resolve_target_keys(100, **CREDENTIALS), keys[:1])
            self.assertEqual(update_api_key_limits.resolve_target_keys(100, target_keys=[], excluded_keys=[], **CREDENTIALS), keys)

    def test_plan_uses_configured_selector_defaults_in_scope_and_selection(self):
        reports = []
        with patch.object(update_api_key_limits, "TARGET_KEYS", ["selected"], create=True), patch.object(update_api_key_limits, "EXCLUDED_KEYS", [], create=True), patch.object(update_api_key_limits, "load_key_limit_overrides", return_value={}), patch.object(update_api_key_limits, "list_api_keys", return_value=[{"token": "sk-a", "key_alias": "selected"}, {"token": "sk-b", "key_alias": "other"}]):
            update_api_key_limits.set_key_limits(config_path=Path("/synthetic/key-limits.json"), report=reports.append, **CREDENTIALS)
            self.assertIn("for 1 configured target key(s).", reports[0])
            self.assertEqual(reports[1], "Would update selected (sk-a): rpm_limit=100, max_budget=700")
            reports.clear()
            update_api_key_limits.set_key_limits(config_path=Path("/synthetic/key-limits.json"), target_keys=[], excluded_keys=[], report=reports.append, **CREDENTIALS)
            self.assertIn("for 2 all key(s).", reports[0])

    def test_inventory_pagination_preserves_request_order_and_headers(self):
        with patch.object(update_api_key_limits, "request_json", side_effect=[{"keys": [{"token": "sk-a"}], "total_pages": 2}, {"keys": [{"token": "sk-b"}], "total_pages": 2}]) as request:
            keys = update_api_key_limits.list_api_keys(75, **CREDENTIALS)
        self.assertEqual(keys, [{"token": "sk-a"}, {"token": "sk-b"}])
        self.assertEqual([call.args[0] for call in request.call_args_list], ["http://litellm.test/key/list?page=1&size=75&return_full_object=true", "http://litellm.test/key/list?page=2&size=75&return_full_object=true"])
        for call in request.call_args_list:
            self.assertEqual(call.kwargs, {"method": "GET", "headers": {"Authorization": "Bearer dummy", "Content-Type": "application/json", "Accept": "application/json"}})

    def test_inventory_rejects_non_list_keys(self):
        with patch.object(update_api_key_limits, "request_json", return_value={"keys": {}}):
            with self.assertRaisesRegex(ValueError, "/key/list response did not include a keys list"):
                update_api_key_limits.list_api_keys(100, **CREDENTIALS)

    def test_update_payload_and_response_semantics_are_preserved(self):
        with patch.object(update_api_key_limits, "request_json", return_value={"ok": True}) as request:
            self.assertEqual(update_api_key_limits.update_api_key({"token": "sk-a", "key_alias": "alias"}, 250, 900, "7d", True, **CREDENTIALS), {"ok": True})
        request.assert_called_once_with("http://litellm.test/key/update", data=json.dumps({"key": "sk-a", "rpm_limit": 250, "max_budget": 900, "budget_duration": "7d", "spend": 0}).encode("utf-8"), method="POST", headers={"Authorization": "Bearer dummy", "Content-Type": "application/json", "Accept": "application/json"})
        self.assertNotIn("spend", update_api_key_limits.build_update_payload("sk-a", 100, 700, "7d"))

    def test_targets_then_exclusions_use_explicit_selectors(self):
        keys = [{"token": "sk-a", "key_alias": "selected"}, {"token": "sk-b", "key_alias": "selected", "user_id": "excluded"}, {"token": "sk-c", "key_alias": "other"}]
        with patch.object(update_api_key_limits, "list_api_keys", return_value=keys) as inventory:
            self.assertEqual(update_api_key_limits.resolve_target_keys(50, target_keys=["selected"], excluded_keys=["excluded"], **CREDENTIALS), keys[:1])
        inventory.assert_called_once_with(50, **CREDENTIALS)

    def test_apply_preplans_all_limits_before_first_update(self):
        with (
            patch.object(update_api_key_limits, "load_key_limit_overrides", return_value={"bad": {"rpm_limit_multiplier": 0.333}}),
            patch.object(update_api_key_limits, "resolve_target_keys", return_value=[{"token": "sk-a"}, {"token": "sk-b", "key_alias": "bad"}]),
            patch.object(update_api_key_limits, "update_api_key") as update,
        ):
            with self.assertRaisesRegex(ValueError, "must produce an integer"):
                update_api_key_limits.set_key_limits(config_path=Path("/synthetic/key-limits.json"), apply=True, **CREDENTIALS)
        update.assert_not_called()

    def test_apply_continues_after_failure_then_raises_count(self):
        reports = []
        keys = [{"token": "sk-a"}, {"token": "sk-b"}]
        with (
            patch.object(update_api_key_limits, "load_key_limit_overrides", return_value={}),
            patch.object(update_api_key_limits, "resolve_target_keys", return_value=keys),
            patch.object(update_api_key_limits, "update_api_key", side_effect=[OSError("offline"), {}]) as update,
        ):
            with self.assertRaisesRegex(CommandError, "Completed with 1 failed update"):
                update_api_key_limits.set_key_limits(config_path=Path("/synthetic/key-limits.json"), apply=True, report=reports.append, **CREDENTIALS)
        self.assertEqual(update.call_count, 2)
        self.assertEqual(reports[-2:], ["Failed to update sk-a: offline", "Updated sk-b: rpm_limit=100, max_budget=700"])

    def test_empty_plan_has_exact_summary_and_page_size_cap(self):
        reports = []
        with patch.object(update_api_key_limits, "load_key_limit_overrides", return_value={}), patch.object(update_api_key_limits, "resolve_target_keys", return_value=[]) as resolve:
            self.assertEqual(update_api_key_limits.set_key_limits(config_path=Path("/synthetic/key-limits.json"), page_size=999, reset_spend=True, report=reports.append, **CREDENTIALS), 0)
        resolve.assert_called_once_with(100, target_keys=[], excluded_keys=["default_user_id"], **CREDENTIALS)
        self.assertEqual(reports, ["DRY RUN: set default_rpm_limit=100, default_max_budget=700, key_limit_overrides=0, budget_duration=7d, spend=0 for 0 all key(s) minus exclusions.", "No API keys to update after applying targets/exclusions."])

    def test_dry_run_uses_canonical_synthetic_rules_and_never_writes(self):
        reports = []
        with tempfile.TemporaryDirectory() as tmpdir:
            config = Path(tmpdir) / "configs" / "key-limits.json"
            config.parent.mkdir()
            config.write_text('{"key-a": {"rpm_limit_multiplier": 2}}')
            config.with_name("key-limits.local.json").write_text('{"key-a": {"max_budget": 900}}')
            with patch.object(update_api_key_limits, "request_json", return_value={"keys": [{"token": "sk-a", "key_alias": "key-a"}, {"token": "sk-admin", "user_id": "default_user_id"}]}) as request:
                self.assertEqual(update_api_key_limits.set_key_limits(config_path=config, report=reports.append, **CREDENTIALS), 0)
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["method"], "GET")
        self.assertEqual(reports, ["DRY RUN: set default_rpm_limit=100, default_max_budget=700, key_limit_overrides=1, budget_duration=7d for 1 all key(s) minus exclusions.", "Would update key-a (sk-a): rpm_limit=200, max_budget=900", "Add --apply to apply these changes."])

    def test_inventory_errors_keep_domain_messages(self):
        failures = [(ValueError("invalid inventory"), "Invalid target/excluded keys: invalid inventory"), (urllib.error.HTTPError("http://litellm.test/key/list", 403, "Forbidden", {}, None), "Failed to list API keys:")]
        for error, message in failures:
            with self.subTest(message=message), patch.object(update_api_key_limits, "load_key_limit_overrides", return_value={}), patch.object(update_api_key_limits, "resolve_target_keys", side_effect=error):
                with self.assertRaisesRegex(CommandError, message):
                    update_api_key_limits.set_key_limits(config_path=Path("/synthetic/key-limits.json"), **CREDENTIALS)


class KeyLimitsIsolationTest(unittest.TestCase):
    def test_backend_is_library_shaped_and_adapter_has_no_business_helpers(self):
        source = Path(update_api_key_limits.__file__).read_text()
        tree = ast.parse(source)
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        self.assertFalse(any(name in {"argparse", "os", "sys"} or name.startswith("llmproxy.core.context") for name in imports))
        self.assertFalse({"COMMAND", "ALIASES", "DESCRIPTION", "MUTATING", "run", "main", "configure", "API_KEY_LIMITS_CONFIG_PATH"}.intersection(vars(update_api_key_limits)))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                self.assertFalse({"args", "context"}.intersection(arg.arg for arg in node.args.args + node.args.kwonlyargs))
        self.assertFalse({"request_json", "load_key_limit_overrides", "list_api_keys", "update_api_key", "resolve_key_limits"}.intersection(vars(command)))
        adapter = ast.parse(Path(command.__file__).read_text())
        self.assertFalse(any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(adapter)))

    def test_configure_does_not_load_backend_environment_or_data(self):
        parser = argparse.ArgumentParser()
        with patch.object(command, "load_component_module", side_effect=AssertionError("backend loaded")), patch.object(command, "load_dotenv", side_effect=AssertionError("environment loaded")), patch("builtins.open", side_effect=AssertionError("data read")):
            command.configure(parser)
            self.assertEqual(parser.parse_args([]).page_size, 100)
            self.assertIn("--apply", parser.format_help())

    def test_selected_root_backend_is_used_instead_of_wrapper_checkout(self):
        from llmproxy.core.component_modules import load_component_module
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component = root / "components" / "llmproxy" / "litellm"
            (component / "src").mkdir(parents=True)
            (component / "compose.yaml").write_text("services: {}\n")
            (component / "src" / "key_limits.py").write_text("def set_key_limits(**kwargs):\n    raise AssertionError('unmocked synthetic backend')\n")
            selected = load_component_module(root, "llmproxy/litellm", "src.key_limits")
            parser = argparse.ArgumentParser()
            command.configure(parser)
            with patch.dict(os.environ, DUMMY_MANAGEMENT_ENV), patch.object(command, "load_dotenv"), patch.object(selected, "set_key_limits", return_value=0) as limits:
                self.assertEqual(command.run(parser.parse_args(["--apply", "--page-size", "999", "--reset-spend"]), SimpleNamespace(root=root, component_dir=root / "chosen-config-owner")), 0)
            kwargs = limits.call_args.kwargs
            self.assertEqual(kwargs["config_path"], root / "chosen-config-owner" / "configs" / "key-limits.json")
            self.assertTrue(kwargs["apply"])
            self.assertTrue(kwargs["reset_spend"])
            self.assertEqual(kwargs["page_size"], 999)

    def test_missing_environment_stops_before_loading_backend(self):
        parser = argparse.ArgumentParser()
        command.configure(parser)
        with patch.dict(os.environ, {}, clear=True), patch.object(command, "load_dotenv"), patch.object(command, "load_component_module") as load:
            with self.assertRaisesRegex(CommandError, "Missing management environment: LITELLM_API_KEY, LITELLM_BASE_URL"):
                command.run(parser.parse_args([]), SimpleNamespace(root=Path("/synthetic")))
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
