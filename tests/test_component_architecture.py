import tempfile
import textwrap
import unittest
import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from llmproxy.deployment.discovery import ComponentError, discover_components
from llmproxy.deployment.preset import PresetError, load_preset, resolve_preset
from llmproxy.deployment.compose import (
    DeploymentError,
    compose_files_for_preset,
    resolve_components,
    selected_stack_names,
)
from llmproxy.core.command import discover_component_commands
from llmproxy.core.resources import is_public_resource, resource_root
from llmproxy.cli import main



class ComponentDiscoveryTest(unittest.TestCase):
    def test_generic_discovery_does_not_interpret_service_config_layers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            directory = root / "components" / "app" / "worker"
            directory.mkdir(parents=True)
            (directory / "compose.yaml").write_text(
                "services:\n  worker:\n    image: example/worker\n"
            )
            integration = directory / "integrations" / "app" / "sink"
            integration.mkdir(parents=True)
            (integration / "config.local.json").symlink_to(root / "not-a-core-resource")
            try:
                component = discover_components(root)["app/worker"]
            except ComponentError as exc:
                self.fail(f"Generic discovery interpreted a service-owned layer: {exc}")
            self.assertFalse(hasattr(component, "config_layer"))
            self.assertFalse(hasattr(component, "local_config_layer"))

    def test_symlinked_components_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            root = Path(tmpdir)
            (root / "components").symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaisesRegex(ComponentError, "Components directory.*symlink"):
                discover_components(root)

    def test_symlinked_component_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            root = Path(tmpdir)
            target = Path(outside) / "worker"
            target.mkdir()
            (target / "compose.yaml").write_text(
                "services:\n  worker:\n    image: example/worker\n"
            )
            stack = root / "components" / "app"
            stack.mkdir(parents=True)
            (stack / "worker").symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(ComponentError, "symlink"):
                discover_components(root)

    def test_component_metadata_cannot_escape_component_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component_dir = root / "components" / "app" / "worker"
            component_dir.mkdir(parents=True)
            (component_dir / "compose.yaml").write_text(
                "services:\n  worker:\n    image: example/worker\n"
            )
            (component_dir / "component.toml").write_text(
                'required_files = ["../../outside"]\n'
            )

            with self.assertRaisesRegex(ComponentError, "must stay inside"):
                discover_components(root)

    def test_inline_compose_service_mapping_is_discovered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component_dir = root / "components" / "app" / "worker"
            component_dir.mkdir(parents=True)
            (component_dir / "compose.yaml").write_text(
                "services:\n  worker: {image: example/worker}\n"
            )

            component = discover_components(root)["app/worker"]

            self.assertEqual(component.services, ("worker",))

    def test_minimal_component_is_discovered_from_path_and_compose(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component_dir = root / "components" / "app" / "worker"
            component_dir.mkdir(parents=True)
            (component_dir / "compose.yaml").write_text(
                "services:\n  worker:\n    image: example/worker\n"
            )

            components = discover_components(root)

            component = components["app/worker"]
            self.assertEqual(component.stack, "app")
            self.assertEqual(component.name, "worker")
            self.assertEqual(component.services, ("worker",))
            self.assertEqual(component.requires, ())
            self.assertEqual(component.features, ())
            self.assertFalse(hasattr(component, "config_layer"))

    def test_optional_metadata_and_owned_files_are_discovered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component_dir = root / "components" / "app" / "api"
            (component_dir / "overlays").mkdir(parents=True)
            (component_dir / "commands").mkdir()
            (component_dir / "configs").mkdir()
            (component_dir / "compose.yaml").write_text(
                "services:\n  api:\n    image: example/api\n"
            )
            (component_dir / "overlays" / "traefik.yaml").write_text(
                "services:\n  api:\n    networks: [public]\n"
            )
            integration = component_dir / "integrations" / "app" / "sink"
            integration.mkdir(parents=True)
            (integration / "config.json").write_text("{}\n")
            (component_dir / "commands" / "health.py").write_text(
                "COMMAND = 'health'\nDESCRIPTION = 'Check health'\nMUTATING = False\n"
                "def configure(parser): pass\n"
                "def run(args, context): return 0\n"
            )
            (component_dir / "component.toml").write_text(
                textwrap.dedent(
                    """
                    requires = ["data/postgres"]
                    required_files = ["configs/license.key"]
                    required_environment = ["API_TOKEN"]

                    [[docker_configs]]
                    source = "configs/config.yaml"
                    name = "app_api-config"
                    environment = "API_CONFIG_NAME"
                    """
                ).strip()
                + "\n"
            )

            component = discover_components(root)["app/api"]

            self.assertEqual(component.requires, ("data/postgres",))
            self.assertEqual(component.features, ("traefik",))
            self.assertFalse(hasattr(component, "config_layer"))
            self.assertEqual(component.commands, (component_dir / "commands" / "health.py",))
            self.assertEqual(component.required_files, (component_dir / "configs" / "license.key",))
            self.assertEqual(component.required_environment, ("API_TOKEN",))
            self.assertEqual(component.docker_configs[0].source, component_dir / "configs" / "config.yaml")
            self.assertEqual(component.docker_configs[0].name, "app_api-config")
            self.assertEqual(component.docker_configs[0].environment, "API_CONFIG_NAME")

    def test_real_components_are_component_owned_without_registry(self):
        root = Path(__file__).resolve().parents[1]

        components = discover_components(root)

        self.assertTrue(
            {
                "monitoring/netdata",
                "llmproxy-data/postgres",
                "llmproxy/litellm",
                "llmproxy/cli-proxy-api",
                "llmproxy/cli-proxy-api-usage",
                "llmproxy/headroom",
            }.issubset(components),
        )
        self.assertFalse((root / "deploy" / "registry.json").exists())
        self.assertEqual(
            components["llmproxy/cli-proxy-api-usage"].requires,
            ("llmproxy/cli-proxy-api",),
        )
        self.assertEqual(
            components["llmproxy/litellm"].docker_configs[0].name,
            "llmproxy_litellm-config-yaml",
        )

    def test_netdata_declares_its_mandatory_traefik_feature(self):
        root = Path(__file__).resolve().parents[1]
        component = discover_components(root)["monitoring/netdata"]

        self.assertEqual(component.required_features, ("traefik",))
        self.assertIn("traefik", resolve_preset("all", root).features)

    def test_component_cannot_be_selected_without_a_mandatory_feature(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "private.toml").write_text(
                'components = ["monitoring/netdata"]\nfeatures = []\n'
            )
            component = root / "components" / "monitoring" / "netdata"
            component.mkdir(parents=True)
            (component / "compose.yaml").write_text(
                "services:\n  netdata:\n    image: netdata\n"
            )
            (component / "component.toml").write_text(
                'required_features = ["traefik"]\n'
            )

            with self.assertRaisesRegex(DeploymentError, "requires features: traefik"):
                resolve_components("private", root)


class StackOwnershipTest(unittest.TestCase):
    def test_nonregular_stack_compose_entries_are_rejected(self):
        for kind in ("directory", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                service = root / "components" / "app" / "api"
                service.mkdir(parents=True)
                (service / "compose.yaml").write_text(
                    "services:\n  api:\n    image: example/api\n"
                )
                (root / "presets").mkdir()
                (root / "presets" / "app.toml").write_text('components = ["app/api"]\n')
                base = service.parent / "compose.yaml"
                if kind == "directory":
                    base.mkdir()
                else:
                    os.mkfifo(base)

                with self.assertRaisesRegex(DeploymentError, "Stack compose file.*regular file"):
                    compose_files_for_preset("app", root=root)

    def test_legacy_stack_base_is_not_a_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "app.toml").write_text('components = ["app/api"]\n')
            service = root / "components" / "app" / "api"
            service.mkdir(parents=True)
            (service / "compose.yaml").write_text("services:\n  api:\n    image: example/api\n")
            legacy = root / "stacks" / "app"
            legacy.mkdir(parents=True)
            (legacy / "compose.yaml").write_text(
                "services:\n  retired:\n    image: example/retired\n"
            )

            self.assertEqual(
                compose_files_for_preset("app", root=root),
                {"app": [service / "compose.yaml"]},
            )

    def test_stack_files_cannot_escape_to_another_owner_or_dangling_target(self):
        for filename in ("compose.yaml", "stack.toml"):
            for existing in (True, False):
                with self.subTest(filename=filename, existing=existing), tempfile.TemporaryDirectory() as tmpdir:
                    root = Path(tmpdir)
                    stack = root / "components" / "app"
                    (stack / "api").mkdir(parents=True)
                    (stack / "api" / "compose.yaml").write_text(
                        "services:\n  api:\n    image: example/api\n"
                    )
                    (root / "presets").mkdir()
                    (root / "presets" / "app.toml").write_text('components = ["app/api"]\n')
                    (stack / "compose.yaml").write_text("services: {}\n")
                    (stack / "stack.toml").write_text("order = 10\n")
                    foreign = root / "components" / "other" / filename
                    foreign.parent.mkdir()
                    if existing:
                        foreign.write_text("not valid compose or metadata\n")
                    (stack / filename).unlink()
                    (stack / filename).symlink_to(foreign)

                    with self.assertRaisesRegex(DeploymentError, "Stack file.*symlink"):
                        compose_files_for_preset("app", root=root)

    def test_stack_files_cannot_be_symlinks_even_to_owned_files(self):
        for filename in ("compose.yaml", "stack.toml"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                stack = root / "components" / "app"
                (stack / "api").mkdir(parents=True)
                (stack / "api" / "compose.yaml").write_text(
                    "services:\n  api:\n    image: example/api\n"
                )
                (root / "presets").mkdir()
                (root / "presets" / "app.toml").write_text('components = ["app/api"]\n')
                (stack / "compose.yaml").write_text("services: {}\n")
                (stack / "stack.toml").write_text("order = 10\n")
                target = stack / f"owned-{filename}"
                (stack / filename).rename(target)
                (stack / filename).symlink_to(target)

                with self.assertRaisesRegex(DeploymentError, "Stack file.*symlink"):
                    compose_files_for_preset("app", root=root)

    def test_checkout_has_only_component_owned_stack_definitions(self):
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "stacks").exists())
        stacks = {component.stack for component in discover_components(root).values()}
        self.assertEqual(stacks, {"llmproxy", "llmproxy-data", "monitoring", "portainer"})
        for stack in stacks:
            with self.subTest(stack=stack):
                self.assertFalse((root / "components" / stack / "compose.yaml").exists())
                self.assertTrue((root / "components" / stack / "stack.toml").is_file())

    def test_stack_bases_and_order_are_owned_beside_service_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "app.toml").write_text(
                'components = ["app/api", "data/db"]\n'
            )
            for stack, service, order in (("app", "api", 20), ("data", "db", 10)):
                directory = root / "components" / stack
                (directory / service).mkdir(parents=True)
                (directory / "compose.yaml").write_text("services: {}\n")
                (directory / "stack.toml").write_text(f"order = {order}\n")
                (directory / service / "compose.yaml").write_text(
                    f"services:\n  {service}:\n    image: example/{service}\n"
                )

            self.assertEqual(selected_stack_names("app", root=root), ("data", "app"))
            self.assertEqual(
                compose_files_for_preset("app", root=root, include_local_overrides=False),
                {
                    "data": [
                        root / "components/data/compose.yaml",
                        root / "components/data/db/compose.yaml",
                    ],
                    "app": [
                        root / "components/app/compose.yaml",
                        root / "components/app/api/compose.yaml",
                    ],
                },
            )
            self.assertEqual(set(discover_components(root)), {"app/api", "data/db"})
            self.assertFalse((root / "stacks").exists())

            # Retired stack metadata must not influence the new owner ordering.
            legacy = root / "stacks" / "app"
            legacy.mkdir(parents=True)
            (legacy / "stack.toml").write_text("order = -100\n")
            self.assertEqual(selected_stack_names("app", root=root), ("data", "app"))


