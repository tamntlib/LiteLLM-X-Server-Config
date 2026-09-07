"""Compose assembly and rendering for component-first presets."""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

from llmproxy.core.env import operational_environment
from llmproxy.core.resources import resource_root
from llmproxy.core.resources import write_private_text
from llmproxy.deployment.component import Component
from llmproxy.deployment.discovery import ComponentError, discover_components
from llmproxy.deployment.preset import PresetError, resolve_preset

ROOT = resource_root()


class DeploymentError(RuntimeError):
    """Public error for invalid deployment definitions or render failures."""


def _owned_stack_path(root: Path, stack: str, filename: str) -> Path:
    stacks_root = (root / "components").resolve()
    stack_directory = root / "components" / stack
    if stack_directory.is_symlink():
        raise DeploymentError(f"Stack directory must not be a symlink: {stack_directory}")
    resolved_stack = stack_directory.resolve()
    try:
        resolved_stack.relative_to(stacks_root)
    except ValueError as exc:
        raise DeploymentError(f"Stack directory escapes components/: {stack_directory}") from exc
    candidate = resolved_stack / filename
    if candidate.is_symlink():
        raise DeploymentError(f"Stack file must not be a symlink: {candidate}")
    path = candidate.resolve()
    try:
        path.relative_to(resolved_stack)
    except ValueError as exc:
        raise DeploymentError(f"Stack file escapes its directory: {path}") from exc
    return path


def _stack_order(root: Path = ROOT) -> tuple[str, ...]:
    entries: list[tuple[int, str]] = []
    stacks_root = root / "components"
    if not stacks_root.is_dir():
        return ()
    if stacks_root.is_symlink():
        raise DeploymentError(f"Components directory must not be a symlink: {stacks_root}")
    for directory in sorted(path for path in stacks_root.iterdir() if path.is_dir()):
        if directory.is_symlink():
            raise DeploymentError(f"Stack directory must not be a symlink: {directory}")
        metadata_path = _owned_stack_path(root, directory.name, "stack.toml")
        metadata = tomllib.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
        entries.append((int(metadata.get("order", 1000)), directory.name))
    return tuple(name for _, name in sorted(entries))


def resolve_components(preset_name: str, root: Path = ROOT) -> tuple[Component, ...]:
    try:
        preset = resolve_preset(preset_name, root)
        discovered = discover_components(root)
    except (PresetError, ComponentError) as exc:
        raise DeploymentError(str(exc)) from exc
    missing = [identifier for identifier in preset.components if identifier not in discovered]
    if missing:
        raise DeploymentError(
            f"Preset '{preset_name}' references unknown components: {', '.join(missing)}"
        )
    selected = tuple(discovered[identifier] for identifier in preset.components)
    selected_ids = {component.identifier for component in selected}
    enabled_features = set(preset.features)
    for component in selected:
        missing_dependencies = [item for item in component.requires if item not in selected_ids]
        if missing_dependencies:
            raise DeploymentError(
                f"Component {component.identifier} requires: {', '.join(missing_dependencies)}"
            )
        missing_features = [
            feature for feature in component.required_features if feature not in enabled_features
        ]
        if missing_features:
            raise DeploymentError(
                f"Component {component.identifier} requires features: {', '.join(missing_features)}"
            )
    by_id = {component.identifier: component for component in selected}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise DeploymentError(f"Component dependency cycle includes: {identifier}")
        if identifier in visited:
            return
        visiting.add(identifier)
        component = by_id[identifier]
        for dependency in (*component.requires, *component.after):
            if dependency in by_id:
                visit(dependency)
        visiting.remove(identifier)
        visited.add(identifier)

    for component in selected:
        visit(component.identifier)
    return selected


