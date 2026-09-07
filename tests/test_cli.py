"""Generic CLI, resource-writing, and deployment contracts."""

import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from llmproxy.cli import main
from llmproxy.core import resources
from llmproxy.deployment.deploy import content_addressed_config_name


class TestLLMProxyCLI(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self._minimal_application_root(self.root)

    def _console(self, command, **kwargs):
        kwargs.setdefault("env", {**os.environ, "LLMPROXY_ROOT": str(self.root)})
        return subprocess.run(command, **kwargs)

    def test_generic_application_fixture_has_only_synthetic_components(self):
        from llmproxy.deployment.discovery import discover_components

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._minimal_application_root(root)
            self.assertEqual(set(discover_components(root)), {"app/api"})


    def _minimal_application_root(self, root: Path) -> None:
        component = root / "components" / "app" / "api"
        component.mkdir(parents=True)
        (root / "presets").mkdir()
        (component / "compose.yaml").write_text("services:\n  api:\n    image: example\n")
        (root / "presets" / "custom.toml").write_text('components = ["app/api"]\n')
        (root / "presets" / "complete.toml").write_text('extends = "custom"\n')
        (root / "components" / "app" / "compose.yaml").write_text("services: {}\n")


    def test_alternate_root_owns_default_render_output(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as cwd:
            root = Path(tmpdir)
            caller = Path(cwd)
            self._minimal_application_root(root)
            with patch("pathlib.Path.cwd", return_value=caller):
                self.assertEqual(main(["render", "--preset", "custom"], root=root), 0)
            generated = root / "build" / "custom" / "app.yaml"
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
                    ["render", "--preset", "custom"],
                    root=root,
                )
            self.assertEqual(code, 2)
            self.assertIn("symlink", stderr.getvalue())
            self.assertFalse((outside / "custom" / "app.yaml").exists())


    def test_private_writer_rejects_normalized_parent_escape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = base / "root"
            root.mkdir()
            escaped = base / "escaped.txt"
            with self.assertRaisesRegex(resources.ResourceError, "escapes"):
                resources.write_private_text(
                    root / "nested" / ".." / ".." / "escaped.txt",
                    "private",
                    confinement_root=root,
                )
            self.assertFalse(escaped.exists())


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
                        "render",
                        "--preset",
                        "custom",
                        "--output-dir",
                        str(parent / "rendered"),
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
            output = root / "rendered"
            output.mkdir()
            os.mkfifo(output / "app.yaml")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(
                    [
                        "render",
                        "--preset",
                        "custom",
                        "--output-dir",
                        str(output),
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
                        "render",
                        "--preset",
                        "custom",
                        "--output-dir",
                        str(output),
                    ],
                    root=root,
                )
            self.assertEqual(code, 2)
            self.assertIn("error:", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())


    def test_private_writer_replaces_hardlink_without_touching_external_inode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = base / "root"
            root.mkdir()
            external = base / "external.txt"
            external.write_text("external")
            output = root / "output.txt"
            os.link(external, output)

            resources.write_private_text(
                output,
                "generated",
                confinement_root=root,
            )

            self.assertEqual(external.read_text(), "external")
            self.assertEqual(output.read_text(), "generated")
            self.assertNotEqual(external.stat().st_ino, output.stat().st_ino)


    def test_private_writer_does_not_open_final_path_after_parent_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as external:
            base = Path(tmpdir)
            root = base / "root"
            parent = root / "nested"
            parent.mkdir(parents=True)
            outside = Path(external)
            output = parent / "output.txt"
            real_open = resources.os.open
            swapped = False

            def swap_before_path_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if not swapped and dir_fd is None and Path(path) == output:
                    swapped = True
                    parent.rename(root / "original")
                    parent.symlink_to(outside, target_is_directory=True)
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch.object(resources.os, "open", side_effect=swap_before_path_open):
                resources.write_private_text(
                    output,
                    "generated",
                    confinement_root=root,
                )

            self.assertFalse((outside / "output.txt").exists())
            self.assertEqual(output.read_text(), "generated")


    def test_resource_root_prefers_valid_invocation_checkout(self):
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as packaged:
            invocation = Path(cwd)
            packaged_root = Path(packaged)
            self._minimal_application_root(invocation)
            self._minimal_application_root(packaged_root)
            with (
                patch.dict(os.environ, {}, clear=False),
                patch.object(resources.Path, "cwd", return_value=invocation),
                patch.object(resources, "_CHECKOUT_ROOT", invocation / "missing"),
                patch.object(resources, "_PACKAGED_ROOT", packaged_root),
            ):
                os.environ.pop("LLMPROXY_ROOT", None)
                self.assertEqual(resources.resource_root(), invocation.resolve())


    def test_invalid_resource_root_is_a_concise_domain_error(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing"
            with redirect_stderr(stderr):
                code = main(["components"], root=missing)
        self.assertEqual(code, 2)
        self.assertIn("not a valid resource root", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


    def test_structurally_incomplete_roots_are_concise_domain_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            for missing in ("components", "presets"):
                root = base / missing
                root.mkdir()
                for name in ("components", "presets"):
                    if name != missing:
                        (root / name).mkdir()
                with self.subTest(missing=missing), redirect_stderr(io.StringIO()) as stderr:
                    code = main(["components"], root=root)
                self.assertEqual(code, 2)
                self.assertIn("not a valid resource root", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
            empty = base / "empty"
            empty.mkdir()
            with redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(["components"], root=empty), 2)
            self.assertIn("not a valid resource root", stderr.getvalue())


    def test_cli_help(self):
        res = self._console(
            ["uv", "run", "llmproxy", "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("llmproxy", res.stdout.lower())
        self.assertIn("stack", res.stdout)
        self.assertIn("components", res.stdout)
        self.assertIn("deploy", res.stdout)


    def test_components_command_lists_component_ids(self):
        res = self._console(
            ["uv", "run", "llmproxy", "components"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        from llmproxy.deployment.discovery import discover_components

        for identifier in discover_components(self.root):
            self.assertIn(identifier, res.stdout)


    def test_env_parse_error_has_concise_exit_two_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("BROKEN LINE\n")
            result = self._console(
                [
                    "uv", "run", "llmproxy", "deploy",
                    "--preset", "custom",
                    "--env-file", str(env_file),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


    def test_unknown_preset_has_concise_exit_two_without_traceback(self):
        result = self._console(
            ["uv", "run", "llmproxy", "render", "--preset", "missing-preset"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


    def test_top_level_presets_shortcut(self):
        res = self._console(
            ["uv", "run", "llmproxy", "presets"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("custom:", res.stdout)
        self.assertIn("complete:", res.stdout)


    def test_top_level_validate_shortcut(self):
        res = self._console(
            ["uv", "run", "llmproxy", "validate", "--preset", "custom"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Preset 'custom' is valid", res.stdout)


    def test_top_level_deploy_docker_dry_run(self):
        res = self._console(
            [
                "uv", "run", "llmproxy", "deploy",
                "--preset", "custom",
                "--driver", "docker",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("docker stack deploy", res.stdout)


    def test_top_level_deploy_ptctools_dry_run(self):
        component = self.root / "components" / "app" / "api"
        content = b"test configuration\n"
        (component / "settings.txt").write_bytes(content)
        (component / "component.toml").write_text(
            '[[docker_configs]]\nsource = "settings.txt"\n'
            'name = "app_settings"\nenvironment = "APP_CONFIG_NAME"\n'
        )
        res = self._console(
            [
                "uv", "run", "llmproxy", "deploy",
                "--preset", "custom",
                "--driver", "ptctools",
                "--allow-remove-services",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("uvx ptctools docker config set", res.stdout)
        self.assertIn(content_addressed_config_name("app_settings", content), res.stdout)
        self.assertIn("uvx ptctools docker stack deploy", res.stdout)



if __name__ == "__main__":
    unittest.main()