class PresetInheritanceTest(unittest.TestCase):
    def test_symlinked_stack_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "app.toml").write_text(
                'components = ["app/api"]\n'
            )
            external_stack = Path(outside) / "app"
            external_stack.mkdir()
            (external_stack / "compose.yaml").write_text("services: {}\n")
            (external_stack / "api").mkdir()
            (external_stack / "api" / "compose.yaml").write_text(
                "services:\n  api:\n    image: example/api\n"
            )
            (root / "components").mkdir()
            (root / "components" / "app").symlink_to(
                external_stack,
                target_is_directory=True,
            )

            with self.assertRaisesRegex(DeploymentError, "(?i)stack director.*symlink"):
                selected_stack_names("app", root=root)

    def test_preset_name_cannot_escape_preset_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            with self.assertRaisesRegex(Exception, "Invalid preset name"):
                load_preset("../outside", root)

    def test_component_dependency_cycle_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "cycle.toml").write_text(
                'components = ["app/a", "app/b"]\n'
            )
            for name, dependency in (("a", "app/b"), ("b", "app/a")):
                directory = root / "components" / "app" / name
                directory.mkdir(parents=True)
                (directory / "compose.yaml").write_text(
                    f"services:\n  {name}:\n    image: example/{name}\n"
                )
                (directory / "component.toml").write_text(
                    f'requires = ["{dependency}"]\n'
                )

            with self.assertRaisesRegex(DeploymentError, "dependency cycle"):
                resolve_components("cycle", root)

    def test_stack_order_respects_cross_stack_dependency(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "ordered.toml").write_text(
                'components = ["app/api", "data/db"]\n'
            )
            for stack, component in (("app", "api"), ("data", "db")):
                stack_dir = root / "components" / stack
                stack_dir.mkdir(parents=True)
                (stack_dir / "compose.yaml").write_text("services: {}\n")
                (stack_dir / "stack.toml").write_text(
                    "order = 10\n" if stack == "app" else "order = 20\n"
                )
                component_dir = root / "components" / stack / component
                component_dir.mkdir(parents=True)
                (component_dir / "compose.yaml").write_text(
                    f"services:\n  {component}:\n    image: example/{component}\n"
                )
            (root / "components" / "app" / "api" / "component.toml").write_text(
                'requires = ["data/db"]\n'
            )

            self.assertEqual(selected_stack_names("ordered", root=root), ("data", "app"))

    def test_preset_extends_base_and_merges_components_and_features(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            presets = root / "presets"
            presets.mkdir()
            (presets / "base.toml").write_text(
                'description = "base"\ncomponents = ["data/postgres", "app/api"]\nfeatures = []\n'
            )
            (presets / "public.toml").write_text(
                'extends = "base"\ncomponents = ["app/web"]\nfeatures = ["traefik"]\n'
            )

            resolved = resolve_preset("public", root)

            self.assertEqual(
                resolved.components,
                ("data/postgres", "app/api", "app/web"),
            )
            self.assertEqual(resolved.features, ("traefik",))

    def test_preset_can_exclude_an_inherited_component(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            presets = root / "presets"
            presets.mkdir()
            (presets / "base.toml").write_text(
                'components = ["app/api", "app/worker"]\nfeatures = ["monitoring"]\n'
            )
            (presets / "without-worker.toml").write_text(
                'extends = "base"\nexclude_components = ["app/worker"]\n'
            )

            resolved = resolve_preset("without-worker", root)

            self.assertEqual(resolved.components, ("app/api",))
            self.assertEqual(resolved.exclude_components, ("app/worker",))
            self.assertEqual(resolved.features, ("monitoring",))

    def test_load_preset_rejects_non_list_component_exclusions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            presets = root / "presets"
            presets.mkdir()
            (presets / "invalid.toml").write_text(
                'exclude_components = "app/api"\n'
            )

            with self.assertRaisesRegex(
                PresetError,
                "exclude_components must be a list of strings",
            ):
                load_preset("invalid", root)

    def test_load_preset_rejects_non_string_component_exclusions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            presets = root / "presets"
            presets.mkdir()
            (presets / "invalid.toml").write_text(
                'exclude_components = ["app/api", 7]\n'
            )

            with self.assertRaisesRegex(
                PresetError,
                "exclude_components must be a list of strings",
            ):
                load_preset("invalid", root)

    def test_preset_rejects_duplicate_component_exclusions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            presets = root / "presets"
            presets.mkdir()
            (presets / "base.toml").write_text(
                'components = ["app/api", "app/worker"]\n'
            )
            (presets / "duplicate.toml").write_text(
                'extends = "base"\n'
                'exclude_components = ["app/worker", "app/worker"]\n'
            )

            with self.assertRaisesRegex(
                PresetError,
                "duplicate exclude_components: app/worker",
            ):
                resolve_preset("duplicate", root)

    def test_child_can_re_add_a_component_excluded_by_its_parent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            presets = root / "presets"
            presets.mkdir()
            (presets / "base.toml").write_text(
                'components = ["app/api", "app/worker", "app/metrics"]\n'
            )
            (presets / "without-worker.toml").write_text(
                'extends = "base"\nexclude_components = ["app/worker"]\n'
            )
            (presets / "restored.toml").write_text(
                'extends = "without-worker"\n'
                'components = ["app/worker", "app/frontend"]\n'
            )

            resolved = resolve_preset("restored", root)

            self.assertEqual(
                resolved.components,
                ("app/api", "app/metrics", "app/worker", "app/frontend"),
            )
            self.assertEqual(resolved.exclude_components, ())

    def test_excluding_a_required_component_fails_dependency_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            presets = root / "presets"
            presets.mkdir()
            (presets / "base.toml").write_text(
                'components = ["app/api", "app/worker"]\n'
            )
            (presets / "broken.toml").write_text(
                'extends = "base"\nexclude_components = ["app/worker"]\n'
            )
            for name in ("api", "worker"):
                component = root / "components" / "app" / name
                component.mkdir(parents=True)
                (component / "compose.yaml").write_text(
                    f"services:\n  {name}:\n    image: example/{name}\n"
                )
            (root / "components" / "app" / "api" / "component.toml").write_text(
                'requires = ["app/worker"]\n'
            )

            with self.assertRaisesRegex(
                DeploymentError,
                "Component app/api requires: app/worker",
            ):
                resolve_components("broken", root)

    def test_preset_rejects_exclusion_of_an_unselected_component(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            presets = root / "presets"
            presets.mkdir()
            (presets / "base.toml").write_text('components = ["app/api"]\n')
            (presets / "unknown.toml").write_text(
                'extends = "base"\nexclude_components = ["app/missing"]\n'
            )

            with self.assertRaisesRegex(PresetError, "not selected"):
                resolve_preset("unknown", root)

    def test_preset_rejects_adding_and_excluding_the_same_component(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            presets = root / "presets"
            presets.mkdir()
            (presets / "base.toml").write_text('components = ["app/api"]\n')
            (presets / "conflict.toml").write_text(
                'extends = "base"\ncomponents = ["app/worker"]\n'
                'exclude_components = ["app/worker"]\n'
            )

            with self.assertRaisesRegex(PresetError, "both adds and excludes"):
                resolve_preset("conflict", root)

    def test_real_all_preset_inherits_complete_topology(self):
        root = Path(__file__).resolve().parents[1]

        preset = load_preset("all", root)
        resolved = resolve_preset("all", root)

        self.assertEqual(preset.extends, "llmproxy")
        self.assertEqual(
            resolved.components,
            (
                "llmproxy-data/postgres",
                "llmproxy/litellm",
                "llmproxy/cli-proxy-api",
                "llmproxy/cli-proxy-api-usage",
                "llmproxy/headroom",
                "monitoring/netdata",
            ),
        )
        self.assertEqual(resolved.features, ("traefik", "monitoring"))

    def test_real_default_preset_subtracts_only_headroom(self):
        root = Path(__file__).resolve().parents[1]

        preset = load_preset("default", root)
        resolved = resolve_preset("default", root)

        self.assertFalse((root / "presets" / "all-no-headroom.toml").exists())
        with self.assertRaisesRegex(PresetError, "Unknown preset"):
            load_preset("all-no-headroom", root)

        self.assertEqual(preset.extends, "all")
        self.assertEqual(preset.exclude_components, ("llmproxy/headroom",))
        self.assertEqual(
            resolved.components,
            (
                "llmproxy-data/postgres",
                "llmproxy/litellm",
                "llmproxy/cli-proxy-api",
                "llmproxy/cli-proxy-api-usage",
                "monitoring/netdata",
            ),
        )
        self.assertEqual(resolved.features, ("traefik", "monitoring"))


class ComponentCommandDiscoveryTest(unittest.TestCase):
    def _complete_resource_root(self, root: Path) -> None:
        (root / "presets").mkdir(exist_ok=True)
        (root / "components").mkdir(exist_ok=True)

    def test_qualified_commands_distinguish_same_name_in_different_stacks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._complete_resource_root(root)
            for stack in ("monitoring", "observability"):
                component_dir = root / "components" / stack / "netdata"
                commands_dir = component_dir / "commands"
                commands_dir.mkdir(parents=True)
                (component_dir / "compose.yaml").write_text(
                    "services:\n  netdata:\n    image: example/netdata\n"
                )
                (commands_dir / "status.py").write_text(
                    "COMMAND = 'status'\n"
                    "DESCRIPTION = 'Status'\n"
                    "def configure(parser): pass\n"
                    "def run(args, context):\n"
                    "    print(context.stack_name, context.component_name)\n"
                    "    return 0\n"
                )

            for stack in ("monitoring", "observability"):
                with self.subTest(stack=stack), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main([f"{stack}/netdata", "status"], root=root), 0)
                    self.assertEqual(output.getvalue(), f"{stack} netdata\n")

            for prefix in (["component", "monitoring/netdata"], ["netdata"]):
                with self.subTest(prefix=prefix), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as rejected:
                        main([*prefix, "status"], root=root)
                    self.assertEqual(rejected.exception.code, 2)

    def test_confirmation_token_after_separator_does_not_allow_import(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component_dir = root / "components" / "app" / "api"
            commands_dir = component_dir / "commands"
            commands_dir.mkdir(parents=True)
            (root / "presets").mkdir()
            self._complete_resource_root(root)
            (root / "presets" / "all.toml").write_text('components = ["app/api"]\n')
            (root / "components" / "app" / "compose.yaml").write_text("services: {}\n")
            (component_dir / "compose.yaml").write_text(
                "services:\n  api:\n    image: example\n"
            )
            marker = root / "imported"
            (commands_dir / "danger.py").write_text(
                'COMMAND = "danger"\n'
                'DESCRIPTION = "danger"\n'
                'MUTATING = True\n'
                'REQUIRES_CONFIRMATION = True\n'
                f'open({str(marker)!r}, "w").write("imported")\n'
                'def configure(parser): pass\n'
                'def run(args, context): return 0\n'
            )
            self.assertEqual(
                main(["app/api", "danger", "--", "--yes"], root=root),
                2,
            )
            self.assertFalse(marker.exists())

    def test_generic_command_does_not_import_component_command_modules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component_dir = root / "components" / "app" / "api"
            commands_dir = component_dir / "commands"
            commands_dir.mkdir(parents=True)
            self._complete_resource_root(root)
            (component_dir / "compose.yaml").write_text(
                "services:\n  api:\n    image: example/api\n"
            )
            marker = root / "imported"
            (commands_dir / "status.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('imported')\n"
                "COMMAND = 'status'\n"
                "DESCRIPTION = 'Status'\n"
                "MUTATING = False\n"
                "def configure(parser): pass\n"
                "def run(args, context): return 0\n"
            )

            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["components"], root=root), 0)
            self.assertFalse(marker.exists())
            self.assertEqual(main(["app/api", "status"], root=root), 0)
            self.assertTrue(marker.exists())

    def test_command_requiring_confirmation_is_blocked_without_yes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component_dir = root / "components" / "app" / "api"
            commands_dir = component_dir / "commands"
            commands_dir.mkdir(parents=True)
            (root / "presets").mkdir()
            self._complete_resource_root(root)
            (component_dir / "compose.yaml").write_text(
                "services:\n  api:\n    image: example/api\n"
            )
            marker = root / "ran"
            imported = root / "imported"
            (commands_dir / "danger.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(imported)!r}).write_text('imported')\n"
                "COMMAND = 'danger'\n"
                "DESCRIPTION = 'Dangerous operation'\n"
                "MUTATING = True\n"
                "REQUIRES_CONFIRMATION = True\n"
                "def configure(parser): pass\n"
                f"def run(args, context): Path({str(marker)!r}).write_text('yes'); return 0\n"
            )

            with redirect_stderr(io.StringIO()):
                blocked = main(["app/api", "danger"], root=root)
            self.assertFalse(imported.exists())
            allowed = main(["app/api", "danger", "--yes"], root=root)

            self.assertEqual(blocked, 2)
            self.assertEqual(allowed, 0)
            self.assertTrue(imported.is_file())
            self.assertTrue(marker.is_file())

    def test_component_command_registers_canonical_path_and_alias(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component_dir = root / "components" / "app" / "api"
            commands_dir = component_dir / "commands"
            commands_dir.mkdir(parents=True)
            (component_dir / "compose.yaml").write_text(
                "services:\n  api:\n    image: example/api\n"
            )
            (commands_dir / "token.py").write_text(
                "COMMAND = 'token'\n"
                "ALIASES = (('key', 'create'),)\n"
                "DESCRIPTION = 'Create token'\n"
                "MUTATING = True\n"
                "def configure(parser): pass\n"
                "def run(args, context): return 0\n"
            )

            commands = discover_component_commands(root)

            self.assertEqual(
                {command.path for command in commands},
                {
                    ("app/api", "token"),
                    ("key", "create"),
                },
            )
            self.assertTrue(all(command.component_identifier == "app/api" for command in commands))


class ResourceRootTest(unittest.TestCase):
    def test_public_resource_filter_matches_local_ignore_convention(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            public = root / "compose.yaml"
            public.write_text("services: {}\n")
            self.assertTrue(is_public_resource(public))
            for name in (
                "config.local",
                "config.local.yaml",
                "config.local-private.json",
                ".env",
                ".env.production",
                ".envrc",
                "config.gen.json",
                "openapi.json",
            ):
                path = root / name
                path.write_text("private\n")
                self.assertFalse(is_public_resource(path), name)
            local_directory_file = root / "configs.local" / "secret.yaml"
            local_directory_file.parent.mkdir()
            local_directory_file.write_text("private\n")
            self.assertFalse(is_public_resource(local_directory_file))
            external = root / "external-secret"
            external.write_text("private\n")
            linked = root / "linked.yaml"
            linked.symlink_to(external)
            self.assertFalse(is_public_resource(linked))

            for directory in (".env", ".env.prod"):
                secret = root / "nested" / directory / "secret.txt"
                secret.parent.mkdir(parents=True)
                secret.write_text("private\n")
                self.assertFalse(is_public_resource(secret), str(secret))

    def test_explicit_resource_root_overrides_checkout_discovery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for name in ("components", "presets"):
                (root / name).mkdir()
            (root / "presets" / "all.toml").write_text(
                'components = ["app/api", "app/headroom"]\n'
            )
            (root / "presets" / "default.toml").write_text(
                'extends = "all"\nexclude_components = ["app/headroom"]\n'
            )
            with patch.dict("os.environ", {"LLMPROXY_ROOT": str(root)}, clear=False):
                selected_root = resource_root()

            self.assertEqual(selected_root, root)
            self.assertEqual(
                resolve_preset("default", selected_root).components,
                ("app/api",),
            )


if __name__ == "__main__":
    unittest.main()
