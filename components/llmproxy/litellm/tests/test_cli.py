"""LiteLLM CLI contracts, discovered through the component module loader."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from llmproxy.cli import create_parser, main
from llmproxy.core import resources
from llmproxy.core.component_modules import load_component_module

CONFIG_COMMAND = ["llmproxy/litellm", "config"]
LITELLM_PATH = Path("components/llmproxy/litellm")
CONFIG_OUTPUT = Path("build/llmproxy/litellm/config.gen.json")


class TestLiteLLMCLI(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._minimal_application_root(self.root)

    def _console(self, command, **kwargs):
        kwargs.setdefault("env", {**os.environ, "LLMPROXY_ROOT": str(self.root)})
        return subprocess.run(command, **kwargs)

    def test_config_commands_are_owned_only_by_litellm_component(self):
        from llmproxy.core.command import discover_commands

        commands = {spec.path: spec for spec in discover_commands(self.root)}
        for action in ("generate", "sync"):
            with self.subTest(action=action):
                path = ("llmproxy/litellm", "config", action)
                self.assertIn(path, commands)
                spec = commands[path]
                self.assertEqual(spec.component_identifier, "llmproxy/litellm")
                self.assertTrue(spec.lazy)
                self.assertEqual(spec.mutating, action == "sync")
                self.assertIn("components/llmproxy/litellm/commands/", spec.source)
                self.assertNotIn(("config", action), commands)


    def test_component_config_sync_preserves_flags_and_application_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._minimal_application_root(root)
            module = load_component_module(root, "llmproxy/litellm", "src.config_sync")
            with patch.object(module, "sync_config", return_value=2) as sync:
                code = main(
                    [*CONFIG_COMMAND, "sync", "--preset", "custom", "--only", "models",
                     "--force", "--prune", "--dry-run"],
                    root=root,
                )
            self.assertEqual(code, 2)
            sync.assert_awaited_once_with(
                config_path=None,
                preset="custom",
                only="models",
                force=True,
                prune=True,
                dry_run=True,
                root=root,
            )


    def _install_config_commands(self, root: Path) -> None:
        component = root / LITELLM_PATH
        commands = component / "commands"
        commands.mkdir(parents=True)
        (component / "compose.yaml").write_text("services:\n  litellm:\n    image: example\n")
        source = Path(__file__).resolve().parents[1]
        for name in ("config_generate.py", "config_sync.py", "key_limits.py", "create_key.py"):
            shutil.copyfile(source / "commands" / name, commands / name)
        (component / "src").mkdir()
        for path in (source / "src").glob("*.py"):
            shutil.copyfile(path, component / "src" / path.name)
        (component / "configs").mkdir()
        shutil.copyfile(
            source / "configs" / "config.json",
            component / "configs" / "config.json",
        )


    def _minimal_application_root(self, root: Path) -> None:
        self._install_config_commands(root)
        component = root / "components" / "app" / "api"
        component.mkdir(parents=True)
        (root / "presets").mkdir()
        (component / "compose.yaml").write_text(
            "services:\n  api:\n    image: example\n"
        )
        (root / "presets" / "custom.toml").write_text(
            'components = ["app/api"]\n'
        )
        (root / "components" / "app" / "compose.yaml").write_text(
            "services: {}\n"
        )
        (root / LITELLM_PATH / "configs" / "config.json").write_text(
            '{"providers": {}, "models": []}\n'
        )


    def test_alternate_root_owns_default_config_output(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as cwd:
            root = Path(tmpdir)
            caller = Path(cwd)
            self._minimal_application_root(root)
            with patch("pathlib.Path.cwd", return_value=caller):
                self.assertEqual(
                    main([*CONFIG_COMMAND, "generate", "--preset", "custom"], root=root),
                    0,
                )

            generated = root / CONFIG_OUTPUT
            self.assertTrue(generated.is_file())
            self.assertEqual(generated.stat().st_mode & 0o777, 0o600)
            self.assertFalse((caller / "build").exists())


    def test_default_outputs_reject_symlinked_build_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as external:
            root = Path(tmpdir)
            outside = Path(external)
            self._minimal_application_root(root)
            (root / "build").symlink_to(outside, target_is_directory=True)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [*CONFIG_COMMAND, "generate", "--preset", "custom"],
                    root=root,
                )
            self.assertEqual(code, 2)
            self.assertIn("symlink", stderr.getvalue())
            self.assertFalse((outside / "llmproxy" / "litellm" / "config.gen.json").exists())


    def test_output_parent_regular_file_is_a_concise_domain_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._minimal_application_root(root)
            parent = root / "not-a-directory"
            parent.write_text("regular file")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        *CONFIG_COMMAND,
                        "generate",
                        "--preset",
                        "custom",
                        "--output",
                        str(parent / "config.json"),
                    ],
                    root=root,
                )
            self.assertEqual(code, 2)
            self.assertIn("error:", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


    def test_non_regular_output_target_is_a_concise_domain_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._minimal_application_root(root)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        *CONFIG_COMMAND,
                        "generate",
                        "--preset",
                        "custom",
                        "--output",
                        "/dev/null",
                    ],
                    root=root,
                )
            self.assertEqual(code, 2)
            self.assertIn("regular file", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


    def test_descriptor_lifecycle_error_is_a_concise_domain_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._minimal_application_root(root)
            output = root / "output.json"
            stderr = io.StringIO()
            with (
                patch.object(resources.os, "fchmod", side_effect=OSError("denied")),
                redirect_stderr(stderr),
            ):
                code = main(
                    [
                        *CONFIG_COMMAND,
                        "generate",
                        "--preset",
                        "custom",
                        "--output",
                        str(output),
                    ],
                    root=root,
                )
            self.assertEqual(code, 2)
            self.assertIn("error:", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


    def test_config_sync_directory_is_a_concise_domain_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            self._minimal_application_root(config_dir)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        *CONFIG_COMMAND,
                        "sync",
                        "--dry-run",
                        "--config",
                        str(config_dir),
                    ],
                    root=config_dir,
                )
        self.assertEqual(code, 2)
        self.assertIn("Config file not found", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


    def test_config_read_permission_error_is_concise(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._minimal_application_root(root)
            stderr = io.StringIO()
            with (
                patch(
                    load_component_module(root, "llmproxy/litellm", "src.config_generate").__name__ + ".load_json",
                    side_effect=PermissionError("denied"),
                ),
                redirect_stderr(stderr),
            ):
                code = main(
                    [*CONFIG_COMMAND, "generate", "--preset", "custom"],
                    root=root,
                )

        self.assertEqual(code, 2)
        self.assertIn("error: denied", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


    def test_config_read_generic_os_error_is_concise(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._minimal_application_root(root)
            stderr = io.StringIO()
            with (
                patch(
                    load_component_module(root, "llmproxy/litellm", "src.config_generate").__name__ + ".load_json",
                    side_effect=OSError("read failed"),
                ),
                redirect_stderr(stderr),
            ):
                code = main(
                    [*CONFIG_COMMAND, "generate", "--preset", "custom"],
                    root=root,
                )

        self.assertEqual(code, 2)
        self.assertIn("error: read failed", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


    def test_missing_component_config_is_a_concise_domain_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "empty.toml").write_text("components = []\n")
            self._install_config_commands(root)
            (root / LITELLM_PATH / "configs" / "config.json").unlink()
            with redirect_stderr(io.StringIO()) as stderr:
                code = main([*CONFIG_COMMAND, "generate", "--preset", "empty"], root=root)
            self.assertEqual(code, 2)
            self.assertIn("config file not found", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


    def test_config_generate_uses_selected_application_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._install_config_commands(root)
            component = root / "components" / "app" / "api"
            component.mkdir(parents=True)
            (root / "presets").mkdir()
            (component / "compose.yaml").write_text(
                "services:\n  api:\n    image: example\n"
            )
            (root / "presets" / "custom.toml").write_text(
                'components = ["app/api"]\n'
            )
            (root / "components" / "app" / "compose.yaml").write_text("services: {}\n")
            (root / LITELLM_PATH / "configs" / "config.json").write_text(
                '{"providers": {}, "router_settings": {"timeout": 123}}\n'
            )
            output = root / "generated.json"
            self.assertEqual(
                main(
                    [
                        *CONFIG_COMMAND,
                        "generate",
                        "--preset",
                        "custom",
                        "--output",
                        str(output),
                    ],
                    root=root,
                ),
                0,
            )
            self.assertEqual(
                json.loads(output.read_text())["router_settings"]["timeout"],
                123,
            )


    def test_cli_help(self):
        res = self._console(
            ["uv", "run", "llmproxy", "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("llmproxy", res.stdout.lower())
        self.assertIn("stack", res.stdout)
        self.assertIn("llmproxy/litellm", res.stdout)
        self.assertIn("key", res.stdout)
        self.assertIn("deploy", res.stdout)


    def test_component_command_has_canonical_path_and_global_alias(self):
        canonical = self._console(
            ["uv", "run", "llmproxy", "llmproxy/litellm", "key-limits", "--help"],
            capture_output=True,
            text=True,
        )
        alias = self._console(
            ["uv", "run", "llmproxy", "key", "limits", "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(canonical.returncode, 0)
        self.assertEqual(alias.returncode, 0)
        self.assertIn("--apply", canonical.stdout)
        self.assertIn("--apply", alias.stdout)


    def test_key_limits_apply_flag_is_wired_to_component_command(self):
        args = create_parser(self.root).parse_args(["key", "limits", "--apply"])
        self.assertTrue(args.apply)
        self.assertEqual(args._command_spec.component_identifier, "llmproxy/litellm")


    def test_unknown_preset_has_concise_exit_two_without_traceback(self):
        for command in (
            [
                "uv",
                "run",
                "llmproxy",
                *CONFIG_COMMAND,
                "generate",
                "--preset",
                "missing-preset",
            ],
        ):
            with self.subTest(command=command):
                result = self._console(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn("error:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)


    def test_config_generate_default_output_is_in_ignored_build_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._minimal_application_root(root)
            output = root / CONFIG_OUTPUT
            result = self._console(
                ["uv", "run", "llmproxy", *CONFIG_COMMAND, "generate", "--preset", "custom"],
                capture_output=True,
                text=True,
                env={**os.environ, "LLMPROXY_ROOT": str(root)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.is_file())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        if Path(".git").exists():
            ignored = self._console(
                ["git", "check-ignore", "--no-index", "-q", str(CONFIG_OUTPUT)],
                capture_output=True,
            )
            self.assertEqual(ignored.returncode, 0)


    def test_config_commands_require_explicit_source_selection(self):
        for command in (
            ["uv", "run", "llmproxy", *CONFIG_COMMAND, "generate"],
            ["uv", "run", "llmproxy", *CONFIG_COMMAND, "sync", "--dry-run"],
        ):
            result = self._console(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("one of the arguments", result.stderr)


    def test_old_top_level_config_namespace_is_rejected_by_console_script(self):
        for action in ("generate", "sync"):
            with self.subTest(action=action):
                result = self._console(
                    ["uv", "run", "llmproxy", "config", action, "--help"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("invalid choice: 'config'", result.stderr)


    def test_component_config_help_exposes_source_and_sync_flags(self):
        for action in ("generate", "sync"):
            with self.subTest(action=action):
                result = self._console(
                    ["uv", "run", "llmproxy", *CONFIG_COMMAND, action, "--help"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("llmproxy/litellm config " + action, result.stdout)
                self.assertIn("--preset", result.stdout)
                self.assertIn("--config", result.stdout)
                for flag in (("--only", "--force", "--prune", "--dry-run") if action == "sync" else ("--output",)):
                    self.assertIn(flag, result.stdout)


    def test_console_config_generate_excludes_headroom_and_round_trips(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._minimal_application_root(root)
            headroom = root / "components" / "llmproxy" / "headroom"
            headroom.mkdir()
            (headroom / "compose.yaml").write_text("services:\n  headroom:\n    image: example\n")
            integration = headroom / "integrations" / "llmproxy" / "litellm"
            integration.mkdir(parents=True)
            (integration / "config.json").write_text(json.dumps({
                "guardrails": {"headroom-compression": {"litellm_params": {
                    "guardrail": "headroom", "mode": "pre_call", "default_on": True,
                }}},
            }))
            (root / "presets" / "all.toml").write_text(
                'components = ["llmproxy/litellm", "llmproxy/headroom"]\n'
            )
            (root / "presets" / "default.toml").write_text(
                'extends = "all"\nexclude_components = ["llmproxy/headroom"]\n'
            )
            env = {**os.environ, "LLMPROXY_ROOT": str(root)}
            generated = {}
            for preset in ("all", "default"):
                output = root / (preset + ".json")
                result = self._console(
                    ["uv", "run", "llmproxy", *CONFIG_COMMAND, "generate",
                     "--preset", preset, "--output", str(output)],
                    capture_output=True, text=True, env=env,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                generated[preset] = json.loads(output.read_text())
            self.assertEqual(generated["all"]["guardrails"][0]["guardrail_name"], "headroom-compression")
            self.assertNotIn("headroom", json.dumps(generated["default"]).lower())
            result = self._console(
                ["uv", "run", "llmproxy", *CONFIG_COMMAND, "sync", "--dry-run",
                 "--only", "models", "--config", str(root / "default.json")],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no API changes", result.stderr)



if __name__ == "__main__":
    unittest.main()