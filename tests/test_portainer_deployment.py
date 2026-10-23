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
from llmproxy.deployment.compose import ROOT, DeploymentError, resolve_components, selected_stack_names
from llmproxy.deployment.deploy import deploy_preset, preflight_preset
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
            "llmproxy-data": "e79ce7756e162452a73d6cb57b2e69f55ad1f4a717020527dae500d86dfb4388",
            "llmproxy": "d4a28374ef825ca1d98f8dc9a58c72dcda9d90111788020462c8e911f268c04b",
        },
    },
    "litellm-only": {
        "components": ("llmproxy-data/postgres", "llmproxy/litellm"),
        "features": (),
        "stacks": ("llmproxy-data", "llmproxy"),
        "hashes": {
            "llmproxy-data": "a5e884c6ba0b32835dd708be4712a5343bfaa85f087e64bac3f336e7cd48b1c4",
            "llmproxy": "12282f54e8d6aebf0ffdd716c86845abe688ad9a1c259285d1224a3aede5918f",
        },
    },
    "litellm-standalone": {
        "components": ("llmproxy-data/postgres", "llmproxy/litellm"),
        "features": ("publish",),
        "stacks": ("llmproxy-data", "llmproxy"),
        "hashes": {
            "llmproxy-data": "a5e884c6ba0b32835dd708be4712a5343bfaa85f087e64bac3f336e7cd48b1c4",
            "llmproxy": "7ca2b49016ffeb73b30ea4b5aa642825c2169a7ffae17d5fbc2af0cb8dbdcb41",
        },
    },
    "litellm-traefik": {
        "components": ("llmproxy-data/postgres", "llmproxy/litellm"),
        "features": ("traefik",),
        "stacks": ("llmproxy-data", "llmproxy"),
        "hashes": {
            "llmproxy-data": "a5e884c6ba0b32835dd708be4712a5343bfaa85f087e64bac3f336e7cd48b1c4",
            "llmproxy": "ee159d9cfc3a656e333fa97a4d3bc5fbe7cec019d8c3b3e80ed24afc292d79e7",
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
            "llmproxy-data": "a5e884c6ba0b32835dd708be4712a5343bfaa85f087e64bac3f336e7cd48b1c4",
            "llmproxy": "706dd47141a8aca7b03de7fef16eb87b6953585b1576409c43fe713339a7fa49",
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
            "llmproxy-data": "e79ce7756e162452a73d6cb57b2e69f55ad1f4a717020527dae500d86dfb4388",
            "llmproxy": "13c76825de5a07ae69fce3c7a8c231678fbd7aaa631f116998eb8afaa34ddbda",
        },
    },
    "portainer": {
        "components": ("portainer/portainer",),
        "features": (),
        "stacks": ("portainer",),
        "hashes": {
            "portainer": "caead208efe4d15d1db34e26555c68c89f8763b2ddc9f7bddb726a186bca4124",
        },
    },
    "traefik": {
        "components": ("traefik/traefik",),
        "features": (),
        "stacks": ("traefik",),
        "hashes": {
            "traefik": "c62efaad69385af95c06c308a6a4ca501935eca1428380185a4936210ab815a2",
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
    "TRAEFIK_NODE_ID": "existing-acme-node",
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


class PortainerDeploymentTest(unittest.TestCase):
    def test_portainer_owns_only_management_services(self):
        component = discover_components(ROOT)["portainer/portainer"]

        self.assertEqual(component.stack, "portainer")
        self.assertEqual(component.name, "portainer")
        self.assertEqual(component.services, ("agent", "portainer"))

    def test_portainer_preset_selects_only_the_portainer_component_and_stack(self):
        preset = resolve_preset("portainer", ROOT)

        self.assertEqual(preset.components, ("portainer/portainer",))
        self.assertEqual(preset.features, ())
        self.assertEqual(selected_stack_names("portainer", root=ROOT), ("portainer",))
        self.assertNotIn("portainer/portainer", resolve_preset("all", ROOT).components)

    def test_traefik_has_its_own_component_and_preset(self):
        components = discover_components(ROOT)
        self.assertIn("traefik/traefik", components)
        ingress = components["traefik/traefik"]
        self.assertEqual(ingress.stack, "traefik")
        self.assertEqual(ingress.services, ("traefik",))
        self.assertEqual(ingress.required_environment,
                         ("CF_DNS_API_TOKEN", "LETSENCRYPT_EMAIL", "TRAEFIK_NODE_ID"))
        self.assertEqual(resolve_preset("traefik", ROOT).components, ("traefik/traefik",))
        self.assertEqual(selected_stack_names("traefik", root=ROOT), ("traefik",))
        self.assertNotIn("traefik/traefik", resolve_preset("all", ROOT).components)

    def test_public_network_is_external_to_both_management_and_ingress(self):
        expected = {
            "traefik": {"name": "public", "external": "true"},
            "portainer": {"name": "public", "external": "true"},
        }
        for preset, definition in expected.items():
            with self.subTest(preset=preset), tempfile.TemporaryDirectory() as tmpdir:
                rendered = render_preset(preset, Path(tmpdir), include_local_overrides=False,
                                         env=PORTAINER_ENV, root=ROOT)[preset]
                normalized = normalized_stack([rendered], PORTAINER_ENV)
                networks = normalized.split("\nnetworks:\n", 1)[1]
                public = re.search(r"^  public:\n((?:^    .*(?:\n|$))+)", networks, re.MULTILINE)
                assert public is not None, "Missing public network definition"
                fields = dict(re.findall(r"^    ([a-z_]+): (.+)$", public.group(1), re.MULTILINE))
                self.assertEqual(fields, definition)

    def test_external_network_consumers_have_no_network_owner_ordering_dependency(self):
        for identifier in ("portainer/portainer", "traefik/traefik"):
            self.assertEqual(discover_components(ROOT)[identifier].after, ())
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for root_name in ("components", "presets"):
                for source in (ROOT / root_name).rglob("*"):
                    if not is_public_resource(source):
                        continue
                    destination = root / source.relative_to(ROOT)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
            (root / "presets" / "infrastructure.toml").write_text(
                'extends = "all"\n'
                'components = ["portainer/portainer", "traefik/traefik"]\n'
            )
            self.assertEqual(
                selected_stack_names("infrastructure", root=root),
                ("monitoring", "llmproxy-data", "llmproxy", "portainer", "traefik"),
            )
            self.assertEqual(selected_stack_names("portainer", root=root), ("portainer",))
            self.assertEqual(selected_stack_names("traefik", root=root), ("traefik",))
            self.assertEqual(
                selected_stack_names("infrastructure", stack="portainer", root=root),
                ("portainer",),
            )

    def test_public_presets_preserve_declared_topology_order_and_rendered_bytes(self):
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

    def test_management_service_definitions_remain_byte_identical_to_previous_stack(self):
        source = (ROOT / "components/portainer/portainer/compose.yaml").read_text()
        retained_services = source[source.index("  agent:"):source.index("\nnetworks:")]
        self.assertEqual(hashlib.sha256(retained_services.encode()).hexdigest(),
                         "b5b5cbb12981bf5f668bb46ac9d8972b6e789614d4124243c5bf4b84ef90b49a")

    def test_ingress_retains_network_name_and_reuses_certificate_volume_on_pinned_node(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            rendered = render_preset("traefik", Path(tmpdir), include_local_overrides=False,
                                     env=PORTAINER_ENV, root=ROOT)["traefik"]
            normalized = normalized_stack([rendered], PORTAINER_ENV)
        self.assertIn("name: portainer_traefik-letsencrypt-data", normalized)
        self.assertIn("node.id == existing-acme-node", normalized)
        self.assertIn("name: public", normalized)
        self.assertEqual(normalized.count("external: true"), 2)
        self.assertIn("traefik:v3.6.13@sha256:34d5089d0b414945342848518b383f11f5b3a645504ed87b77ffeb9d683d0e48", normalized)
        self.assertIn("--certificatesresolvers.le.acme.storage=/letsencrypt/acme.json", normalized)
        self.assertIn("source: /var/run/docker.sock", normalized)
        self.assertIn("read_only: true", normalized)
        self.assertEqual(normalized.count("mode: host"), 2)

    def test_management_does_not_require_ingress_credentials_or_node(self):
        preflight_preset("portainer", {"PORTAINER_HOST": "portainer.invalid"}, root=ROOT)

    def test_ingress_preflight_requires_the_certificate_volume_node(self):
        env = {name: value for name, value in PORTAINER_ENV.items() if name != "TRAEFIK_NODE_ID"}
        with self.assertRaisesRegex(DeploymentError, "TRAEFIK_NODE_ID"):
            preflight_preset("traefik", env, root=ROOT)

    def test_traefik_dry_run_targets_only_the_ingress_stack(self):
        with patch("llmproxy.deployment.deploy._run_command") as run_command:
            commands = deploy_preset("traefik", driver="docker", dry_run=True,
                                     env=PORTAINER_ENV, root=ROOT)
        run_command.assert_not_called()
        self.assertEqual(len(commands), 1)
        self.assertRegex(commands[0], r"^docker stack deploy -c .+/traefik\.yaml traefik$")

    def test_portainer_required_environment_is_scoped_to_its_preset(self):
        component = discover_components(ROOT)["portainer/portainer"]

        self.assertEqual(
            set(component.required_environment),
            {"PORTAINER_HOST"},
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
                "presets/traefik.toml",
                "components/traefik/traefik/compose.yaml",
                "components/traefik/traefik/component.toml",
                "components/traefik/stack.toml",
            }.issubset(packaged)
        )
        self.assertIn("components/llmproxy/litellm/compose.local.example.yaml", packaged)
        for path in map(Path, packaged):
            self.assertFalse(any(".local" in part for part in path.parts[:-1]))
            if ".local" in path.name:
                self.assertEqual(path.name, "compose.local.example.yaml")
        self.assertFalse(any(part.startswith(".env") for path in packaged for part in Path(path).parts))

    def test_packaged_resource_fallback_discovers_eight_presets_and_eight_components(self):
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
            self.assertEqual(len(discover_components(selected_root)), 8)
            self.assertEqual(len(list_presets(selected_root)), 8)
            self.assertEqual(resolve_preset("traefik", selected_root).components, ("traefik/traefik",))
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
        self.assertEqual(by_name["TRAEFIK_NODE_ID"], "")
        self.assertEqual(by_name["TRAEFIK_ROUTER_ENTRYPOINTS"], "websecure")
        self.assertEqual(by_name["TRAEFIK_ROUTER_TLS"], "true")
        self.assertEqual(by_name["LETSENCRYPT_RESOLVER"], "le")
        self.assertFalse((ROOT / "portainer" / ".env.example").exists())


if __name__ == "__main__":
    unittest.main()
