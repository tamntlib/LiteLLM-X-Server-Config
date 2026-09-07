"""Load component-owned Python libraries in application-root-local namespaces."""

from __future__ import annotations

import hashlib
import importlib
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from importlib.util import spec_from_file_location
import re
import sys
from pathlib import Path
from types import ModuleType

from llmproxy.core.command import CommandError


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class _ComponentFinder(MetaPathFinder):
    """Confine relative imports to the same component, including sibling modules."""

    def __init__(self):
        self.roots: dict[str, Path] = {}

    def find_spec(self, fullname, path=None, target=None):
        namespace, separator, relative = fullname.partition(".")
        directory = self.roots.get(namespace)
        if not separator or directory is None:
            return None
        parts = relative.split(".")
        if not all(part.isidentifier() for part in parts):
            raise CommandError(f"Invalid component module name: {relative}")
        candidate = directory.joinpath(*parts)
        module_path = candidate / "__init__.py" if candidate.is_dir() else candidate.with_suffix(".py")
        for parent in (module_path, *module_path.parents):
            if parent.is_symlink():
                raise CommandError(f"Component module path contains a symlink: {parent}")
            if parent == directory:
                break
        if module_path.is_file():
            return spec_from_file_location(fullname, module_path)
        if candidate.is_dir():
            spec = ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [str(candidate)]
            return spec
        raise ModuleNotFoundError(f"Component module not found: {relative}", name=fullname)


_finder = _ComponentFinder()
sys.meta_path.insert(0, _finder)


def _component_directory(root: Path, identifier: str) -> Path:
    parts = identifier.split("/")
    if len(parts) != 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        raise CommandError(f"Invalid component identifier: {identifier}")
    directory = root.absolute() / "components" / parts[0] / parts[1]
    for parent in (directory, *directory.parents):
        if parent.is_symlink():
            raise CommandError(f"Component module path contains a symlink: {parent}")
    compose = directory / "compose.yaml"
    if not compose.is_file() or compose.is_symlink():
        raise CommandError(f"Component not found: {identifier}")
    return directory.resolve()


def load_component_module(root: Path, identifier: str, module_name: str) -> ModuleType:
    """Load an owned library only when explicitly requested, never via sys.path."""
    parts = module_name.split(".")
    if not parts or not all(part.isidentifier() for part in parts):
        raise CommandError(f"Invalid component module name: {module_name}")
    directory = _component_directory(Path(root), identifier)
    path = directory.joinpath(*parts)
    target = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
    for parent in (target, *target.parents):
        if parent.is_symlink():
            raise CommandError(f"Component module path contains a symlink: {parent}")
        if parent == directory:
            break
    if not target.is_file() and not path.is_dir():
        raise CommandError(f"Component module not found: {identifier}:{module_name}")

    namespace = "_llmproxy_owned_" + hashlib.sha256(str(directory).encode()).hexdigest()
    prefix = namespace + "."
    existing = {name for name in sys.modules if name == namespace or name.startswith(prefix)}
    _finder.roots[namespace] = directory
    if namespace not in sys.modules:
        package = ModuleType(namespace)
        package.__package__ = namespace
        package.__path__ = [str(directory)]
        package.__spec__ = ModuleSpec(namespace, loader=None, is_package=True)
        package.__spec__.submodule_search_locations = package.__path__
        sys.modules[namespace] = package
    try:
        return importlib.import_module(prefix + module_name)
    except Exception as exc:
        for name in list(sys.modules):
            if (name == namespace or name.startswith(prefix)) and name not in existing:
                del sys.modules[name]
        if namespace not in sys.modules:
            _finder.roots.pop(namespace, None)
        if isinstance(exc, ImportError):
            raise CommandError(f"Cannot import component module: {identifier}:{module_name}") from exc
        raise
