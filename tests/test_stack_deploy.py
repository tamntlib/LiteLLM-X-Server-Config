import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from llmproxy.cli import main
from llmproxy.core.env import load_dotenv
from llmproxy.core.resources import is_public_resource
from llmproxy.deployment import (
    ROOT,
    DeploymentError,
    _assert_no_unapproved_service_removals,
    docker_stack_config,
    content_addressed_config_name,
    deploy_preset,
    load_env_file,
    load_env_files,
    preflight_preset,
)


class DeploySupportTest(unittest.TestCase):
    def test_dotenv_does_not_override_explicit_process_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".env"
            path.write_text("TARGET=file\nFILE_ONLY=value\n")
            with patch.dict(os.environ, {"TARGET": "process"}, clear=True):
                load_dotenv(path)
                self.assertEqual(os.environ["TARGET"], "process")
                self.assertEqual(os.environ["FILE_ONLY"], "value")

    def test_content_addressed_name_changes_only_when_content_changes(self):
        first = content_addressed_config_name("llmproxy_litellm-config-yaml", b"a")
        same = content_addressed_config_name("llmproxy_litellm-config-yaml", b"a")
        changed = content_addressed_config_name("llmproxy_litellm-config-yaml", b"b")
        self.assertEqual(first, same)
        self.assertNotEqual(first, changed)
        self.assertRegex(first, r"^llmproxy_litellm-config-yaml-[0-9a-f]{12}$")

    def test_env_file_parser_does_not_override_process_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".env"
            path.write_text("A=file\nB='two words'\n# comment\n")
            with patch.dict(os.environ, {"A": "process"}, clear=False):
                env = load_env_file(path)
        self.assertEqual(env["A"], "process")
        self.assertEqual(env["B"], "two words")

    def test_later_env_file_wins_before_process_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            first = Path(tmpdir) / "first.env"
            second = Path(tmpdir) / "second.env"
            first.write_text("SHARED=first\nFIRST_ONLY=yes\n")
            second.write_text("SHARED=second\n")
            with patch.dict(os.environ, {}, clear=True):
                env = load_env_files([first, second])
        self.assertEqual(env["SHARED"], "second")
        self.assertEqual(env["FIRST_ONLY"], "yes")

    def test_litellm_only_does_not_require_cli_proxy_private_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "litellm-only.toml").write_text(
                'components = ["llmproxy/litellm"]\n'
            )
            (root / "components" / "llmproxy").mkdir(parents=True)
            (root / "components" / "llmproxy" / "compose.yaml").write_text("services: {}\n")
            component = root / "components" / "llmproxy" / "litellm"
            (component / "configs").mkdir(parents=True)
            (component / "compose.yaml").write_text("services:\n  litellm:\n    image: example\n")
            (component / "component.toml").write_text(
                'required_files = ["configs/litellm.yaml"]\n'
                'required_environment = ["DB_PASSWORD", "LITELLM_MASTER_KEY", "LITELLM_SALT_KEY"]\n'
            )
            (component / "configs" / "litellm.yaml").write_text("general_settings: {}\n")
            env = {
                "DB_PASSWORD": "x",
                "LITELLM_MASTER_KEY": "x",
                "LITELLM_SALT_KEY": "x",
            }
            preflight_preset("litellm-only", env, root=root)

    def test_excluded_component_contributes_no_required_inputs_or_docker_configs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "base.toml").write_text(
                'components = ["app/api", "app/worker"]\n'
            )
            (root / "presets" / "without-worker.toml").write_text(
                'extends = "base"\nexclude_components = ["app/worker"]\n'
            )
            stack = root / "components" / "app"
            stack.mkdir(parents=True)
            (stack / "compose.yaml").write_text("services: {}\n")
            api = root / "components" / "app" / "api"
            api.mkdir(parents=True)
            (api / "compose.yaml").write_text(
                "services:\n  api:\n    image: example/api\n"
            )
            worker = root / "components" / "app" / "worker"
            worker.mkdir(parents=True)
            (worker / "compose.yaml").write_text(
                "services:\n  worker:\n    image: example/worker\n"
                "    environment: [WORKER_INLINE=${WORKER_INLINE}]\n"
            )
            (worker / "component.toml").write_text(
                'required_files = ["missing.local.yaml"]\n'
                'required_environment = ["WORKER_REQUIRED"]\n'
                "[[docker_configs]]\n"
                'source = "missing.local.yaml"\n'
                'name = "app_worker-config"\n'
                'environment = "WORKER_CONFIG_NAME"\n'
            )
            output = root / "build" / "without-worker" / "app.yaml"
            output.parent.mkdir(parents=True)
            output.write_text("services: {}\n")

            preflight_preset("without-worker", {}, root=root)
            with patch(
                "llmproxy.deployment.deploy.render_preset",
                return_value={"app": output},
            ):
                commands = deploy_preset(
                    "without-worker",
                    dry_run=True,
                    env={},
                    root=root,
                )

            self.assertEqual(
                commands,
                [f"docker stack deploy -c {output} app"],
            )

    def test_llmproxy_requires_cli_proxy_private_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "llmproxy.toml").write_text(
                'components = ["llmproxy/cli-proxy-api"]\n'
            )
            (root / "components" / "llmproxy").mkdir(parents=True)
            (root / "components" / "llmproxy" / "compose.yaml").write_text("services: {}\n")
            component = root / "components" / "llmproxy" / "cli-proxy-api"
            component.mkdir(parents=True)
            (component / "compose.yaml").write_text("services:\n  cli-proxy-api:\n    image: example\n")
            (component / "component.toml").write_text(
                'required_files = ["configs/config.local.yaml"]\n'
            )
            env = {
                "DB_PASSWORD": "x",
                "LITELLM_MASTER_KEY": "x",
                "LITELLM_SALT_KEY": "x",
                "CLI_PROXY_API_MANAGEMENT_KEY": "x",
                "CLI_PROXY_API_USAGE_LOGIN_PASSWORD": "x",
                "HEADROOM_API_KEY": "x",
                "LITELLM_HOST": "x",
                "CLI_PROXY_API_HOST": "x",
                "CLI_PROXY_API_USAGE_HOST": "x",
                "HEADROOM_HOST": "x",
                "HEADROOM_BASIC_AUTH": "x",
            }
            with self.assertRaisesRegex(DeploymentError, "configs/config.local.yaml"):
                preflight_preset("llmproxy", env, root=root)

    def test_docker_dry_run_never_probes_ptctools(self):
        with patch("llmproxy.deployment.deploy.shutil.which") as which:
            commands = deploy_preset(
                "litellm-only",
                driver="docker",
                dry_run=True,
                env={
                    "DB_PASSWORD": "x",
                    "LITELLM_MASTER_KEY": "x",
                    "LITELLM_SALT_KEY": "x",
                },
                skip_file_preflight=True,
            )
        which.assert_not_called()
        self.assertTrue(any("stack deploy" in command for command in commands))
        self.assertFalse(any("ptctools" in command for command in commands))

    def test_disabled_local_overrides_are_excluded_from_preflight(self):
        with patch("llmproxy.deployment.deploy.preflight_preset") as preflight:
            deploy_preset(
                "litellm-only",
                driver="docker",
                dry_run=True,
                include_local_overrides=False,
                env={
                    "DB_PASSWORD": "x",
                    "LITELLM_MASTER_KEY": "x",
                    "LITELLM_SALT_KEY": "x",
                },
                skip_file_preflight=True,
            )

        self.assertFalse(preflight.call_args.kwargs["include_local_overrides"])

    def test_ptctools_driver_is_lazy_and_uses_uvx(self):
        with patch("llmproxy.deployment.deploy.shutil.which", return_value="/usr/bin/uvx") as which:
            commands = deploy_preset(
                "llmproxy",
                driver="ptctools",
                dry_run=True,
                allow_remove_services=True,
                env={
                    "DB_PASSWORD": "x",
                    "LITELLM_MASTER_KEY": "x",
                    "LITELLM_SALT_KEY": "x",
                    "CLI_PROXY_API_MANAGEMENT_KEY": "x",
                    "CLI_PROXY_API_USAGE_LOGIN_PASSWORD": "x",
                    "HEADROOM_API_KEY": "x",
                    "LITELLM_HOST": "llm.example.com",
                    "CLI_PROXY_API_HOST": "cpa.example.com",
                    "CLI_PROXY_API_USAGE_HOST": "usage.example.com",
                    "HEADROOM_HOST": "headroom.example.com",
                    "HEADROOM_BASIC_AUTH": "x",
                },
                skip_file_preflight=True,
            )
        which.assert_called_once_with("uvx")
        self.assertTrue(any(command.startswith("uvx ptctools ") for command in commands))
        deploys = [command for command in commands if "stack deploy" in command]
        self.assertTrue(all("--env-file" in command for command in deploys))
        self.assertTrue(all("<generated-env:" in command for command in deploys))
        self.assertFalse(any("headroom.example.com" in command for command in commands))

    def test_ptctools_uses_temporary_filtered_env_files(self):
        observed_paths = []
        observed_contents = []
        observed_modes = []
        observed_envs = []

        def record(command, *, env, check=True, root=ROOT):
            observed_envs.append((list(command), dict(env)))
            if "stack" in command and "deploy" in command:
                env_path = Path(command[command.index("--env-file") + 1])
                observed_paths.append(env_path)
                observed_contents.append(env_path.read_text())
                observed_modes.append(env_path.stat().st_mode & 0o777)
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("llmproxy.deployment.deploy.shutil.which", return_value="/usr/bin/uvx"), patch(
            "llmproxy.deployment.deploy._run_command", side_effect=record
        ):
            deploy_preset(
                "litellm-only",
                driver="ptctools",
                allow_remove_services=True,
                env={
                    "DB_PASSWORD": "database-secret",
                    "LITELLM_MASTER_KEY": "master-secret",
                    "LITELLM_SALT_KEY": "salt-secret",
                    "PORTAINER_URL": "https://portainer.example.com",
                    "PORTAINER_ACCESS_TOKEN": "portainer-secret",
                    "UNRELATED_SECRET": "must-not-be-written",
                },
                skip_file_preflight=True,
            )

        self.assertTrue(observed_contents)
        self.assertTrue(all(mode == 0o600 for mode in observed_modes))
        self.assertTrue(any("DB_PASSWORD=database-secret" in text for text in observed_contents))
        self.assertTrue(all("UNRELATED_SECRET" not in text for text in observed_contents))
        self.assertTrue(all(not path.exists() for path in observed_paths))
        self.assertTrue(
            all(env.get("PORTAINER_ACCESS_TOKEN") == "portainer-secret" for _, env in observed_envs)
        )
        self.assertTrue(all("DB_PASSWORD" not in env for _, env in observed_envs))
        self.assertTrue(all("LITELLM_MASTER_KEY" not in env for _, env in observed_envs))
        self.assertTrue(all("UNRELATED_SECRET" not in env for _, env in observed_envs))

    def test_ptctools_skips_content_addressed_config_that_already_exists(self):
        observed = []

        def record(command, *, env, check=True, root=ROOT):
            observed.append(list(command))
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with (
            patch("llmproxy.deployment.deploy.shutil.which", return_value="/usr/bin/uvx"),
            patch("llmproxy.deployment.deploy._run_command", side_effect=record),
        ):
            deploy_preset(
                "litellm-only",
                driver="ptctools",
                allow_remove_services=True,
                env={
                    "DB_PASSWORD": "database-secret",
                    "LITELLM_MASTER_KEY": "master-secret",
                    "LITELLM_SALT_KEY": "salt-secret",
                    "PORTAINER_URL": "https://portainer.example.com",
                    "PORTAINER_ACCESS_TOKEN": "portainer-secret",
                },
                skip_file_preflight=True,
            )

        self.assertTrue(any(command[3:5] == ["config", "get"] for command in observed))
        self.assertFalse(any(command[3:5] == ["config", "set"] for command in observed))

    def test_ptctools_config_inventory_failure_stops_deploy(self):
        def record(command, *, env, check=True, root=ROOT):
            if "config" in command and "get" in command:
                return type(
                    "Result",
                    (),
                    {"returncode": 1, "stdout": "", "stderr": "connection refused at http://localhost:4040/api"},
                )()
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with (
            patch("llmproxy.deployment.deploy.shutil.which", return_value="/usr/bin/uvx"),
            patch("llmproxy.deployment.deploy._run_command", side_effect=record),
            self.assertRaisesRegex(DeploymentError, "Cannot inspect Portainer config"),
        ):
            deploy_preset(
                "litellm-only",
                driver="ptctools",
                allow_remove_services=True,
                env={
                    "DB_PASSWORD": "database-secret",
                    "LITELLM_MASTER_KEY": "master-secret",
                    "LITELLM_SALT_KEY": "salt-secret",
                    "PORTAINER_URL": "https://portainer.example.com",
                    "PORTAINER_ACCESS_TOKEN": "portainer-secret",
                },
                skip_file_preflight=True,
            )

    def test_ptctools_mixed_error_and_not_found_response_fails_closed(self):
        def record(command, *, env, check=True, root=ROOT):
            if command[3:5] == ["config", "get"]:
                name = command[command.index("-n") + 1]
                return type(
                    "Result",
                    (),
                    {
                        "returncode": 1,
                        "stdout": "",
                        "stderr": (
                            "authentication failed\n"
                            f"Error: Config '{name}' not found\n"
                        ),
                    },
                )()
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with (
            patch("llmproxy.deployment.deploy.shutil.which", return_value="/usr/bin/uvx"),
            patch("llmproxy.deployment.deploy._run_command", side_effect=record),
            self.assertRaisesRegex(DeploymentError, "Cannot inspect Portainer config"),
        ):
            deploy_preset(
                "litellm-only",
                driver="ptctools",
                allow_remove_services=True,
                env={
                    "DB_PASSWORD": "x",
                    "LITELLM_MASTER_KEY": "x",
                    "LITELLM_SALT_KEY": "x",
                    "PORTAINER_URL": "https://portainer.example.com",
                    "PORTAINER_ACCESS_TOKEN": "x",
                },
                skip_file_preflight=True,
            )

    def test_docker_config_inventory_failure_stops_deploy(self):
        def record(command, *, env, check=True, root=ROOT):
            if "info" in command:
                return type(
                    "Result",
                    (),
                    {"returncode": 0, "stdout": "true\n", "stderr": ""},
                )()
            if "config" in command and "ls" in command:
                return type(
                    "Result",
                    (),
                    {"returncode": 1, "stdout": "", "stderr": "docker daemon unavailable"},
                )()
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with (
            patch("llmproxy.deployment.deploy._run_command", side_effect=record),
            self.assertRaisesRegex(DeploymentError, "Cannot inspect Docker config"),
        ):
            deploy_preset(
                "litellm-only",
                driver="docker",
                allow_remove_services=True,
                env={
                    "DB_PASSWORD": "x",
                    "LITELLM_MASTER_KEY": "x",
                    "LITELLM_SALT_KEY": "x",
                },
                skip_file_preflight=True,
            )

    def test_docker_creates_config_after_successful_empty_inventory(self):
        observed = []

        def record(command, *, env, check=True, root=ROOT):
            observed.append(list(command))
            if "info" in command:
                return type(
                    "Result",
                    (),
                    {"returncode": 0, "stdout": "true\n", "stderr": ""},
                )()
            if "config" in command and "ls" in command:
                return type(
                    "Result",
                    (),
                    {
                        "returncode": 0,
                        "stdout": "",
                        "stderr": "",
                    },
                )()
            if "stack" in command and "services" in command:
                stack_name = command[command.index("services") + 1]
                services = {
                    "llmproxy-data": "llmproxy-data_db\n",
                    "llmproxy": "llmproxy_litellm\n",
                }
                return type(
                    "Result",
                    (),
                    {"returncode": 0, "stdout": services[stack_name], "stderr": ""},
                )()
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("llmproxy.deployment.deploy._run_command", side_effect=record):
            deploy_preset(
                "litellm-only",
                driver="docker",
                allow_remove_services=True,
                env={
                    "DB_PASSWORD": "x",
                    "LITELLM_MASTER_KEY": "x",
                    "LITELLM_SALT_KEY": "x",
                },
                skip_file_preflight=True,
            )

        self.assertTrue(
            any("config" in command and "create" in command for command in observed)
        )

    def test_ptctools_creates_config_only_after_exact_not_found_response(self):
        observed = []

        def record(command, *, env, check=True, root=ROOT):
            observed.append(list(command))
            if command[3:5] == ["config", "get"]:
                name = command[command.index("-n") + 1]
                return type(
                    "Result",
                    (),
                    {
                        "returncode": 1,
                        "stdout": "",
                        "stderr": f"Error: Config '{name}' not found\n",
                    },
                )()
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with (
            patch("llmproxy.deployment.deploy.shutil.which", return_value="/usr/bin/uvx"),
            patch("llmproxy.deployment.deploy._run_command", side_effect=record),
        ):
            deploy_preset(
                "litellm-only",
                driver="ptctools",
                allow_remove_services=True,
                env={
                    "DB_PASSWORD": "database-secret",
                    "LITELLM_MASTER_KEY": "master-secret",
                    "LITELLM_SALT_KEY": "salt-secret",
                    "PORTAINER_URL": "https://portainer.example.com",
                    "PORTAINER_ACCESS_TOKEN": "portainer-secret",
                },
                skip_file_preflight=True,
            )

        self.assertTrue(any(command[3:5] == ["config", "set"] for command in observed))

    def test_renderer_subprocess_environment_excludes_deployment_secrets(self):
        observed_env = {}

        def render(command, *, cwd, env, text, capture_output, check):
            observed_env.update(env)
            return type(
                "Result",
                (),
                {"returncode": 0, "stdout": "services: {}\n", "stderr": ""},
            )()

        with patch("llmproxy.deployment.compose.subprocess.run", side_effect=render):
            rendered = docker_stack_config(
                [ROOT / "components/llmproxy/compose.yaml"],
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "DB_PASSWORD": "database-secret",
                    "UNRELATED_SECRET": "unrelated-secret",
                },
            )

        self.assertEqual(rendered, "services: {}\n")
        self.assertIn("PATH", observed_env)
        self.assertNotIn("DB_PASSWORD", observed_env)
        self.assertNotIn("UNRELATED_SECRET", observed_env)

    def test_ptctools_requires_explicit_remote_risk_acknowledgement(self):
        with patch("llmproxy.deployment.deploy.shutil.which", return_value="/usr/bin/uvx"):
            with self.assertRaisesRegex(DeploymentError, "cannot inspect remote service inventory"):
                deploy_preset(
                    "llmproxy",
                    driver="ptctools",
                    dry_run=True,
                    env={
                        "DB_PASSWORD": "x",
                        "LITELLM_MASTER_KEY": "x",
                        "LITELLM_SALT_KEY": "x",
                        "CLI_PROXY_API_MANAGEMENT_KEY": "x",
                        "CLI_PROXY_API_USAGE_LOGIN_PASSWORD": "x",
                        "HEADROOM_API_KEY": "x",
                        "LITELLM_HOST": "llm.example.com",
                        "CLI_PROXY_API_HOST": "cpa.example.com",
                        "CLI_PROXY_API_USAGE_HOST": "usage.example.com",
                        "HEADROOM_HOST": "headroom.example.com",
                        "HEADROOM_BASIC_AUTH": "x",
                    },
                    skip_file_preflight=True,
                )

    def test_deploy_order_is_data_before_application(self):
        commands = deploy_preset(
            "litellm-only",
            driver="docker",
            dry_run=True,
            env={
                "DB_PASSWORD": "x",
                "LITELLM_MASTER_KEY": "x",
                "LITELLM_SALT_KEY": "x",
            },
            skip_file_preflight=True,
        )
        deploys = [command for command in commands if "stack deploy" in command]
        self.assertIn("llmproxy-data", deploys[0])
        self.assertIn("llmproxy", deploys[1])

    def test_all_deploys_monitoring_before_dependent_stacks(self):
        commands = deploy_preset(
            "all",
            driver="docker",
            dry_run=True,
            env={
                "NETDATA_HOST": "netdata.example.com",
                "NETDATA_BASIC_AUTH": "x",
                "DB_PASSWORD": "x",
                "LITELLM_MASTER_KEY": "x",
                "LITELLM_SALT_KEY": "x",
                "CLI_PROXY_API_MANAGEMENT_KEY": "x",
                "CLI_PROXY_API_USAGE_LOGIN_PASSWORD": "x",
                "HEADROOM_API_KEY": "x",
                "LITELLM_HOST": "llm.example.com",
                "CLI_PROXY_API_HOST": "cpa.example.com",
                "CLI_PROXY_API_USAGE_HOST": "usage.example.com",
                "HEADROOM_HOST": "headroom.example.com",
                "HEADROOM_BASIC_AUTH": "x",
            },
            skip_file_preflight=True,
        )
        deploys = [command for command in commands if "stack deploy" in command]
        self.assertIn(" monitoring", deploys[0])
        self.assertIn("llmproxy-data", deploys[1])
        self.assertIn(" llmproxy", deploys[2])

    def test_default_dry_run_omits_headroom_and_prunes_only_when_authorized(self):
        env = {
            "NETDATA_HOST": "netdata.example.invalid",
            "NETDATA_BASIC_AUTH": "placeholder",
            "DB_PASSWORD": "placeholder",
            "LITELLM_MASTER_KEY": "placeholder",
            "LITELLM_SALT_KEY": "placeholder",
            "CLI_PROXY_API_MANAGEMENT_KEY": "placeholder",
            "CLI_PROXY_API_USAGE_LOGIN_PASSWORD": "placeholder",
            "LITELLM_HOST": "litellm.example.invalid",
            "CLI_PROXY_API_HOST": "cli.example.invalid",
            "CLI_PROXY_API_USAGE_HOST": "usage.example.invalid",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for resource_dir in ("components", "presets"):
                source = ROOT / resource_dir
                for path in source.rglob("*"):
                    if not is_public_resource(path):
                        continue
                    destination = root / path.relative_to(ROOT)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)

            private_config = (
                root
                / "components"
                / "llmproxy"
                / "cli-proxy-api"
                / "configs"
                / "config.local.yaml"
            )
            private_config.write_text("providers: {}\n")

            safe = deploy_preset(
                "default",
                driver="docker",
                dry_run=True,
                env=env,
                include_local_overrides=False,
                root=root,
            )
            authorized = deploy_preset(
                "default",
                driver="docker",
                dry_run=True,
                env=env,
                include_local_overrides=False,
                allow_remove_services=True,
                root=root,
            )

        safe_deploys = [command for command in safe if "stack deploy" in command]
        safe_configs = [command for command in safe if "config create" in command]
        authorized_deploys = [
            command for command in authorized if "stack deploy" in command
        ]
        self.assertEqual(len(safe), 6)
        self.assertEqual(len(safe_configs), 3)
        self.assertTrue(any(str(private_config) in command for command in safe_configs))
        self.assertTrue(
            any("monitoring_netdata-conf-" in command for command in safe_configs)
        )
        self.assertTrue(
            any("llmproxy_litellm-config-yaml-" in command for command in safe_configs)
        )
        self.assertTrue(
            any(
                "llmproxy_cli-proxy-api-config-yaml-" in command
                for command in safe_configs
            )
        )
        self.assertEqual(len(safe_deploys), 3)
        self.assertRegex(safe_deploys[0], r"/monitoring\.yaml monitoring$")
        self.assertRegex(safe_deploys[1], r"/llmproxy-data\.yaml llmproxy-data$")
        self.assertRegex(safe_deploys[2], r"/llmproxy\.yaml llmproxy$")
        self.assertFalse(any("--prune" in command for command in safe_deploys))
        self.assertTrue(all("--prune" in command for command in authorized_deploys))
        command_text = "\n".join((*safe, *authorized))
        self.assertNotIn("components/llmproxy/headroom", command_text)
        self.assertNotIn("HEADROOM_", command_text)

    def test_stack_filter_deploys_only_selected_stack_and_its_configs(self):
        commands = deploy_preset(
            "all",
            stack="monitoring",
            driver="docker",
            dry_run=True,
            env={
                "NETDATA_HOST": "netdata.example.com",
                "NETDATA_BASIC_AUTH": "x",
            },
            skip_file_preflight=True,
        )
        deploys = [command for command in commands if "stack deploy" in command]
        configs = [command for command in commands if "config create" in command]
        self.assertEqual(len(deploys), 1)
        self.assertIn(" monitoring", deploys[0])
        self.assertEqual(len(configs), 1)
        self.assertIn("monitoring_netdata-conf", configs[0])
        self.assertFalse(any("llmproxy_" in command for command in configs))

    def test_stack_filter_rejects_stack_not_in_preset(self):
        with self.assertRaisesRegex(
            DeploymentError,
            "Stack 'monitoring' is not part of preset 'llmproxy'",
        ):
            deploy_preset(
                "llmproxy",
                stack="monitoring",
                driver="docker",
                dry_run=True,
                env={},
                skip_file_preflight=True,
            )

    def test_stack_filter_preflights_only_selected_stack(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "all.toml").write_text(
                'components = ["llmproxy-data/postgres", "monitoring/netdata"]\n'
            )
            (root / "components" / "llmproxy-data").mkdir(parents=True)
            (root / "components" / "llmproxy-data" / "compose.yaml").write_text("services: {}\n")
            postgres = root / "components" / "llmproxy-data" / "postgres"
            postgres.mkdir(parents=True)
            (postgres / "compose.yaml").write_text("services:\n  db:\n    image: postgres\n")
            (postgres / "component.toml").write_text(
                'required_environment = ["DB_PASSWORD"]\n'
            )
            netdata = root / "components" / "monitoring" / "netdata"
            netdata.mkdir(parents=True)
            (netdata / "compose.yaml").write_text("services:\n  netdata:\n    image: netdata\n")
            (netdata / "component.toml").write_text(
                'required_environment = ["NETDATA_HOST"]\n'
            )
            preflight_preset(
                "all",
                {"DB_PASSWORD": "x"},
                stack="llmproxy-data",
                root=root,
            )

    def test_stack_filter_does_not_load_monitoring_env_for_application_stack(self):
        loaded_paths = []

        def read_env(path):
            loaded_paths.append(path)
            return {"DB_PASSWORD": "x"}

        with patch("llmproxy.core.env.read_env_values", side_effect=read_env), patch.dict(
            os.environ,
            {"LITELLM_MASTER_KEY": "x", "LITELLM_SALT_KEY": "x"},
            clear=True,
        ):
            deploy_preset(
                "all",
                stack="llmproxy-data",
                driver="docker",
                dry_run=True,
                skip_file_preflight=True,
            )

        self.assertEqual(loaded_paths, [ROOT / ".env"])

    def test_selected_stack_child_environment_excludes_unrelated_secrets(self):
        observed = []

        def record(command, *, env, check=True, root=ROOT):
            observed.append((list(command), dict(env)))
            if "info" in command:
                stdout = "true\n"
            elif "stack" in command and "services" in command:
                stdout = "llmproxy-data_db\n"
            else:
                stdout = ""
            return type(
                "Result",
                (),
                {"returncode": 0, "stdout": stdout, "stderr": ""},
            )()

        with patch("llmproxy.deployment.deploy._run_command", side_effect=record):
            deploy_preset(
                "all",
                stack="llmproxy-data",
                driver="docker",
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "DB_PASSWORD": "database-secret",
                    "NETDATA_BASIC_AUTH": "monitoring-secret",
                    "PORTAINER_ACCESS_TOKEN": "portainer-secret",
                    "UNRELATED_SECRET": "must-not-reach-child",
                },
                skip_file_preflight=True,
            )

        self.assertTrue(observed)
        deploy_envs = [
            env
            for command, env in observed
            if "stack" in command and "deploy" in command
        ]
        non_deploy_envs = [
            env
            for command, env in observed
            if not ("stack" in command and "deploy" in command)
        ]
        self.assertEqual(len(deploy_envs), 1)
        self.assertEqual(deploy_envs[0].get("DB_PASSWORD"), "database-secret")
        self.assertTrue(all("DB_PASSWORD" not in env for env in non_deploy_envs))
        self.assertTrue(all("NETDATA_BASIC_AUTH" not in env for _, env in observed))
        self.assertTrue(all("PORTAINER_ACCESS_TOKEN" not in env for _, env in observed))
        self.assertTrue(all("UNRELATED_SECRET" not in env for _, env in observed))

    def test_selected_stack_non_dry_commands_keep_context_and_scope(self):
        observed_commands = []

        def record(command, *, env, check=True, root=ROOT):
            observed_commands.append(command)
            if "info" in command:
                stdout = "true\n"
            elif "stack" in command and "services" in command:
                stdout = "llmproxy-data_db\n"
            else:
                stdout = ""
            return type(
                "Result",
                (),
                {"returncode": 0, "stdout": stdout, "stderr": ""},
            )()

        with patch("llmproxy.deployment.deploy._run_command", side_effect=record):
            deploy_preset(
                "all",
                stack="llmproxy-data",
                driver="docker",
                docker_context="production",
                env={"DB_PASSWORD": "x"},
                skip_file_preflight=True,
            )

        self.assertTrue(observed_commands)
        self.assertTrue(
            all(command[:3] == ["docker", "--context", "production"] for command in observed_commands)
        )
        stack_commands = [command for command in observed_commands if "stack" in command]
        self.assertTrue(stack_commands)
        self.assertTrue(all("llmproxy-data" in command for command in stack_commands))
        self.assertFalse(any("monitoring" in command for command in stack_commands))
        self.assertFalse(any(command[-1] == "llmproxy" for command in stack_commands))

    def test_monitoring_stack_loads_root_env_file(self):
        loaded_paths = []

        def read_env(path):
            loaded_paths.append(path)
            return {
                "NETDATA_HOST": "netdata.example.com",
                "NETDATA_BASIC_AUTH": "x",
            }

        with patch("llmproxy.core.env.read_env_values", side_effect=read_env), patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            deploy_preset(
                "all",
                stack="monitoring",
                driver="docker",
                dry_run=True,
                skip_file_preflight=True,
            )

        self.assertEqual(loaded_paths, [ROOT / ".env"])

    def test_full_all_preset_loads_root_env_file_once(self):
        loaded_paths = []

        def read_env(path):
            loaded_paths.append(path)
            return {
                "NETDATA_HOST": "netdata.example.com",
                "NETDATA_BASIC_AUTH": "x",
                "DB_PASSWORD": "x",
                "LITELLM_MASTER_KEY": "x",
                "LITELLM_SALT_KEY": "x",
                "CLI_PROXY_API_MANAGEMENT_KEY": "x",
                "CLI_PROXY_API_USAGE_LOGIN_PASSWORD": "x",
                "HEADROOM_API_KEY": "x",
                "LITELLM_HOST": "llm.example.com",
                "CLI_PROXY_API_HOST": "cpa.example.com",
                "CLI_PROXY_API_USAGE_HOST": "usage.example.com",
                "HEADROOM_HOST": "headroom.example.com",
                "HEADROOM_BASIC_AUTH": "x",
            }

        with patch("llmproxy.core.env.read_env_values", side_effect=read_env), patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            deploy_preset(
                "all",
                driver="docker",
                dry_run=True,
                skip_file_preflight=True,
            )

        self.assertEqual(loaded_paths, [ROOT / ".env"])

    def test_root_env_example_is_the_only_env_template(self):
        root_example = (ROOT / ".env.example").read_text()
        self.assertIn("NETDATA_HOST=", root_example)
        self.assertIn("NETDATA_BASIC_AUTH=", root_example)
        self.assertFalse((ROOT / "monitoring" / ".env.example").exists())

    def test_cli_passes_stack_filter_to_deploy(self):
        with patch("llmproxy.commands.deploy.deploy_preset", return_value=["docker stack deploy"]) as deploy:
            with redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "deploy",
                        "--preset",
                        "all",
                        "--stack",
                        "llmproxy",
                        "--dry-run",
                    ]
                )
        self.assertEqual(result, 0)
        self.assertEqual(deploy.call_args.kwargs["stack"], "llmproxy")

    def test_cli_no_local_overrides_applies_to_deploy(self):
        with patch("llmproxy.commands.deploy.deploy_preset", return_value=["docker stack deploy"]) as deploy:
            with redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "deploy",
                        "--preset",
                        "all",
                        "--no-local-overrides",
                        "--dry-run",
                    ]
                )
        self.assertEqual(result, 0)
        self.assertFalse(deploy.call_args.kwargs["include_local_overrides"])

    def test_inputs_lists_merge_order_and_marks_local_files(self):
        output = io.StringIO()
        files = {
            "llmproxy": [
                ROOT / "components" / "llmproxy" / "compose.yaml",
                ROOT / "components" / "llmproxy" / "litellm" / "compose.yaml",
                ROOT / "components" / "llmproxy" / "litellm" / "compose.local.yaml",
            ]
        }
        with patch("llmproxy.commands.inputs.compose_files_for_preset", return_value=files):
            with redirect_stdout(output):
                result = main(["inputs", "--preset", "all", "--stack", "llmproxy"])

        self.assertEqual(result, 0)
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "llmproxy:",
                "  components/llmproxy/compose.yaml",
                "  components/llmproxy/litellm/compose.yaml",
                "  components/llmproxy/litellm/compose.local.yaml [local]",
            ],
        )

    def test_cli_reports_selected_stack_after_deploy(self):
        output = io.StringIO()
        with patch("llmproxy.commands.deploy.deploy_preset", return_value=[]):
            with redirect_stdout(output):
                result = main(
                    [
                        "deploy",
                        "--preset",
                        "all",
                        "--stack",
                        "llmproxy",
                    ]
                )
        self.assertEqual(result, 0)
        self.assertIn("Submitted stack 'llmproxy' through docker", output.getvalue())
        self.assertIn("not verified", output.getvalue())

    def test_stack_filter_limits_service_removal_check_to_selected_stack(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": "monitoring_netdata\n",
                "stderr": "",
            },
        )()
        with patch("llmproxy.deployment.deploy._run_command", return_value=result) as run:
            _assert_no_unapproved_service_removals(
                "all",
                stack="monitoring",
                context="production",
                env={},
                allow_remove_services=False,
            )
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertIn("monitoring", command)
        self.assertNotIn("llmproxy", command)
        self.assertNotIn("llmproxy-data", command)

    def test_service_removal_requires_explicit_authorization(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": "llmproxy_litellm\nllmproxy_cli-proxy-api\n",
                "stderr": "",
            },
        )()
        with patch("llmproxy.deployment.deploy._run_command", return_value=result):
            with self.assertRaisesRegex(DeploymentError, "would remove services"):
                _assert_no_unapproved_service_removals(
                    "litellm-only",
                    context=None,
                    env={},
                    allow_remove_services=False,
                )

    def test_service_inventory_failure_is_not_treated_as_missing_stack(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": "Cannot connect to the Docker daemon",
            },
        )()
        with patch("llmproxy.deployment.deploy._run_command", return_value=result):
            with self.assertRaisesRegex(DeploymentError, "Cannot inspect service inventory"):
                _assert_no_unapproved_service_removals(
                    "litellm-only",
                    context=None,
                    env={},
                    allow_remove_services=False,
                )

    def test_mixed_service_inventory_error_is_not_treated_as_missing_stack(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": "authentication failed\nnothing found in stack: llmproxy",
            },
        )()
        with patch("llmproxy.deployment.deploy._run_command", return_value=result):
            with self.assertRaisesRegex(DeploymentError, "Cannot inspect service inventory"):
                _assert_no_unapproved_service_removals(
                    "litellm-only",
                    context=None,
                    env={},
                    allow_remove_services=False,
                )

    def test_missing_stack_is_safe_for_initial_deploy(self):
        def missing_stack(command, *, env, check=False, root=ROOT):
            stack_name = command[command.index("services") + 1]
            return type(
                "Result",
                (),
                {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": f"nothing found in stack: {stack_name}",
                },
            )()

        with patch(
            "llmproxy.deployment.deploy._run_command", side_effect=missing_stack
        ):
            _assert_no_unapproved_service_removals(
                "litellm-only",
                context=None,
                env={},
                allow_remove_services=False,
            )

    def test_docker_prune_is_enabled_only_with_removal_authorization(self):
        env = {
            "DB_PASSWORD": "x",
            "LITELLM_MASTER_KEY": "x",
            "LITELLM_SALT_KEY": "x",
        }
        safe = deploy_preset(
            "litellm-only",
            driver="docker",
            dry_run=True,
            env=env,
            skip_file_preflight=True,
        )
        destructive = deploy_preset(
            "litellm-only",
            driver="docker",
            dry_run=True,
            env=env,
            allow_remove_services=True,
            skip_file_preflight=True,
        )
        self.assertFalse(any("--prune" in command for command in safe))
        self.assertTrue(
            all("--prune" in command for command in destructive if "stack deploy" in command)
        )


