"""Source-owned Claude/GPT model alias updater contracts (no live API calls)."""

from email.message import Message
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_model_aliases.py"


def updater():
    spec = importlib.util.spec_from_file_location("update_model_aliases", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UpdateClaudeAliasesTest(unittest.TestCase):
    def test_renames_only_claude_alias_keys_preserving_format_and_targets(self):
        module = updater()
        text = ('{\n  "other": {"claude-opus-5": "leave"},\n'
                '  "aliases": {\n'
                '    "claude-*": "a-1",\n'
                '    "claude-opus-5": {"model": "a-1", "hidden": true},\n'
                '    "openai/claude-opus-5": "o-1",\n'
                '    "gpt-6": "o-1"\n  }\n}\n')
        updated, changes = module.update_aliases(text, {"opus": "claude-opus-5-5"})
        expected = text.replace('"claude-opus-5": {', '"claude-opus-5-5": {').replace(
            '"openai/claude-opus-5":', '"openai/claude-opus-5-5":'
        )
        self.assertEqual(updated, expected)
        self.assertEqual(len(changes), 2)
        self.assertEqual(json.loads(updated)["other"], {"claude-opus-5": "leave"})

    def test_public_anthropic_uses_only_latest_api_id_row_without_auth(self):
        module = updater()
        document = ('# Models overview\n\n## Latest models\n'
                    '| Feature | Claude Opus | Claude Sonnet |\n'
                    '| Claude API ID | `claude-opus-9-1` | `claude-sonnet-9` |\n'
                    '| Amazon Bedrock ID | `claude-opus-99` |\n'
                    '## All models\n`claude-opus-99`\n')
        requests = []

        def open_document(request, timeout):
            requests.append(request)
            self.assertEqual(timeout, 30)
            return io.BytesIO(document.encode())

        latest = module.fetch_latest_public("anthropic", {"opus", "sonnet"}, open_url=open_document)
        self.assertEqual(latest, {"opus": "claude-opus-9-1", "sonnet": "claude-sonnet-9"})
        self.assertEqual(requests[0].full_url, "https://platform.claude.com/docs/en/models/overview.md")
        self.assertIsNone(requests[0].get_header("Authorization"))

    def test_claude_base_model_preserves_namespace_and_does_not_follow_route_alias(self):
        module = updater()
        text = ('{"aliases": {"claude-fable-5": "a-0"}, '
                '"model_name_base_model_map": {"a-0": "anthropic/claude-opus-5", "a-1": "gpt-6-sol"}}')
        updated, changes = module.update_base_models(text, {"opus": "claude-opus-5-5"})
        self.assertEqual(changes, [("a-0", "anthropic/claude-opus-5", "anthropic/claude-opus-5-5")])
        self.assertEqual(updated, text.replace('"anthropic/claude-opus-5"', '"anthropic/claude-opus-5-5"'))
        with self.assertRaisesRegex(module.UpdateError, "invalid latest model"):
            module.update_base_models(text, {"opus": "claude-fable-5"})

    def test_invalid_base_model_map_aborts_before_discovery(self):
        module = updater()
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            text = '{"aliases":{"claude-opus-5":"a-1"},"model_name_base_model_map":{"a-1":null}}'
            config.write_text(text)
            with patch.object(module, "fetch_latest_public") as fetch:
                with patch("sys.stderr", new_callable=io.StringIO) as output:
                    self.assertEqual(module.main(["--config", str(config), "--write"]), 1)
                fetch.assert_not_called()
                self.assertIn("model_name_base_model_map must be an object of strings", output.getvalue())
            self.assertEqual(config.read_text(), text)

    def test_fetches_newest_active_model_per_line_across_pages(self):
        module = updater()
        pages = [
            {"data": [
                {"line": "mythos", "id": "claude-fable-not-this", "lifecycle": "active"},
                {"line": "opus", "id": "opus-deprecated", "lifecycle": "deprecated"},
                {"line": "opus", "id": "claude-opus-5-5", "lifecycle": "active"},
                {"line": "opus", "id": "claude-opus-5", "lifecycle": "active"},
            ], "has_more": True, "last_id": "cursor /?"},
            {"data": [{"line": "fable", "id": "claude-fable-5-2", "lifecycle": "active"}],
             "has_more": False, "last_id": "claude-fable-5-2"},
        ]
        requests = []

        def open_page(request, timeout):
            requests.append(request)
            self.assertEqual(timeout, 30)
            return io.BytesIO(json.dumps(pages[len(requests) - 1]).encode())

        latest = module.fetch_latest("test-key", {"opus", "fable"}, open_url=open_page)
        self.assertEqual(latest, {"opus": "claude-opus-5-5", "fable": "claude-fable-5-2"})
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer test-key")
        query = parse_qs(urlparse(requests[1].full_url).query)
        self.assertEqual(query["after_id"], ["cursor /?"])
        self.assertEqual(query["lifecycle[]"], ["active"])

    def test_cli_previews_then_writes_only_with_explicit_flag(self):
        module = updater()
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            original = '{"aliases": {"claude-opus-5": "a-1"}}\n'
            config.write_text(original)
            with patch.object(module, "fetch_latest", return_value={"opus": "claude-opus-5-5"}):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(module.main(["--source", "api", "--config", str(config)]), 0)
                self.assertEqual(config.read_text(), original)
                self.assertIn("Dry run", output.getvalue())
                with patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(module.main(["--source", "api", "--config", str(config), "--write"]), 0)
                self.assertEqual(json.loads(config.read_text())["aliases"], {"claude-opus-5-5": "a-1"})
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
                # A second run is byte-for-byte idempotent.
                before = config.stat().st_mtime_ns
                with patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(module.main(["--source", "api", "--config", str(config), "--write"]), 0)
                self.assertEqual(config.stat().st_mtime_ns, before)

    def test_missing_api_key_has_actionable_error_without_writing(self):
        module = updater()
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            original = '{"aliases": {"claude-opus-5": "a-1"}}'
            config.write_text(original)
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
                with patch("sys.stderr", new_callable=io.StringIO) as output:
                    self.assertEqual(module.main(["--source", "api", "--config", str(config), "--write"]), 1)
            self.assertEqual(output.getvalue().strip(), "Update aborted: ANTHROPIC_API_KEY is required")
            self.assertEqual(config.read_text(), original)

    def test_repository_config_preserves_all_route_values_and_unrelated_sections(self):
        module = updater()
        config = SCRIPT.parents[1] / "integrations/llmproxy/litellm/config.json"
        text = config.read_text()
        before = json.loads(text)
        latest = {"fable": "claude-fable-5-2", "opus": "claude-opus-5-5",
                  "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5"}
        self.assertEqual(module.required_lines(text), set(latest))
        updated, changes = module.update_aliases(text, latest)
        after = json.loads(updated)
        expected = dict(before)
        renames = dict(changes)
        expected["aliases"] = {renames.get(key, key): value for key, value in before["aliases"].items()}
        self.assertEqual(after, expected)
        for prefix, route_prefix in (("", "a"), ("openai/", "o")):
            for index, line in enumerate(("fable", "opus", "sonnet", "haiku")):
                self.assertEqual(after["aliases"][prefix + latest[line]], f"{route_prefix}-{index}")
        self.assertEqual(module.update_aliases(updated, latest), (updated, []))

    def test_collision_duplicate_keys_and_missing_line_fail_closed(self):
        module = updater()
        cases = [
            ('{"aliases":{"claude-opus-5":"a-1","claude-opus-5-5":"a-2"}}',
             {"opus": "claude-opus-5-5"}, "collision"),
            ('{"aliases":{"claude-opus-5":"a-1","claude-opus-5":"a-2"}}',
             {"opus": "claude-opus-5-5"}, "Duplicate"),
            ('{"aliases":{"claude-opus-5":"a-1"}}', {}, "Missing"),
        ]
        for text, latest, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                module.update_aliases(text, latest)

    def test_empty_or_malformed_catalogue_never_selects_a_fallback(self):
        module = updater()
        cases = [
            {"data": [], "has_more": False},
            {"data": [], "has_more": True, "last_id": "repeated"},
            {"data": []},
            {"data": "invalid"},
            {"data": [None]},
            {"data": [{"line": "opus", "lifecycle": "active", "id": ""}]},
        ]
        for page in cases:
            with self.subTest(page=page):
                def open_page(request, timeout):
                    return io.BytesIO(json.dumps(page).encode())
                with self.assertRaises(ValueError):
                    module.fetch_latest("test-key", {"opus"}, open_url=open_page)

    def test_network_error_redacts_response_and_does_not_write(self):
        module = updater()
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            original = '{"aliases": {"claude-opus-5": "a-1"}}'
            config.write_text(original)
            error = HTTPError("https://api.anthropic.com/v1/models", 401, "SECRET", Message(), io.BytesIO(b"SECRET"))
            with patch.object(module, "fetch_latest", side_effect=error):
                with patch("sys.stderr", new_callable=io.StringIO) as output:
                    self.assertEqual(module.main(["--source", "api", "--config", str(config), "--write"]), 1)
            self.assertEqual(output.getvalue().strip(), "Model discovery failed: HTTP 401")
            self.assertEqual(config.read_text(), original)

    def test_concurrent_edit_is_not_overwritten(self):
        module = updater()
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text('{"aliases":{"claude-opus-5":"a-1"}}')
            concurrent = '{"aliases":{"claude-opus-5":"a-2"}}'

            def fetch(*args):
                config.write_text(concurrent)
                return {"opus": "claude-opus-5-5"}

            with patch.object(module, "fetch_latest", side_effect=fetch):
                with patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
                    self.assertEqual(module.main(["--source", "api", "--config", str(config), "--write"]), 1)
            self.assertEqual(config.read_text(), concurrent)

    def test_symlink_input_is_rejected_before_discovery(self):
        module = updater()
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text('{"aliases":{"claude-opus-5":"a-1"}}')
            link = config.with_name("link.json")
            link.symlink_to(config)
            with patch.object(module, "fetch_latest") as fetch:
                with patch("sys.stderr", new_callable=io.StringIO):
                    self.assertEqual(module.main(["--source", "api", "--config", str(link), "--write"]), 1)
            fetch.assert_not_called()
            self.assertTrue(link.is_symlink())

    def test_no_concrete_aliases_needs_no_credentials_or_network(self):
        module = updater()
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            text = '{"aliases":{"claude-*":"a-1","gpt-6":"o-1"}}'
            config.write_text(text)
            with patch.object(module, "fetch_latest") as fetch:
                with patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(module.main(["--source", "api", "--config", str(config), "--write"]), 0)
            fetch.assert_not_called()
            self.assertEqual(config.read_text(), text)


class UpdateGPTAliasesTest(unittest.TestCase):
    def test_public_openai_uses_featured_links_not_historical_versions(self):
        module = updater()
        document = ('# Models\n## Featured models\n'
                    '- [GPT Sol](/api/docs/models/gpt-9.1-sol.md): Current\n'
                    '- [GPT Astra](/api/docs/models/gpt-9-astra.md): Current\n'
                    '- [Preview](/api/docs/models/gpt-99-sol-preview.md): Excluded\n'
                    '## Browse our full catalog of models\n'
                    '- [Historical](/api/docs/models/gpt-99-sol.md): Excluded\n')
        requests = []

        def open_document(request, timeout):
            requests.append(request)
            return io.BytesIO(document.encode())

        latest = module.fetch_latest_public("openai", {"sol", "astra"}, open_url=open_document)
        self.assertEqual(latest, {"sol": "gpt-9.1-sol", "astra": "gpt-9-astra"})
        self.assertEqual(requests[0].full_url, "https://developers.openai.com/api/docs/models.md")
        self.assertIsNone(requests[0].get_header("Authorization"))

    def test_default_source_updates_both_providers_without_keys(self):
        module = updater()
        text = '{"aliases":{"claude-opus-5":"a-1","gpt-6-sol":"o-1"}}\n'
        catalogues = {"anthropic": {"opus": "claude-opus-9"}, "openai": {"sol": "gpt-9-sol"}}
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(text)
            with patch.dict(os.environ, {}, clear=True):
                with patch.object(module, "fetch_latest_public", side_effect=lambda provider, lines: catalogues[provider]) as public:
                    with patch.object(module, "fetch_latest") as api:
                        with patch("sys.stdout", new_callable=io.StringIO):
                            self.assertEqual(module.main(["--provider", "all", "--config", str(config), "--write"]), 0)
                        api.assert_not_called()
                    self.assertEqual(public.call_count, 2)
            self.assertEqual(json.loads(config.read_text())["aliases"], {"claude-opus-9": "a-1", "gpt-9-sol": "o-1"})

    def test_public_source_rejects_missing_ambiguous_or_changed_layout(self):
        module = updater()
        documents = [
            ("anthropic", "<html>Login required</html>"),
            ("anthropic", "## Latest models\n| Claude API ID | `claude-sonnet-9` |\n"),
            ("anthropic", "## Latest models\n| Claude API ID | `claude-opus-9` | `claude-opus-10` |\n"),
            ("anthropic", "## Latest models\n| Claude API alias | `claude-opus-9` |\n"),
            ("openai", "## Featured models\n- [Model](/api/docs/models/gpt-9-sol-preview.md): Preview\n"),
            ("openai", "## Featured models\n- [Model](https://example.org/gpt-9-sol.md): Wrong host\n"),
            ("openai", "## Featured models\n- [A](/api/docs/models/gpt-9-sol.md): A\n- [B](/api/docs/models/gpt-10-sol.md): B\n"),
            ("openai", "## Featured models\n## Featured models\n"),
        ]
        for provider, document in documents:
            with self.subTest(provider=provider, document=document):
                def open_document(request, timeout):
                    return io.BytesIO(document.encode())
                with self.assertRaises(module.UpdateError):
                    module.fetch_latest_public(provider, {"sol" if provider == "openai" else "opus"}, open_url=open_document)

    def test_public_failure_does_not_fallback_to_api_or_write_partial_changes(self):
        module = updater()
        text = '{"aliases":{"claude-opus-5":"a-1","gpt-6-sol":"o-1"}}\n'
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(text)
            def public(provider, lines):
                if provider == "openai":
                    raise module.UpdateError("Public catalogue is missing required model lines")
                return {"opus": "claude-opus-9"}
            with patch.object(module, "fetch_latest_public", side_effect=public):
                with patch.object(module, "fetch_latest") as api, patch.object(module, "fetch_latest_openai") as openai_api:
                    with patch("sys.stderr", new_callable=io.StringIO):
                        self.assertEqual(module.main(["--provider", "all", "--config", str(config), "--write"]), 1)
                    api.assert_not_called()
                    openai_api.assert_not_called()
            self.assertEqual(config.read_text(), text)

    def test_public_document_size_limit(self):
        module = updater()
        def open_document(request, timeout):
            return io.BytesIO(b"x" * 1_048_577)
        with self.assertRaisesRegex(module.UpdateError, "size limit"):
            module.fetch_latest_public("anthropic", {"opus"}, open_url=open_document)

    def test_cli_updates_base_models_even_when_alias_names_are_already_current(self):
        module = updater()
        text = ('{"aliases": {"gpt-6.2-sol": "o-1"}, '
                '"model_name_base_model_map": {"a-fallback-vision": "openai/gpt-6.1-sol", '
                '"o-1": "gpt-6.1-sol", "o-0-vision": "gpt-6-astra", '
                '"a-1": "claude-opus-5", "image": "gpt-image-2.5-flare"}, '
                '"other": {"base_model": "gpt-6.1-sol"}}\n')
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(text)
            latest = {"sol": "gpt-6.2-sol", "astra": "gpt-6.2-astra"}
            with patch.object(module, "fetch_latest_public", return_value=latest) as fetch:
                args = ["--provider", "openai", "--config", str(config)]
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(module.main([*args, "--dry-run"]), 0)
                self.assertIn("3 base_model mappings", output.getvalue())
                self.assertEqual(config.read_text(), text)
                with patch("sys.stdout", new_callable=io.StringIO):
                    self.assertEqual(module.main([*args, "--write"]), 0)
                fetch.assert_called_with("openai", {"sol", "astra"})
            expected = text.replace('"openai/gpt-6.1-sol"', '"openai/gpt-6.2-sol"').replace(
                '"o-1": "gpt-6.1-sol"', '"o-1": "gpt-6.2-sol"'
            ).replace('"o-0-vision": "gpt-6-astra"', '"o-0-vision": "gpt-6.2-astra"')
            self.assertEqual(config.read_text(), expected)

    def test_cli_openai_updates_aliases_and_base_models_together(self):
        module = updater()
        text = ('{"aliases": {"gpt-6-sol": "o-1", "anthropic/gpt-6-sol": "a-1", '
                '"claude-opus-5": "a-1", "gpt-*": "o-1", '
                '"gpt-image-2.5-flare": "gpt-image-2.5-flare"}, '
                '"model_name_base_model_map": {"o-1": "gpt-6-sol"}}\n')
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(text)
            with patch.object(module, "fetch_latest_openai", return_value={"sol": "gpt-6.1-sol"}) as fetch:
                with patch.object(module, "fetch_latest") as anthropic:
                    with patch("sys.stdout", new_callable=io.StringIO) as output:
                        self.assertEqual(module.main(["--source", "api", "--provider", "openai", "--config", str(config)]), 0)
                    self.assertEqual(config.read_text(), text)
                    self.assertIn("Dry run: 2 aliases", output.getvalue())
                    with patch("sys.stdout", new_callable=io.StringIO):
                        self.assertEqual(module.main(["--source", "api", "--provider", "openai", "--config", str(config), "--write"]), 0)
                    anthropic.assert_not_called()
                self.assertEqual(fetch.call_args.args[1], {"sol"})
            expected = text.replace('"gpt-6-sol":', '"gpt-6.1-sol":').replace(
                '"anthropic/gpt-6-sol":', '"anthropic/gpt-6.1-sol":').replace(
                '"o-1": "gpt-6-sol"', '"o-1": "gpt-6.1-sol"')
            self.assertEqual(config.read_text(), expected)

    def test_all_mode_writes_both_providers_once_and_is_idempotent(self):
        module = updater()
        text = '{"aliases":{"claude-opus-5":"a-1","gpt-6-sol":"o-1"}}\n'
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(text)
            with patch.object(module, "fetch_latest", return_value={"opus": "claude-opus-5-5"}):
                with patch.object(module, "fetch_latest_openai", return_value={"sol": "gpt-6.1-sol"}):
                    with patch("sys.stdout", new_callable=io.StringIO) as output:
                        self.assertEqual(module.main(["--source", "api", "--provider", "all", "--config", str(config), "--write"]), 0)
                    self.assertIn("Updated 2 aliases", output.getvalue())
                    self.assertEqual(config.read_text(), text.replace('"claude-opus-5":', '"claude-opus-5-5":').replace(
                        '"gpt-6-sol":', '"gpt-6.1-sol":'))
                    before = config.stat().st_mtime_ns
                    with patch("sys.stdout", new_callable=io.StringIO):
                        self.assertEqual(module.main(["--source", "api", "--provider", "all", "--config", str(config), "--write"]), 0)
                    self.assertEqual(config.stat().st_mtime_ns, before)

    def test_all_mode_second_provider_failure_leaves_original_bytes(self):
        module = updater()
        text = '{"aliases":{"claude-opus-5":"a-1","gpt-6-sol":"o-1"}}\n'
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text(text)
            with patch.object(module, "fetch_latest", return_value={"opus": "claude-opus-5-5"}):
                with patch.object(module, "fetch_latest_openai", side_effect=module.UpdateError("Missing GPT line")):
                    with patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
                        self.assertEqual(module.main(["--source", "api", "--provider", "all", "--config", str(config), "--write"]), 1)
            self.assertEqual(config.read_text(), text)

    def test_missing_openai_key_is_reported_without_anthropic_discovery(self):
        module = updater()
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text('{"aliases":{"gpt-6-sol":"o-1"}}')
            with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                with patch("sys.stderr", new_callable=io.StringIO) as output:
                    self.assertEqual(module.main(["--source", "api", "--provider", "openai", "--config", str(config), "--write"]), 1)
            self.assertEqual(output.getvalue().strip(), "Update aborted: OPENAI_API_KEY is required")

    def test_openai_missing_lines_and_malformed_catalogue_fail_closed(self):
        module = updater()
        cases = [
            {"object": "list", "data": []},
            {"object": "list", "data": [None]},
            {"object": "list", "data": [{"id": "gpt-7-sol", "shutdown_date": "invalid"}]},
            {"object": "list", "data": [{"id": "gpt-7-sol"}, {"id": "gpt-7-sol"}]},
            {"object": "list", "data": [{"id": "gpt-7-sol-preview"}]},
            {"data": [{"id": "gpt-7-sol"}]},
        ]
        for page in cases:
            with self.subTest(page=page):
                def open_page(request, timeout):
                    return io.BytesIO(json.dumps(page).encode())
                with self.assertRaises(ValueError):
                    module.fetch_latest_openai("test-key", {"sol"}, open_url=open_page)

    def test_gpt_repository_aliases_and_collision_detection(self):
        module = updater()
        config = SCRIPT.parents[1] / "integrations/llmproxy/litellm/config.json"
        text = config.read_text()
        # Synthetic future catalogue fixture, not a claim of live availability.
        latest = {line: f"gpt-7-{line}" for line in ("astra", "sol", "terra", "luna")}
        self.assertEqual(module.required_lines(text, "openai"), set(latest))
        updated, changes = module.update_aliases(text, latest, "openai")
        self.assertEqual(len(changes), 8)
        before = json.loads(text)
        after = json.loads(updated)
        expected = dict(before)
        renames = dict(changes)
        expected["aliases"] = {renames.get(key, key): value for key, value in before["aliases"].items()}
        self.assertEqual(after, expected)
        for prefix, target in (("", "o"), ("anthropic/", "a")):
            for index, line in enumerate(("astra", "sol", "terra", "luna")):
                self.assertEqual(after["aliases"][prefix + latest[line]], f"{target}-{index}")
        collision = '{"aliases":{"gpt-6-sol":"o-1","gpt-7-sol":"o-2"}}'
        with self.assertRaisesRegex(ValueError, "collision"):
            module.update_aliases(collision, latest, "openai")

    def test_openai_uses_numeric_versions_not_response_order_or_creation_time(self):
        module = updater()
        models = [
            {"id": "gpt-6.9-sol", "created": 9999999999},
            {"id": "gpt-6.10-sol", "created": 1},
            {"id": "gpt-6.10-sol-2026-11-01", "created": 9999999999},
            {"id": "gpt-99-sol-preview", "created": 9999999999},
            {"id": "gpt-image-2.5-flare", "created": 9999999999},
            {"id": "gpt-99-sol", "shutdown_date": "2000-01-01"},
            {"id": "gpt-7-astra", "shutdown_date": None},
        ]
        requests = []

        def open_page(request, timeout):
            requests.append(request)
            self.assertEqual(timeout, 30)
            return io.BytesIO(json.dumps({"object": "list", "data": models}).encode())

        latest = module.fetch_latest_openai("test-key", {"sol", "astra"}, open_url=open_page)
        self.assertEqual(latest, {"sol": "gpt-6.10-sol", "astra": "gpt-7-astra"})
        self.assertEqual(requests[0].full_url, "https://api.openai.com/v1/models")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer test-key")


if __name__ == "__main__":
    unittest.main()
