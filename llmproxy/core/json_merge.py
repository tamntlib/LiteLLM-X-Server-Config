"""Generic JSON loading and deterministic deep-merge utilities."""

from __future__ import annotations

import json
from pathlib import Path

_DELETE_KEYS = "$delete"


def load_json(path: Path) -> dict:
    with path.open() as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key == _DELETE_KEYS:
            continue
        if isinstance(value, dict):
            existing = result.get(key, {})
            result[key] = deep_merge(existing if isinstance(existing, dict) else {}, value)
        else:
            result[key] = value
    delete_keys = override.get(_DELETE_KEYS, [])
    if isinstance(delete_keys, list):
        for key in delete_keys:
            if isinstance(key, str):
                result.pop(key, None)
    return result


def load_with_local(path: Path) -> tuple[dict, dict]:
    base = load_json(path)
    local_path = path.with_name(path.stem + ".local" + path.suffix)
    if not local_path.is_file():
        return base, base
    return deep_merge(base, load_json(local_path)), base