class DeployRootContractTest(unittest.TestCase):
    def test_rendered_deploy_output_stays_under_alternate_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output = root / "build" / "alternate" / "app.yaml"
            output.parent.mkdir(parents=True)
            output.write_text("services: {}\n")
            with (
                patch(
                    "llmproxy.deployment.deploy.selected_stack_names",
                    return_value=["app"],
                ),
                patch(
                    "llmproxy.deployment.deploy.resolve_components",
                    return_value=[],
                ),
                patch("llmproxy.deployment.deploy.preflight_preset"),
                patch(
                    "llmproxy.deployment.deploy.render_preset",
                    return_value={"app": output},
                ) as render,
            ):
                deploy_preset(
                    "alternate",
                    env={},
                    dry_run=True,
                    root=root,
                )
            self.assertEqual(
                render.call_args.kwargs["output_dir"],
                root / "build" / "alternate",
            )

    def test_cli_threads_application_root_to_deploy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            alternate_root = Path(tmpdir)
            for name in ("components", "presets"):
                (alternate_root / name).mkdir()
            with patch("llmproxy.commands.deploy.deploy_preset", return_value=[]) as deploy:
                code = main(
                    ["deploy", "--preset", "alternate-only", "--dry-run"],
                    root=alternate_root,
                )
            self.assertEqual(code, 0)
            self.assertEqual(deploy.call_args.kwargs["root"], alternate_root)

    def test_service_removal_guard_uses_alternate_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "presets").mkdir()
            (root / "presets" / "same-name.toml").write_text(
                'components = ["llmproxy/newapi"]\n'
            )
            (root / "components" / "llmproxy").mkdir(parents=True)
            (root / "components" / "llmproxy" / "compose.yaml").write_text(
                "services: {}\n"
            )
            component = root / "components" / "llmproxy" / "newapi"
            component.mkdir(parents=True)
            (component / "compose.yaml").write_text(
                "services:\n  newapi:\n    image: example/newapi\n"
            )
            result = type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": "llmproxy_litellm\n",
                    "stderr": "",
                },
            )()
            with patch(
                "llmproxy.deployment.deploy._run_command", return_value=result
            ):
                with self.assertRaisesRegex(DeploymentError, "llmproxy_litellm"):
                    _assert_no_unapproved_service_removals(
                        "same-name",
                        context=None,
                        env={},
                        allow_remove_services=False,
                        root=root,
                    )


if __name__ == "__main__":
    unittest.main()
