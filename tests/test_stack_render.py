import hashlib
import tempfile
import unittest
from pathlib import Path

from llmproxy.deployment import (
    compose_files_for_preset,
    local_override_paths_for_preset,
    render_preset,
)
from llmproxy.deployment.compose import docker_stack_config


def top_level_keys(text: str, section: str) -> set[str]:
    lines = text.splitlines()
    inside = False
    keys = set()
    for line in lines:
        if line == f"{section}:":
            inside = True
            continue
        if inside and line and not line.startswith(" "):
            break
        if inside and line.startswith("  ") and not line.startswith("    ") and ":" in line:
            keys.add(line.strip().split(":", 1)[0])
    return keys


class StackRenderTest(unittest.TestCase):
    def test_monitoring_without_stack_base_only_retains_top_level_extension(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base = root / "compose.yaml"
            base.write_text("services: {}\n")
            files = compose_files_for_preset(
                "all", stack="monitoring", include_local_overrides=False,
            )["monitoring"]
            self.assertEqual(len(files), 1)
            before = docker_stack_config([base, *files], root=root)
            after = docker_stack_config(files, root=root)
            extension = (
                "x-common-healthcheck:\n"
                "  interval: 30s\n  retries: 3\n  timeout: 10s\n"
            )
            # Docker preserves top-level extensions for a single input only.
            # The entire previous render, including service healthcheck, is unchanged.
            self.assertIn("    healthcheck:\n", before)
            self.assertIn("      interval: 30s\n", before)
            self.assertNotIn("x-common-healthcheck:", before)
            self.assertEqual(after, before + extension)
            self.assertEqual(
                hashlib.sha256(before.encode()).hexdigest(),
                "3edec85e05ea1dbf5418214550e90be929eeb1ba7e582a506ada649a86c30990",
            )

    def test_shared_network_stack_base_is_merged_before_components(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "app.toml").write_text(
                'components = ["app/api", "app/worker"]\n'
            )
            stack = root / "components" / "app"
            stack.mkdir(parents=True)
            base = stack / "compose.yaml"
            base.write_text("networks:\n  shared:\n    external: true\n")
            component_files = []
            for name in ("api", "worker"):
                component = stack / name
                component.mkdir()
                compose = component / "compose.yaml"
                compose.write_text(
                    f"services:\n  {name}:\n    image: example/{name}\n"
                    "    networks: [shared]\n"
                )
                component_files.append(compose)

            self.assertEqual(
                compose_files_for_preset("app", root=root, include_local_overrides=False),
                {"app": [base, *component_files]},
            )
            output = render_preset("app", root=root, include_local_overrides=False)["app"]
            text = output.read_text()
            self.assertEqual(top_level_keys(text, "services"), {"api", "worker"})
            self.assertEqual(top_level_keys(text, "networks"), {"shared"})
            self.assertIn("external: true", text)
            self.assertEqual(text.count("shared:"), 3)

    def test_absent_stack_base_preserves_component_feature_and_local_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "app.toml").write_text(
                'components = ["app/b", "app/a"]\n'
                'features = ["second", "first"]\n'
            )
            components = [root / "components" / "app" / name for name in ("b", "a")]
            for component in components:
                (component / "overlays").mkdir(parents=True)
                (component / "compose.yaml").write_text(
                    f"services:\n  {component.name}:\n    image: example/{component.name}\n"
                    "    environment:\n      LAYER: component\n"
                )
                for feature in ("second", "first"):
                    (component / "overlays" / f"{feature}.yaml").write_text(
                        f"services:\n  {component.name}:\n"
                        f"    environment:\n      LAYER: {feature}\n"
                    )
                (component / "compose.local.yaml").write_text(
                    f"services:\n  {component.name}:\n    environment:\n      LAYER: local\n"
                )
            public = [component / "compose.yaml" for component in components] + [
                component / "overlays" / f"{feature}.yaml"
                for feature in ("second", "first")
                for component in components
            ]
            local = [component / "compose.local.yaml" for component in components]
            for include_local in (False, True):
                with self.subTest(include_local=include_local):
                    self.assertEqual(
                        compose_files_for_preset(
                            "app", root=root, include_local_overrides=include_local,
                        ),
                        {"app": public + (local if include_local else [])},
                    )
                    text = render_preset(
                        "app", root=root, include_local_overrides=include_local,
                    )["app"].read_text()
                    self.assertEqual(top_level_keys(text, "services"), {"a", "b"})
                    self.assertEqual(text.count(f"LAYER: {'local' if include_local else 'first'}"), 2)

    def test_rendered_files_are_private(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outputs = render_preset(
                "litellm-only",
                Path(tmpdir),
                include_local_overrides=False,
            )
            self.assertTrue(outputs)
            for output in outputs.values():
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def render(self, preset):
        with tempfile.TemporaryDirectory() as tmpdir:
            outputs = render_preset(preset, Path(tmpdir), include_local_overrides=False)
            return {
                stack: path.read_text()
                for stack, path in outputs.items()
                if stack in {"monitoring", "llmproxy-data", "llmproxy"}
            }

    def test_litellm_only_contains_exact_service_set(self):
        rendered = self.render("litellm-only")
        self.assertEqual(top_level_keys(rendered["llmproxy-data"], "services"), {"db"})
        self.assertEqual(top_level_keys(rendered["llmproxy"], "services"), {"litellm"})

    def test_litellm_only_has_no_optional_resources(self):
        text = "\n".join(self.render("litellm-only").values())
        for forbidden in (
            "cli-proxy-api",
            "headroom-data",
            "HEADROOM_",
            "CLI_PROXY_API_",
            "traefik.http",
            "monitoring",
        ):
            self.assertNotIn(forbidden, text)

    def test_llmproxy_contains_all_application_services_without_monitoring(self):
        rendered = self.render("llmproxy")
        self.assertEqual(
            top_level_keys(rendered["llmproxy"], "services"),
            {"litellm", "cli-proxy-api", "cli-proxy-api-usage", "headroom"},
        )
        self.assertNotIn("monitoring", rendered["llmproxy"])

    def test_all_matches_current_service_and_network_topology(self):
        rendered = self.render("all")
        self.assertEqual(top_level_keys(rendered["monitoring"], "services"), {"netdata"})
        self.assertEqual(top_level_keys(rendered["llmproxy-data"], "services"), {"db"})
        self.assertEqual(
            top_level_keys(rendered["llmproxy"], "services"),
            {"litellm", "cli-proxy-api", "cli-proxy-api-usage", "headroom"},
        )
        self.assertIn("monitoring:", rendered["llmproxy-data"])
        self.assertGreaterEqual(rendered["llmproxy"].count("monitoring:"), 4)

    def test_default_removes_only_headroom_owned_resources(self):
        rendered = self.render("default")
        self.assertEqual(top_level_keys(rendered["monitoring"], "services"), {"netdata"})
        self.assertEqual(top_level_keys(rendered["llmproxy-data"], "services"), {"db"})
        self.assertEqual(
            top_level_keys(rendered["llmproxy"], "services"),
            {"litellm", "cli-proxy-api", "cli-proxy-api-usage"},
        )
        self.assertEqual(
            top_level_keys(rendered["llmproxy"], "volumes"),
            {
                "cli-proxy-api-auth-data",
                "cli-proxy-api-config-data",
                "cli-proxy-api-plugins-data",
                "cli-proxy-api-usage-data",
            },
        )
        self.assertEqual(
            top_level_keys(rendered["llmproxy"], "configs"),
            {"litellm-config-yaml", "cli-proxy-api-config-yaml"},
        )
        self.assertNotIn("headroom", "\n".join(rendered.values()).lower())
        self.assertIn("monitoring:", rendered["llmproxy-data"])

    def test_excluded_component_contributes_no_compose_overlay_or_local_input(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "base.toml").write_text(
                'components = ["app/api", "app/worker"]\n'
                'features = ["monitoring"]\n'
            )
            (root / "presets" / "without-worker.toml").write_text(
                'extends = "base"\nexclude_components = ["app/worker"]\n'
            )
            stack = root / "components" / "app"
            stack.mkdir(parents=True)
            (stack / "compose.yaml").write_text("services: {}\n")
            for name in ("api", "worker"):
                component = root / "components" / "app" / name
                (component / "overlays").mkdir(parents=True)
                (component / "compose.yaml").write_text(
                    f"services:\n  {name}:\n    image: example/{name}\n"
                )
                (component / "overlays" / "monitoring.yaml").write_text(
                    f"services:\n  {name}:\n    networks: [monitoring]\n"
                )
                (component / "compose.local.yaml").write_text(
                    f"services:\n  {name}:\n    environment: [LOCAL=1]\n"
                )

            files = compose_files_for_preset("without-worker", root=root)["app"]

            self.assertEqual(
                files,
                [
                    stack / "compose.yaml",
                    root / "components" / "app" / "api" / "compose.yaml",
                    root
                    / "components"
                    / "app"
                    / "api"
                    / "overlays"
                    / "monitoring.yaml",
                    root / "components" / "app" / "api" / "compose.local.yaml",
                ],
            )

    def test_all_declares_complete_resource_contract(self):
        rendered = self.render("all")
        self.assertEqual(
            top_level_keys(rendered["monitoring"], "volumes"),
            {"netdata-config", "netdata-lib", "netdata-cache"},
        )
        self.assertEqual(
            top_level_keys(rendered["monitoring"], "networks"),
            {"monitoring", "public"},
        )
        self.assertEqual(
            top_level_keys(rendered["monitoring"], "configs"),
            {"netdata-conf"},
        )
        self.assertEqual(
            top_level_keys(rendered["llmproxy-data"], "volumes"),
            {"db-data"},
        )
        self.assertEqual(
            top_level_keys(rendered["llmproxy-data"], "networks"),
            {"internal", "monitoring"},
        )
        self.assertEqual(
            top_level_keys(rendered["llmproxy"], "volumes"),
            {
                "cli-proxy-api-auth-data",
                "cli-proxy-api-config-data",
                "cli-proxy-api-plugins-data",
                "cli-proxy-api-usage-data",
                "headroom-data",
            },
        )
        self.assertEqual(
            top_level_keys(rendered["llmproxy"], "networks"),
            {"internal", "monitoring", "public"},
        )
        self.assertEqual(
            top_level_keys(rendered["llmproxy"], "configs"),
            {"litellm-config-yaml", "cli-proxy-api-config-yaml"},
        )
        for router in ("litellm", "cli-proxy-api", "cli-proxy-api-usage", "headroom"):
            self.assertIn(f"traefik.http.routers.{router}", rendered["llmproxy"])

    def test_all_rendered_runtime_contract_snapshot(self):
        expected = {
            "monitoring": "0a201cbee81da15d4e0c4b30bc5574a7dcf8298011bfaed0a64c500bef5e9183",
            "llmproxy-data": "ca146c25f981697f0d5447a86880b6d9a50a250d0b4bf5ab622ebecf3693a949",
            "llmproxy": "93e248ed0b849d1a60e0f9b8bb77630d94181b9a08e47aa53d19ef950af97cc9",
        }
        rendered = self.render("all")
        actual = {
            stack: hashlib.sha256(text.encode()).hexdigest()
            for stack, text in rendered.items()
        }
        self.assertEqual(actual, expected)

    def test_litellm_traefik_only_adds_litellm_router(self):
        text = self.render("litellm-traefik")["llmproxy"]
        self.assertIn("traefik.http.routers.litellm", text)
        self.assertNotIn("traefik.http.routers.cli-proxy-api", text)

    def test_standalone_publish_is_explicit(self):
        internal = self.render("litellm-only")["llmproxy"]
        standalone = self.render("litellm-standalone")["llmproxy"]
        self.assertNotIn("published:", internal)
        self.assertIn("published: 4000", standalone)

    def test_component_local_overrides_are_selected_and_owner_ordered(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "selected.toml").write_text(
                'components = ["app/a", "app/b"]\n'
            )
            stack = root / "components" / "app"
            stack.mkdir(parents=True)
            (stack / "compose.yaml").write_text("services: {}\n")
            local_paths = []
            for name in ("a", "b", "unselected"):
                component = root / "components" / "app" / name
                component.mkdir(parents=True)
                (component / "compose.yaml").write_text(
                    f"services:\n  {name}:\n    image: example/{name}\n"
                )
                local = component / "compose.local.yaml"
                local.write_text("services: {}\n")
                if name != "unselected":
                    local_paths.append(local)

            overrides = local_override_paths_for_preset("selected", root=root)
            files = compose_files_for_preset("selected", root=root)

        self.assertEqual(overrides, {"app": local_paths})
        self.assertEqual(files["app"][-2:], local_paths)


if __name__ == "__main__":
    unittest.main()
