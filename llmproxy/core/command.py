"""Command contracts and filesystem/package discovery."""

from __future__ import annotations

import importlib
import importlib.util
import ast
import pkgutil
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable


class CommandError(RuntimeError):
    """Raised for invalid command definitions or execution input."""


@dataclass(frozen=True)
class CommandSpec:
    path: tuple[str, ...]
    description: str
    configure: Callable
    run: Callable
    mutating: bool = False
    requires_confirmation: bool = False
    source: str = ""
    component_identifier: str | None = None
    lazy: bool = False


def _command_path(value: object) -> tuple[str, ...]:
    if isinstance(value, str) and value:
        return (value,)
    if isinstance(value, (tuple, list)) and value and all(isinstance(item, str) and item for item in value):
        return tuple(value)
    raise CommandError("COMMAND must be a non-empty string or sequence of strings")


def command_spec_from_module(module: ModuleType, *, prefix: tuple[str, ...] = ()) -> CommandSpec:
    try:
        command = _command_path(module.COMMAND)
        description = str(module.DESCRIPTION)
        configure = module.configure
        run = module.run
    except AttributeError as exc:
        raise CommandError(f"Incomplete command contract in {module.__name__}: {exc}") from exc
    if not callable(configure) or not callable(run):
        raise CommandError(f"Command configure/run must be callable in {module.__name__}")
    return CommandSpec(
        path=(*prefix, *command),
        description=description,
        configure=configure,
        run=run,
        mutating=bool(getattr(module, "MUTATING", False)),
        requires_confirmation=bool(getattr(module, "REQUIRES_CONFIRMATION", False)),
        source=getattr(module, "__file__", module.__name__) or module.__name__,
        component_identifier=getattr(module, "COMPONENT_IDENTIFIER", None),
    )


def discover_package_commands(package_name: str = "llmproxy.commands") -> list[CommandSpec]:
    package = importlib.import_module(package_name)
    specs: list[CommandSpec] = []
    for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        if info.ispkg or ".pipeline." in info.name:
            continue
        module = importlib.import_module(info.name)
        if hasattr(module, "COMMAND"):
            specs.append(command_spec_from_module(module))
    return specs


def _load_file_module(path: Path, identifier: str) -> ModuleType:
    module_name = "_llmproxy_component_" + identifier.replace("/", "_").replace("-", "_") + "_" + path.stem
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise CommandError(f"Cannot load component command: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _component_command_metadata(path: Path) -> dict[str, object]:
    wanted = {
        "COMMAND",
        "ALIASES",
        "DESCRIPTION",
        "MUTATING",
        "REQUIRES_CONFIRMATION",
    }
    values: dict[str, object] = {}
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise CommandError(f"Cannot parse component command metadata: {path}: {exc}") from exc
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value_node = node.value
        if value_node is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name) or target.id not in wanted:
                continue
            try:
                values[target.id] = ast.literal_eval(value_node)
            except (ValueError, TypeError) as exc:
                raise CommandError(
                    f"Component command metadata {target.id} must be a literal in {path}"
                ) from exc
    missing = {"COMMAND", "DESCRIPTION"} - values.keys()
    if missing:
        raise CommandError(
            f"Incomplete command metadata in {path}: missing {', '.join(sorted(missing))}"
        )
    return values


def _lazy_component_spec(
    path: Path,
    component_identifier: str,
    command_path: tuple[str, ...],
    metadata: dict[str, object],
) -> CommandSpec:
    loaded: list[ModuleType] = []

    def module() -> ModuleType:
        if not loaded:
            instance = _load_file_module(path, component_identifier)
            setattr(instance, "COMPONENT_IDENTIFIER", component_identifier)
            loaded.append(instance)
        return loaded[0]

    def configure(parser):
        command_module = module()
        if not callable(getattr(command_module, "configure", None)):
            raise CommandError(f"Command configure must be callable in {path}")
        return command_module.configure(parser)

    def run(args, context):
        command_module = module()
        if not callable(getattr(command_module, "run", None)):
            raise CommandError(f"Command run must be callable in {path}")
        return command_module.run(args, context)

    return CommandSpec(
        path=command_path,
        description=str(metadata["DESCRIPTION"]),
        configure=configure,
        run=run,
        mutating=bool(metadata.get("MUTATING", False)),
        requires_confirmation=bool(metadata.get("REQUIRES_CONFIRMATION", False)),
        source=str(path),
        component_identifier=component_identifier,
        lazy=True,
    )


def discover_component_commands(root: Path) -> list[CommandSpec]:
    from llmproxy.deployment.discovery import discover_components

    specs: list[CommandSpec] = []
    for component in discover_components(root).values():
        for path in component.commands:
            metadata = _component_command_metadata(path)
            command_path = _command_path(metadata["COMMAND"])
            command = _lazy_component_spec(
                path,
                component.identifier,
                (component.identifier, *command_path),
                metadata,
            )
            specs.append(command)
            aliases = metadata.get("ALIASES", ())
            if not isinstance(aliases, (tuple, list)):
                raise CommandError(f"ALIASES must be a sequence in {path}")
            for alias in aliases:
                specs.append(
                    _lazy_component_spec(
                        path,
                        component.identifier,
                        _command_path(alias),
                        metadata,
                    )
                )
    return specs


def discover_commands(root: Path) -> list[CommandSpec]:
    specs = [*discover_package_commands(), *discover_component_commands(root)]
    by_path: dict[tuple[str, ...], CommandSpec] = {}
    for spec in specs:
        if spec.path in by_path:
            raise CommandError(
                f"Duplicate command {' '.join(spec.path)} from {by_path[spec.path].source} and {spec.source}"
            )
        by_path[spec.path] = spec
    return [by_path[path] for path in sorted(by_path)]
