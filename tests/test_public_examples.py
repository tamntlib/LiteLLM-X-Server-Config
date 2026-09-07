"""Public local-override examples must not expose private overrides."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from llmproxy.core.resources import is_public_resource
from llmproxy.deployment.compose import compose_files_for_preset


ROOT = Path(__file__).resolve().parents[1]


class PublicExampleTest(unittest.TestCase):
    def test_example_is_not_merged_until_copied_to_local_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component = root / "components" / "app" / "api"
            component.mkdir(parents=True)
            (root / "presets").mkdir()
            (root / "presets" / "app.toml").write_text('components = ["app/api"]\n')
            compose = component / "compose.yaml"
            compose.write_text("services:\n  api:\n    image: example/api\n")
            example = component / "compose.local.example.yaml"
            example.write_text("services:\n  api:\n    image: example/local\n")
            self.assertEqual(compose_files_for_preset("app", root=root), {"app": [compose]})
            local = component / "compose.local.yaml"
            shutil.copyfile(example, local)
            self.assertEqual(compose_files_for_preset("app", root=root), {"app": [compose, local]})
            self.assertEqual(
                compose_files_for_preset("app", root=root, include_local_overrides=False),
                {"app": [compose]},
            )

    def test_git_tracks_compose_example_but_ignores_private_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            shutil.copyfile(ROOT / ".gitignore", root / ".gitignore")
            subprocess.run(
                ["git", "init", "--quiet", str(root)], check=True, capture_output=True,
            )
            component = "components/llmproxy/litellm"
            cases = {
                f"{component}/compose.local.example.yaml": False,
                f"{component}/compose.local.yaml": True,
                f"{component}/compose.local.example.yaml.bak": True,
                f"{component}/integrations/llmproxy/litellm/config.local.json": True,
                f"{component}/config.local.example.json": True,
                f"{component}/private.local/compose.local.example.yaml": True,
                f"{component}/compose.local.example.yaml/payload": True,
                ".env": True,
            }
            for name, ignored in cases.items():
                with self.subTest(name=name):
                    result = subprocess.run(
                        ["git", "-c", "core.excludesFile=/dev/null", "check-ignore",
                         "--no-index", "--quiet", name],
                        cwd=root, capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 0 if ignored else 1, result.stderr)

    def test_public_resource_example_exception_is_leaf_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cases = {
                "compose.local.example.yaml": True,
                "compose.local.yaml": False,
                "compose.local.example.yaml.bak": False,
                "config.local.example.json": False,
                "private.local/compose.local.example.yaml": False,
                "compose.local.example.yaml/payload": False,
                ".env.d/compose.local.example.yaml": False,
            }
            for index, (name, public) in enumerate(cases.items()):
                with self.subTest(name=name):
                    # Separate roots allow testing a name as both file and parent directory.
                    case = root / str(index)
                    path = case / name
                    path.parent.mkdir(parents=True)
                    path.write_text("synthetic example\n")
                    self.assertEqual(is_public_resource(path), public)
