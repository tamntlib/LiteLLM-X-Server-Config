import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llmproxy.core import resources
from llmproxy.core.resources import is_public_resource
from llmproxy.deployment import render_preset
from llmproxy.deployment.compose import ROOT, resolve_components, selected_stack_names
from llmproxy.deployment.deploy import deploy_preset
from llmproxy.deployment.discovery import discover_components
from llmproxy.deployment.preset import list_presets, resolve_preset


EXISTING_PRESETS = {
    "all": {
        "components": (
            "llmproxy-data/postgres",
            "llmproxy/litellm",
            "llmproxy/cli-proxy-api",
            "llmproxy/cli-proxy-api-usage",
            "llmproxy/headroom",
            "monitoring/netdata",
        ),
        "features": ("traefik", "monitoring"),
        "stacks": ("monitoring", "llmproxy-data", "llmproxy"),
        "hashes": {
            "monitoring": "0a201cbee81da15d4e0c4b30bc5574a7dcf8298011bfaed0a64c500bef5e9183",
            "llmproxy-data": "ca146c25f981697f0d5447a86880b6d9a50a250d0b4bf5ab622ebecf3693a949",
            "llmproxy": "93e248ed0b849d1a60e0f9b8bb77630d94181b9a08e47aa53d19ef950af97cc9",
        },
    },
    "litellm-only": {
        "components": ("llmproxy-data/postgres", "llmproxy/litellm"),
        "features": (),
        "stacks": ("llmproxy-data", "llmproxy"),
        "hashes": {
            "llmproxy-data": "00d00a6dfad857f984b9b096ff994da367e858818c28bcebefd8aaf32c102eaf",
            "llmproxy": "92e84c1fd3dfc6609d2dc677f5457c09ddc7df93fd258a95ae9d0bdc42546447",
        },
    },
    "litellm-standalone": {
        "components": ("llmproxy-data/postgres", "llmproxy/litellm"),
        "features": ("publish",),
        "stacks": ("llmproxy-data", "llmproxy"),
        "hashes": {
            "llmproxy-data": "00d00a6dfad857f984b9b096ff994da367e858818c28bcebefd8aaf32c102eaf",
            "llmproxy": "4af9a62aad3abd07f5048bba8b4f2e7fe142d1f4cb9aaf02665ec161846f43e8",
        },
    },
    "litellm-traefik": {
        "components": ("llmproxy-data/postgres", "llmproxy/litellm"),
        "features": ("traefik",),
        "stacks": ("llmproxy-data", "llmproxy"),
        "hashes": {
            "llmproxy-data": "00d00a6dfad857f984b9b096ff994da367e858818c28bcebefd8aaf32c102eaf",
            "llmproxy": "dfb76daf2b06f99e26691de4ccaadbba1d8aec2bf5936204cc5f49f704b39306",
        },
    },
    "llmproxy": {
        "components": (
            "llmproxy-data/postgres",
            "llmproxy/litellm",
            "llmproxy/cli-proxy-api",
            "llmproxy/cli-proxy-api-usage",
            "llmproxy/headroom",
        ),
        "features": ("traefik",),
        "stacks": ("llmproxy-data", "llmproxy"),
        "hashes": {
            "llmproxy-data": "00d00a6dfad857f984b9b096ff994da367e858818c28bcebefd8aaf32c102eaf",
            "llmproxy": "a3ff52dc246c94d4acdb9ca1a0d3855c7fd016a6ddd3aea679e381a91a0e5fb6",
        },
    },
}

# Public-only render snapshots; monitoring now retains its top-level extension
# without the empty stack base (runtime parity is covered in test_stack_render).
ALL_PUBLIC_PRESETS = {
    **EXISTING_PRESETS,
    "default": {
        "components": (
            "llmproxy-data/postgres",
            "llmproxy/litellm",
            "llmproxy/cli-proxy-api",
            "llmproxy/cli-proxy-api-usage",
            "monitoring/netdata",
        ),
        "features": ("traefik", "monitoring"),
        "stacks": ("monitoring", "llmproxy-data", "llmproxy"),
        "hashes": {
            "monitoring": "0a201cbee81da15d4e0c4b30bc5574a7dcf8298011bfaed0a64c500bef5e9183",
            "llmproxy-data": "ca146c25f981697f0d5447a86880b6d9a50a250d0b4bf5ab622ebecf3693a949",
            "llmproxy": "9a6c59facc86a7d15809f3f706fe3c6ac4e19c87a866d61b2ff3e4510f86977b",
        },
    },
    "portainer": {
        "components": ("portainer/portainer",),
        "features": (),
        "stacks": ("portainer",),
        "hashes": {
            "portainer": "50e9dbe4f00145d7fba13a030cd2fdd9fe5aa8543c08ae117f7e7ce74b22494a",
        },
    },
}

