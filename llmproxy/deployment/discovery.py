"""Filesystem discovery for self-contained deployment components."""

from __future__ import annotations

import tomllib
from pathlib import Path

from llmproxy.deployment.component import Component, DockerConfigSpec


class ComponentError(RuntimeError):
    """Raised when a component does not satisfy the filesystem contract."""


def _mapping_keys(text: str, section: str) -> tuple[str, ...]:
    lines = text.splitlines()
    in_section = False
    keys: list[str] = []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0:
            in_section = stripped == f"{section}:"
            continue
        if in_section and indent == 2 and ":" in stripped:
            key = stripped.split(":", 1)[0].strip().strip("'\"")
            if key:
                keys.append(key)
    return tuple(keys)


def _string_tuple(metadata: dict, name: str) -> tuple[str, ...]:
    value = metadata.get(name, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ComponentError(f"{name} must be a list of strings")
    return tuple(value)


def _owned_path(directory: Path, value: str, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ComponentError(f"{field} must be a non-empty relative path")
    root = directory.resolve()
    candidate = (directory / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ComponentError(f"{field} must stay inside component directory: {value}") from exc
    return candidate


def load_component(directory: Path, root: Path) -> Component:
    components_root = (root / "components").resolve()
    if directory.is_symlink() or directory.parent.is_symlink():
        raise ComponentError(f"Component directories must not be symlinks: {directory}")
    directory = directory.resolve()
    try:
        relative = directory.relative_to(components_root)
    except ValueError as exc:
        raise ComponentError(f"Component is outside components/: {directory}") from exc
    if len(relative.parts) != 2:
        raise ComponentError(
            f"Component path must be components/<stack>/<component>: {directory}"
        )
    stack, name = relative.parts
    compose_file = _owned_path(directory, "compose.yaml", "compose_file")
    if not compose_file.is_file():
        raise ComponentError(f"Missing component compose file: {compose_file}")
    services = _mapping_keys(compose_file.read_text(), "services")
    if not services:
        raise ComponentError(f"Component has no services: {compose_file}")

    metadata_path = _owned_path(directory, "component.toml", "component_metadata")
    metadata = tomllib.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    docker_configs: list[DockerConfigSpec] = []
    for item in metadata.get("docker_configs", []):
        if not isinstance(item, dict):
            raise ComponentError(f"Invalid docker_configs entry in {metadata_path}")
        try:
            source = _owned_path(directory, item["source"], "docker_configs.source")
            if not isinstance(item["name"], str) or not isinstance(item["environment"], str):
                raise ComponentError("docker_configs name/environment must be strings")
            docker_configs.append(
                DockerConfigSpec(
                    source=source,
                    name=item["name"],
                    environment=item["environment"],
                )
            )
        except KeyError as exc:
            raise ComponentError(
                f"Missing docker_configs field {exc.args[0]} in {metadata_path}"
            ) from exc

    overlays_dir = directory / "overlays"
    if overlays_dir.is_symlink():
        raise ComponentError(f"Overlay directory must not be a symlink: {overlays_dir}")
    features = tuple(
        _owned_path(directory, str(path.relative_to(directory)), "overlay").stem
        for path in sorted(overlays_dir.glob("*.yaml"))
    ) if overlays_dir.is_dir() else ()
    commands_dir = directory / "commands"
    if commands_dir.is_symlink():
        raise ComponentError(f"Commands directory must not be a symlink: {commands_dir}")
    commands = tuple(
        _owned_path(directory, str(path.relative_to(directory)), "command")
        for path in sorted(commands_dir.glob("*.py")) if path.name != "__init__.py"
    ) if commands_dir.is_dir() else ()

    return Component(
        identifier=f"{stack}/{name}",
        stack=stack,
        name=name,
        directory=directory,
        compose_file=compose_file,
        services=services,
        requires=_string_tuple(metadata, "requires"),
        after=_string_tuple(metadata, "after"),
        required_files=tuple(
            _owned_path(directory, path, "required_files")
            for path in _string_tuple(metadata, "required_files")
        ),
        required_environment=_string_tuple(metadata, "required_environment"),
        required_features=_string_tuple(metadata, "required_features"),
        docker_configs=tuple(docker_configs),
        features=features,
        commands=commands,
    )


def discover_components(root: Path) -> dict[str, Component]:
    components_root = root / "components"
    discovered: dict[str, Component] = {}
    if not components_root.is_dir():
        return discovered
    if components_root.is_symlink():
        raise ComponentError(f"Components directory must not be a symlink: {components_root}")
    for stack_dir in sorted(path for path in components_root.iterdir() if path.is_dir()):
        if stack_dir.is_symlink():
            raise ComponentError(f"Component stack directories must not be symlinks: {stack_dir}")
        for component_dir in sorted(path for path in stack_dir.iterdir() if path.is_dir()):
            if not (component_dir / "compose.yaml").is_file():
                continue
            component = load_component(component_dir, root)
            if component.identifier in discovered:
                raise ComponentError(f"Duplicate component: {component.identifier}")
            discovered[component.identifier] = component
    return discovered
