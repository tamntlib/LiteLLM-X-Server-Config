"""Docker Swarm and Portainer deployment execution and preflight."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import hashlib
import re
from pathlib import Path

from llmproxy.core.env import (
    command_environment,
    load_env_file,
    operational_environment,
    write_ptctools_env_file,
)
from llmproxy.core.process import command_display_text
from llmproxy.deployment.compose import (
    ROOT,
    DeploymentError,
    compose_files_for_preset,
    render_preset,
    resolve_components,
    selected_stack_names,
)


def preflight_preset(
    preset_name: str,
    env: dict[str, str],
    *,
    stack: str | None = None,
    root: Path = ROOT,
    check_files: bool = True,
    include_local_overrides: bool = True,
) -> None:
    selected_stacks = set(selected_stack_names(preset_name, stack, root))
    components = [
        component
        for component in resolve_components(preset_name, root)
        if component.stack in selected_stacks
    ]
    required_files: set[Path] = set()
    required_env: set[str] = set()
    for component in components:
        required_files.update(component.required_files)
        required_env.update(component.required_environment)
    for paths in compose_files_for_preset(
        preset_name,
        stack=stack,
        include_local_overrides=include_local_overrides,
        root=root,
    ).values():
        for path in paths:
            for match in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", path.read_text()):
                required_env.add(match.group(1))

    if check_files:
        missing_files = sorted(
            path for path in required_files if not path.is_file()
        )
        if missing_files:
            raise DeploymentError(
                "Missing files required by preset "
                f"'{preset_name}': {', '.join(str(path) for path in missing_files)}"
            )
    missing_env = sorted(name for name in required_env if not env.get(name))
    if missing_env:
        raise DeploymentError(
            "Missing environment variables required by preset "
            f"'{preset_name}': {', '.join(missing_env)}"
        )


def _docker_command(context: str | None, *args: str) -> list[str]:
    command = ["docker"]
    if context:
        command.extend(["--context", context])
    command.extend(args)
    return command


def _command_text(command: list[str]) -> str:
    return command_display_text(command)


def _run_command(
    command: list[str],
    *,
    env: dict[str, str],
    check: bool = True,
    root: Path = ROOT,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command,
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise DeploymentError(
            result.stderr.strip()
            or result.stdout.strip()
            or f"Command failed: {_command_text(command)}"
        )
    return result


def _expected_stack_services(
    preset_name: str, stack: str | None = None, root: Path = ROOT
) -> dict[str, set[str]]:
    selected_stacks = selected_stack_names(preset_name, stack, root)
    components = resolve_components(preset_name, root)
    expected: dict[str, set[str]] = {}
    for stack_name in selected_stacks:
        expected[stack_name] = {
            f"{stack_name}_{service}"
            for component in components
            if component.stack == stack_name
            for service in component.services
        }
    return expected


def content_addressed_config_name(base_name: str, content: bytes) -> str:
    return f"{base_name}-{hashlib.sha256(content).hexdigest()[:12]}"


def _assert_no_unapproved_service_removals(
    preset_name: str,
    *,
    stack: str | None = None,
    context: str | None,
    env: dict[str, str],
    allow_remove_services: bool,
    root: Path = ROOT,
) -> None:
    if allow_remove_services:
        return
    for stack_name, expected in _expected_stack_services(preset_name, stack, root).items():
        command = _docker_command(
            context,
            "stack",
            "services",
            stack_name,
            "--format",
            "{{.Name}}",
        )
        result = _run_command(command, env=env, check=False, root=root)
        if result.returncode:
            message = (result.stderr or result.stdout).strip()
            missing_stack = any(
                re.fullmatch(pattern, message, flags=re.IGNORECASE)
                for pattern in (
                    rf"nothing found in stack:\s*{re.escape(stack_name)}",
                    rf"no such stack:\s*{re.escape(stack_name)}",
                )
            )
            if missing_stack:
                continue
            raise DeploymentError(
                f"Cannot inspect service inventory for stack '{stack_name}': "
                f"{message or 'unknown Docker error'}"
            )
        current = {
            line.strip() for line in result.stdout.splitlines() if line.strip()
        }
        removed = sorted(current - expected)
        if removed:
            raise DeploymentError(
                "Deployment would remove services: "
                f"{', '.join(removed)}. Re-run with --allow-remove-services after review."
            )


def deploy_preset(
    preset_name: str,
    *,
    stack: str | None = None,
    driver: str = "docker",
    docker_context: str | None = None,
    env_file: Path | None = None,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
    allow_remove_services: bool = False,
    skip_file_preflight: bool = False,
    include_local_overrides: bool = True,
    root: Path = ROOT,
) -> list[str]:
    if driver not in {"docker", "ptctools"}:
        raise DeploymentError(f"Unknown deploy driver: {driver}")
    if driver == "ptctools" and not allow_remove_services:
        raise DeploymentError(
            "ptctools cannot inspect remote service inventory; "
            "use --driver docker or explicitly pass --allow-remove-services"
        )
    selected_stacks = selected_stack_names(preset_name, stack, root)
    selected_components = [
        component
        for component in resolve_components(preset_name, root)
        if component.stack in selected_stacks
    ]
    if env is not None:
        deploy_env = dict(env)
    elif env_file is not None:
        deploy_env = load_env_file(env_file)
    else:
        deploy_env = load_env_file(root / ".env")
    preflight_preset(
        preset_name,
        deploy_env,
        stack=stack,
        check_files=not skip_file_preflight,
        include_local_overrides=include_local_overrides,
        root=root,
    )

    commands: list[list[str]] = []
    config_env: dict[str, str] = {}
    config_specs = [
        spec
        for component in selected_components
        for spec in component.docker_configs
    ]
    for spec in config_specs:
        source = spec.source
        if not source.is_file():
            if skip_file_preflight:
                continue
            raise DeploymentError(f"Missing Docker config source: {source}")
        config_name = content_addressed_config_name(spec.name, source.read_bytes())
        config_env[spec.environment] = config_name
        if driver == "docker":
            commands.append(
                _docker_command(
                    docker_context, "config", "create", config_name, str(source)
                )
            )
        else:
            commands.append(
                [
                    "uvx",
                    "ptctools",
                    "docker",
                    "config",
                    "set",
                    "-n",
                    config_name,
                    "-f",
                    str(source),
                    "--ownership",
                    "team",
                ]
            )
    deploy_env.update(config_env)

    outputs = render_preset(
        preset_name,
        output_dir=root / "build" / preset_name,
        stack=stack,
        include_local_overrides=include_local_overrides,
        env=deploy_env,
        root=root,
    )
    operational_env = operational_environment(deploy_env, driver=driver)
    stack_envs = {
        stack_name: command_environment(
            {stack_name: output_path}, deploy_env, driver
        )
        for stack_name, output_path in outputs.items()
    }
    for stack_name in selected_stacks:
        if stack_name not in outputs:
            continue
        if driver == "docker":
            deploy_args = ["stack", "deploy"]
            if allow_remove_services:
                deploy_args.append("--prune")
            deploy_args.extend(["-c", str(outputs[stack_name]), stack_name])
            commands.append(_docker_command(docker_context, *deploy_args))
        else:
            commands.append(
                [
                    "uvx",
                    "ptctools",
                    "docker",
                    "stack",
                    "deploy",
                    "-n",
                    stack_name,
                    "-f",
                    str(outputs[stack_name]),
                    "--env-file",
                    f"__PTCTOOLS_ENV__:{stack_name}",
                    "--ownership",
                    "team",
                ]
            )

    command_texts = [_command_text(command) for command in commands]
    if dry_run:
        if driver == "ptctools":
            if not shutil.which("uvx"):
                raise DeploymentError(
                    "ptctools driver requires uvx; use --driver docker instead"
                )
        return command_texts

    if driver == "ptctools" and not shutil.which("uvx"):
        raise DeploymentError(
            "ptctools driver requires uvx; use --driver docker instead"
        )

    if driver == "docker":
        manager = _run_command(
            _docker_command(
                docker_context, "info", "--format", "{{.Swarm.ControlAvailable}}"
            ),
            env=operational_env,
            root=root,
        )
        if manager.stdout.strip().lower() != "true":
            raise DeploymentError("Docker target is not a Swarm manager")
        _assert_no_unapproved_service_removals(
            preset_name,
            stack=stack,
            context=docker_context,
            env=operational_env,
            allow_remove_services=allow_remove_services,
            root=root,
        )

    temp_env_dir = (
        tempfile.TemporaryDirectory(prefix="llmproxy-stack-env-")
        if driver == "ptctools"
        else None
    )
    try:
        for command in commands:
            resolved_command = list(command)
            run_env = operational_env
            for index, item in enumerate(resolved_command):
                if not item.startswith("__PTCTOOLS_ENV__:"):
                    continue
                stack_name = item.split(":", 1)[1]
                if temp_env_dir is None:
                    raise DeploymentError(
                        "Internal error: missing ptctools env directory"
                    )
                env_path = Path(temp_env_dir.name) / f"{stack_name}.env"
                write_ptctools_env_file(
                    env_path,
                    outputs[stack_name].read_text(),
                    stack_envs[stack_name],
                )
                resolved_command[index] = str(env_path)
            if (
                driver == "docker"
                and "stack" in resolved_command
                and "deploy" in resolved_command
            ):
                run_env = stack_envs[resolved_command[-1]]
            if (
                driver == "docker"
                and "config" in resolved_command
                and "create" in resolved_command
            ):
                config_name = resolved_command[-2]
                inventory_command = _docker_command(
                    docker_context,
                    "config",
                    "ls",
                    "--filter",
                    f"name={config_name}",
                    "--format",
                    "{{.Name}}",
                )
                inspected = _run_command(
                    inventory_command,
                    env=operational_env,
                    check=False,
                    root=root,
                )
                if inspected.returncode:
                    message = (inspected.stderr or inspected.stdout or "").strip()
                    raise DeploymentError(
                        f"Cannot inspect Docker config '{config_name}': "
                        f"{message or 'unknown error'}"
                    )
                existing_names = {
                    line.strip()
                    for line in inspected.stdout.splitlines()
                    if line.strip()
                }
                if config_name in existing_names:
                    continue
            if (
                driver == "ptctools"
                and "config" in resolved_command
                and "set" in resolved_command
            ):
                config_name = resolved_command[resolved_command.index("-n") + 1]
                inspect_command = [
                    "uvx",
                    "ptctools",
                    "docker",
                    "config",
                    "get",
                    "-n",
                    config_name,
                ]
                inspected = _run_command(
                    inspect_command,
                    env=operational_env,
                    check=False,
                    root=root,
                )
                if inspected.returncode == 0:
                    continue
                message = inspected.stderr or inspected.stdout or ""
                not_found = re.fullmatch(
                    rf"Error: Config '{re.escape(config_name)}' not found",
                    message.strip(),
                    flags=re.IGNORECASE,
                )
                if not not_found:
                    raise DeploymentError(
                        f"Cannot inspect Portainer config '{config_name}': "
                        f"{message.strip() or 'unknown error'}"
                    )
            _run_command(resolved_command, env=run_env, root=root)
    finally:
        if temp_env_dir is not None:
            temp_env_dir.cleanup()

    if driver == "docker":
        for (
            stack_name,
            expected,
        ) in _expected_stack_services(preset_name, stack, root).items():
            result = _run_command(
                _docker_command(
                    docker_context,
                    "stack",
                    "services",
                    stack_name,
                    "--format",
                    "{{.Name}}",
                ),
                env=operational_env,
                root=root,
            )
            actual = {
                line.strip() for line in result.stdout.splitlines() if line.strip()
            }
            missing = sorted(expected - actual)
            if missing:
                raise DeploymentError(
                    f"Deployment verification failed; missing services: {', '.join(missing)}"
                )
    return command_texts