def selected_stack_names(
    preset_name: str,
    stack: str | None = None,
    root: Path = ROOT,
) -> tuple[str, ...]:
    components = resolve_components(preset_name, root)
    by_id = {component.identifier: component for component in components}
    available = {component.stack for component in components}
    declared = _stack_order(root)
    priority = {name: index for index, name in enumerate(declared)}
    fallback = len(priority)
    edges = {name: set() for name in available}
    incoming = {name: 0 for name in available}
    for component in components:
        for dependency in (*component.requires, *component.after):
            target = by_id.get(dependency)
            if target is None or target.stack == component.stack:
                continue
            if component.stack not in edges[target.stack]:
                edges[target.stack].add(component.stack)
                incoming[component.stack] += 1

    ready = sorted(
        (name for name, count in incoming.items() if count == 0),
        key=lambda name: (priority.get(name, fallback), name),
    )
    ordered_stacks: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered_stacks.append(current)
        for dependent in sorted(edges[current]):
            incoming[dependent] -= 1
            if incoming[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=lambda name: (priority.get(name, fallback), name))
    if len(ordered_stacks) != len(available):
        raise DeploymentError("Stack dependency cycle detected")
    stacks = tuple(ordered_stacks)
    if stack is None:
        return stacks
    if stack not in available:
        choices = ", ".join(stacks)
        raise DeploymentError(
            f"Stack '{stack}' is not part of preset '{preset_name}'. Available: {choices}"
        )
    return (stack,)


def compose_files_for_preset(
    preset_name: str,
    *,
    stack: str | None = None,
    include_local_overrides: bool = True,
    root: Path = ROOT,
) -> dict[str, list[Path]]:
    preset = resolve_preset(preset_name, root)
    components = resolve_components(preset_name, root)
    selected_stacks = selected_stack_names(preset_name, stack, root)
    files: dict[str, list[Path]] = {}
    for stack_name in selected_stacks:
        base = _owned_stack_path(root, stack_name, "compose.yaml")
        if base.exists() and not base.is_file():
            raise DeploymentError(f"Stack compose file must be a regular file: {base}")
        files[stack_name] = [base] if base.is_file() else []

    for component in components:
        if component.stack in files:
            files[component.stack].append(component.compose_file)

    for feature in preset.features:
        for component in components:
            if component.stack not in files:
                continue
            overlay = component.overlay(feature)
            if overlay:
                files[component.stack].append(overlay)

    if include_local_overrides:
        for stack_name, paths in local_override_paths_for_preset(
            preset_name,
            stack=stack,
            root=root,
        ).items():
            files[stack_name].extend(paths)
    return files


def local_override_paths_for_preset(
    preset_name: str,
    *,
    stack: str | None = None,
    root: Path = ROOT,
) -> dict[str, list[Path]]:
    selected_stacks = selected_stack_names(preset_name, stack, root)
    paths = {stack_name: [] for stack_name in selected_stacks}
    for component in resolve_components(preset_name, root):
        if component.stack not in paths:
            continue
        local = component.local_compose_file
        if local:
            paths[component.stack].append(local)
    return paths


def docker_stack_config(
    compose_files: list[Path],
    env: dict[str, str] | None = None,
    root: Path = ROOT,
) -> str:
    command = ["docker", "stack", "config", "--skip-interpolation"]
    for compose_file in compose_files:
        command.extend(["-c", str(compose_file)])
    result = subprocess.run(
        command,
        cwd=root,
        env=operational_environment(env or dict(os.environ), driver="docker"),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise DeploymentError(result.stderr.strip() or "docker stack config failed")
    return result.stdout


def render_preset(
    preset_name: str,
    output_dir: Path | None = None,
    *,
    stack: str | None = None,
    include_local_overrides: bool = True,
    env: dict[str, str] | None = None,
    root: Path = ROOT,
) -> dict[str, Path]:
    default_output = output_dir is None
    output_dir = output_dir or root / "build" / preset_name
    outputs: dict[str, Path] = {}
    for stack_name, compose_files in compose_files_for_preset(
        preset_name,
        stack=stack,
        include_local_overrides=include_local_overrides,
        root=root,
    ).items():
        output_path = output_dir / f"{stack_name}.yaml"
        output_path = write_private_text(
            output_path,
            docker_stack_config(compose_files, env=env, root=root),
            confinement_root=root if default_output else None,
        )
        outputs[stack_name] = output_path
    return outputs


def validate_preset(
    preset_name: str,
    *,
    stack: str | None = None,
    include_local_overrides: bool = True,
    root: Path = ROOT,
) -> None:
    for files in compose_files_for_preset(
        preset_name,
        stack=stack,
        include_local_overrides=include_local_overrides,
        root=root,
    ).values():
        docker_stack_config(files, root=root)
