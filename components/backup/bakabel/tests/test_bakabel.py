"""The backup stack is an explicit, independent, env-driven opt-in."""

from pathlib import Path
import os
import re
import tempfile
import unittest

from llmproxy.deployment.compose import compose_files_for_preset, render_preset, selected_stack_names
from llmproxy.deployment.deploy import DeploymentError, preflight_preset
from llmproxy.deployment.discovery import discover_components
from llmproxy.deployment.preset import list_presets, resolve_preset


ROOT = Path(__file__).resolve().parents[4]
COMPONENT = ROOT / "components/backup/bakabel"
PINNED_IMAGE = (
    "ghcr.io/tamntlib/bakabel@sha256:"
    "c80fe490b5ad3f0b857ce442d92d41589e172258f1696a90d97c98095da07323"
)


class BakabelTest(unittest.TestCase):
    def test_discovery_requires_explicit_independent_preset(self):
        components = discover_components(ROOT)
        self.assertIn("backup/bakabel", components)
        component = components["backup/bakabel"]
        self.assertEqual(component.services, ("bakabel",))
        self.assertEqual(component.requires, ())
        self.assertEqual(component.after, ())
        self.assertEqual(component.required_features, ())
        self.assertEqual(component.features, ())
        self.assertEqual(component.commands, ())
        self.assertEqual(component.required_files, ())
        self.assertEqual(component.docker_configs, ())
        self.assertEqual(
            component.required_environment,
            ("BAKABEL_RESTIC_PASSWORD",),
        )
        preset = resolve_preset("backup", ROOT)
        self.assertEqual(preset.components, ("backup/bakabel",))
        self.assertIsNone(preset.extends)
        self.assertEqual(preset.features, ())
        self.assertEqual(selected_stack_names("backup", root=ROOT), ("backup",))
        self.assertEqual(
            compose_files_for_preset("backup", root=ROOT),
            {"backup": [COMPONENT / "compose.yaml"]},
        )
        for other in list_presets(ROOT):
            if other.name != "backup":
                self.assertNotIn("backup/bakabel", resolve_preset(other.name, ROOT).components)

    def test_swarm_runtime_contract_uses_env_config_and_named_volumes(self):
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "DOCKER_HOST": "unix:///nonexistent/bakabel-component-test.sock",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            outputs = render_preset(
                "backup",
                Path(tmpdir),
                stack="backup",
                root=ROOT,
                include_local_overrides=False,
                env=env,
            )
            self.assertEqual(tuple(outputs), ("backup",))
            text = outputs["backup"].read_text()

        self.assertEqual(re.findall(r"^  ([\w-]+):$", text, re.M), ["bakabel"])
        self.assertEqual(re.findall(r"^    image: (.+)$", text, re.M), [PINNED_IMAGE])
        for expected in (
            "BAKABEL_REPOSITORY: ${BAKABEL_REPOSITORY:-/repository}",
            "BAKABEL_SCHEDULE: ${BAKABEL_SCHEDULE:-0 2 * * *}",
            "BAKABEL_TZ: ${BAKABEL_TZ:-Asia/Ho_Chi_Minh}",
            "BAKABEL_AUTO_INIT: ${BAKABEL_AUTO_INIT:-true}",
            "RESTIC_PASSWORD: ${BAKABEL_RESTIC_PASSWORD:?set BAKABEL_RESTIC_PASSWORD in .env}",
            "stop_grace_period: 30s",
            "        constraints:\n          - node.role == manager\n",
            "        source: bakabel-state\n        target: /var/lib/bakabel",
            "        source: bakabel-repo\n        target: /repository",
            "        source: /var/run/docker.sock\n        target: /var/run/docker.sock\n        read_only: true",
            "  bakabel-repo: {}",
            "  bakabel-state: {}",
        ):
            self.assertIn(expected, text)
        for forbidden in ("ports:", "networks:", "configs:", "secrets:", "depends_on:", ":latest", "bakabel.yaml"):
            self.assertNotIn(forbidden, text)
        self.assertFalse((COMPONENT / "configs").exists())

    def test_preflight_requires_restic_password(self):
        env = {
            "BAKABEL_RESTIC_PASSWORD": "synthetic-test-passphrase",
        }
        preflight_preset("backup", env, stack="backup", root=ROOT, include_local_overrides=False)
        with self.assertRaisesRegex(DeploymentError, "BAKABEL_RESTIC_PASSWORD"):
            preflight_preset(
                "backup",
                {},
                stack="backup",
                root=ROOT,
                include_local_overrides=False,
            )

    def test_opt_in_components_declare_bakabel_labels(self):
        env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "DOCKER_HOST": "unix:///nonexistent/bakabel-component-test.sock",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            outputs = render_preset(
                "default",
                Path(tmpdir),
                root=ROOT,
                include_local_overrides=False,
                env=env,
            )
            pg_text = outputs["llmproxy-data"].read_text()
            app_text = outputs["llmproxy"].read_text()
        self.assertIn("      labels:\n        bakabel.type: postgres\n", pg_text)
        self.assertIn("      labels:\n        bakabel.paths: /data/app.db\n        bakabel.type: sqlite\n", app_text)
