"""Preset loading, inheritance, and selection resolution."""

from __future__ import annotations

import tomllib
import re
from dataclasses import dataclass
from pathlib import Path


class PresetError(RuntimeError):
    """Raised for invalid or cyclic preset definitions."""


_PRESET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _preset_path(name: str, root: Path) -> Path:
    if not _PRESET_NAME.fullmatch(name):
        raise PresetError(f"Invalid preset name: {name}")
    presets_directory = root / "presets"
    if presets_directory.is_symlink():
        raise PresetError(f"Preset directory must not be a symlink: {presets_directory}")
    presets_root = presets_directory.resolve()
    path = (presets_root / f"{name}.toml").resolve()
    try:
        path.relative_to(presets_root)
    except ValueError as exc:
        raise PresetError(f"Preset path escapes presets directory: {name}") from exc
    return path


@dataclass(frozen=True)
class Preset:
    name: str
    description: str = ""
    extends: str | None = None
    components: tuple[str, ...] = ()
    exclude_components: tuple[str, ...] = ()
    features: tuple[str, ...] = ()


def _string_tuple(data: dict, field: str) -> tuple[str, ...]:
    value = data.get(field, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PresetError(f"Preset field {field} must be a list of strings")
    return tuple(value)


def _duplicates(items: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in items:
        if item in seen and item not in duplicates:
            duplicates.append(item)
        seen.add(item)
    return tuple(duplicates)


def load_preset(name: str, root: Path) -> Preset:
    path = _preset_path(name, root)
    if not path.is_file():
        choices = ", ".join(sorted(item.stem for item in (root / "presets").glob("*.toml")))
        raise PresetError(f"Unknown preset '{name}'. Available: {choices}")
    data = tomllib.loads(path.read_text())
    extends = data.get("extends")
    if extends is not None and not isinstance(extends, str):
        raise PresetError("Preset field extends must be a string")
    exclude_components = _string_tuple(data, "exclude_components")
    duplicate_exclusions = _duplicates(exclude_components)
    if duplicate_exclusions:
        raise PresetError(
            f"Preset '{name}' has duplicate exclude_components: "
            f"{', '.join(duplicate_exclusions)}"
        )
    return Preset(
        name=name,
        description=str(data.get("description", "")),
        extends=extends,
        components=_string_tuple(data, "components"),
        exclude_components=exclude_components,
        features=_string_tuple(data, "features"),
    )


def _dedupe(items: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def resolve_preset(name: str, root: Path, _chain: tuple[str, ...] = ()) -> Preset:
    if name in _chain:
        chain = " -> ".join((*_chain, name))
        raise PresetError(f"Cyclic preset inheritance: {chain}")
    current = load_preset(name, root)
    overlap = sorted(set(current.components) & set(current.exclude_components))
    if overlap:
        raise PresetError(
            f"Preset '{name}' both adds and excludes components: {', '.join(overlap)}"
        )
    base = (
        resolve_preset(current.extends, root, (*_chain, name))
        if current.extends
        else Preset(name=name)
    )
    selected = _dedupe((*base.components, *current.components))
    selected_set = set(selected)
    missing_exclusions = [
        identifier
        for identifier in current.exclude_components
        if identifier not in selected_set
    ]
    if missing_exclusions:
        raise PresetError(
            f"Preset '{name}' excludes components that are not selected: "
            f"{', '.join(missing_exclusions)}"
        )
    excluded = set(current.exclude_components)
    resolved_components = tuple(
        identifier for identifier in selected if identifier not in excluded
    )
    inherited_exclusions = tuple(
        identifier
        for identifier in base.exclude_components
        if identifier not in current.components
    )
    return Preset(
        name=current.name,
        description=current.description or base.description,
        extends=current.extends,
        components=resolved_components,
        exclude_components=_dedupe(
            (*inherited_exclusions, *current.exclude_components)
        ),
        features=_dedupe((*base.features, *current.features)),
    )


def list_presets(root: Path) -> list[Preset]:
    return [load_preset(path.stem, root) for path in sorted((root / "presets").glob("*.toml"))]