PORTAINER_ENV = {
    "PORTAINER_HOST": "portainer.invalid",
    "CF_DNS_API_TOKEN": "deterministic-placeholder",
    "LETSENCRYPT_EMAIL": "operator@example.invalid",
    "TRAEFIK_ROUTER_ENTRYPOINTS": "websecure",
    "TRAEFIK_ROUTER_TLS": "true",
    "LETSENCRYPT_RESOLVER": "le",
}


def normalized_stack(compose_files: list[Path], env: dict[str, str]) -> str:
    command = ["docker", "stack", "config"]
    for compose_file in compose_files:
        command.extend(["-c", str(compose_file)])
    process_env = {
        "PATH": os.environ.get("PATH", os.defpath),
        **env,
    }
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=process_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout


class PortainerMigrationTest(unittest.TestCase):
    def test_portainer_is_one_component_with_the_complete_service_set(self):
        component = discover_components(ROOT)["portainer/portainer"]

        self.assertEqual(component.stack, "portainer")
        self.assertEqual(component.name, "portainer")
        self.assertEqual(component.services, ("traefik", "agent", "portainer"))

    def test_portainer_preset_selects_only_the_portainer_component_and_stack(self):
        preset = resolve_preset("portainer", ROOT)

        self.assertEqual(preset.components, ("portainer/portainer",))
        self.assertEqual(preset.features, ())
        self.assertEqual(selected_stack_names("portainer", root=ROOT), ("portainer",))
        self.assertNotIn("portainer/portainer", resolve_preset("all", ROOT).components)

    def test_all_seven_public_presets_preserve_topology_order_and_rendered_bytes(self):
        self.assertEqual({preset.name for preset in list_presets(ROOT)}, set(ALL_PUBLIC_PRESETS))
        for name, expected in ALL_PUBLIC_PRESETS.items():
            with self.subTest(preset=name), tempfile.TemporaryDirectory() as tmpdir:
                preset = resolve_preset(name, ROOT)
                outputs = render_preset(
                    name,
                    Path(tmpdir),
                    include_local_overrides=False,
                    root=ROOT,
                )
                hashes = {
                    stack: hashlib.sha256(path.read_bytes()).hexdigest()
                    for stack, path in outputs.items()
                }

                self.assertEqual(preset.components, expected["components"])
                self.assertEqual(preset.features, expected["features"])
                self.assertEqual(selected_stack_names(name, root=ROOT), expected["stacks"])
                self.assertEqual(tuple(outputs), expected["stacks"])
                self.assertEqual(
                    [path.name for path in outputs.values()],
                    [f"{stack}.yaml" for stack in expected["stacks"]],
                )
                self.assertEqual(hashes, expected["hashes"])

    def test_portainer_render_matches_legacy_normalized_snapshot(self):
        # Captured from: git show HEAD:portainer/portainer.yaml | docker stack config -c -
        legacy_normalized_sha256 = (
            "993c888a8af1c679f73eeb056234e04f71bcbc0e714f35345369941cc9ff29b6"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            rendered = render_preset(
                "portainer",
                directory / "rendered",
                include_local_overrides=False,
                env=PORTAINER_ENV,
                root=ROOT,
            )["portainer"]

            self.assertEqual(
                hashlib.sha256(
                    normalized_stack([rendered], PORTAINER_ENV).encode()
                ).hexdigest(),
                legacy_normalized_sha256,
            )

    def test_portainer_required_environment_is_scoped_to_its_preset(self):
        component = discover_components(ROOT)["portainer/portainer"]

        self.assertEqual(
            set(component.required_environment),
            {"PORTAINER_HOST", "CF_DNS_API_TOKEN", "LETSENCRYPT_EMAIL"},
        )
        existing_required = {
            name
            for preset in EXISTING_PRESETS
            for selected in resolve_components(preset, ROOT)
            for name in selected.required_environment
        }
        self.assertTrue(set(component.required_environment).isdisjoint(existing_required))

    def test_portainer_dry_run_has_exactly_one_non_mutating_deploy_command(self):
        with patch("llmproxy.deployment.deploy._run_command") as run_command:
            commands = deploy_preset(
                "portainer",
                driver="docker",
                dry_run=True,
                env=PORTAINER_ENV,
                root=ROOT,
            )

        run_command.assert_not_called()
        self.assertEqual(len(commands), 1)
        self.assertRegex(commands[0], r"^docker stack deploy -c .+/portainer\.yaml portainer$")

    def test_portainer_is_not_special_cased_in_python_or_a_central_registry(self):
        python_sources = "\n".join(
            path.read_text()
            for path in sorted((ROOT / "llmproxy").rglob("*.py"))
        )

        self.assertNotIn("portainer/portainer", python_sources)
        self.assertNotIn('"portainer"', python_sources.lower())
        self.assertNotIn("llmproxy/headroom", python_sources)
        self.assertNotIn('"default"', python_sources)
        self.assertFalse((ROOT / "deploy" / "registry.json").exists())

    def test_public_packaging_selection_includes_portainer_and_excludes_private_files(self):
        packaged = {
            path.relative_to(ROOT).as_posix()
            for root_name in ("components", "presets")
            for path in (ROOT / root_name).rglob("*")
            if is_public_resource(path)
        }

        self.assertTrue(
            {
                "components/portainer/portainer/component.toml",
                "components/portainer/portainer/compose.yaml",
                "components/portainer/stack.toml",
                "presets/default.toml",
                "presets/portainer.toml",
            }.issubset(packaged)
        )
        self.assertIn("components/llmproxy/litellm/compose.local.example.yaml", packaged)
        for path in map(Path, packaged):
            self.assertFalse(any(".local" in part for part in path.parts[:-1]))
            if ".local" in path.name:
                self.assertEqual(path.name, "compose.local.example.yaml")
        self.assertFalse(any(part.startswith(".env") for path in packaged for part in Path(path).parts))

    def test_packaged_resource_fallback_discovers_seven_presets_and_seven_components(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            packaged_root = directory / "site-packages" / "llmproxy" / "resources"
            for root_name in ("components", "presets"):
                for source in (ROOT / root_name).rglob("*"):
                    if not is_public_resource(source):
                        continue
                    destination = packaged_root / source.relative_to(ROOT)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
            caller = directory / "caller"
            caller.mkdir()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(resources.Path, "cwd", return_value=caller),
                patch.object(resources, "_CHECKOUT_ROOT", directory / "missing"),
                patch.object(resources, "_PACKAGED_ROOT", packaged_root),
            ):
                selected_root = resources.resource_root()

            self.assertEqual(selected_root, packaged_root)
            self.assertEqual(len(discover_components(selected_root)), 7)
            self.assertEqual(len(list_presets(selected_root)), 7)
            self.assertEqual(
                resolve_preset("portainer", selected_root).components,
                ("portainer/portainer",),
            )
            self.assertEqual(
                resolve_preset("all", selected_root).components,
                EXISTING_PRESETS["all"]["components"],
            )
            self.assertEqual(
                resolve_preset("default", selected_root).components,
                tuple(
                    component
                    for component in EXISTING_PRESETS["all"]["components"]
                    if component != "llmproxy/headroom"
                ),
            )

    def test_root_env_example_owns_the_sanitized_portainer_contract(self):
        text = (ROOT / ".env.example").read_text()
        assignments = re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", text, re.MULTILINE)
        by_name = {}
        for name, value in assignments:
            self.assertNotIn(name, by_name)
            by_name[name] = value

        self.assertEqual(by_name["PORTAINER_HOST"], "portainer.example.com")
        self.assertEqual(by_name["CF_DNS_API_TOKEN"], "")
        self.assertEqual(by_name["LETSENCRYPT_EMAIL"], "")
        self.assertEqual(by_name["TRAEFIK_ROUTER_ENTRYPOINTS"], "websecure")
        self.assertEqual(by_name["TRAEFIK_ROUTER_TLS"], "true")
        self.assertEqual(by_name["LETSENCRYPT_RESOLVER"], "le")
        self.assertFalse((ROOT / "portainer" / ".env.example").exists())


if __name__ == "__main__":
    unittest.main()
