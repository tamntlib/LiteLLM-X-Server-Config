"""Convention-based Hatch build hook for public regular files only."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.metadata.plugin.interface import MetadataHookInterface

RESOURCE_ROOTS = ("components", "presets")
PACKAGE_ROOTS = ("llmproxy",)
SDIST_ONLY_ROOTS = ("tests",)
EXCLUDED_PARTS = {"build", "dist", "__pycache__"}
EXCLUDED_NAMES = {"config.gen.json", "openapi.json"}
REQUIRED_BUILD_INPUTS = (
    "pyproject.toml", "hatch_build.py", "README.md", "LICENSE",
    "llmproxy/__init__.py", "llmproxy/core/resources.py",
)
# Hatch's default license globs and automatic sdist/configuration inputs bypass
# _regular_files too. Check existing matches without following their symlinks.
IMPLICIT_BUILD_INPUT_GLOBS = (
    "hatch.toml", ".gitignore", ".hgignore",
    "LICEN[CS]E*", "COPYING*", "NOTICE*", "AUTHORS*",
)


def _validate_build_inputs(project_root: Path) -> None:
    """Reject private, missing, non-regular, or symlinked build inputs."""
    names: set[str] = set(REQUIRED_BUILD_INPUTS)
    names.update(
        path.name
        for pattern in IMPLICIT_BUILD_INPUT_GLOBS
        for path in project_root.glob(pattern)
    )
    for name in sorted(names):
        relative = Path(name)
        if _is_private_or_generated(relative):
            raise ValueError(f"Unsafe build input: {name} (private or generated)")
        current = project_root
        for index, part in enumerate(relative.parts):
            current /= part
            try:
                mode = current.lstat().st_mode
            except OSError as exc:
                raise ValueError(f"Unsafe build input: {name}") from exc
            expected = stat.S_ISREG if index == len(relative.parts) - 1 else stat.S_ISDIR
            if not expected(mode):
                raise ValueError(f"Unsafe build input: {name} (not a regular file/directory)")


class CustomMetadataHook(MetadataHookInterface):
    """Validate before Hatch reads README content or collects license metadata."""

    def __init__(self, root: str, config: dict) -> None:
        super().__init__(root, config)
        # Hatch instantiates metadata hooks even for entirely static metadata;
        # update() alone would not run. Build initialize() runs too late here.
        # The configuration and this hook are already loaded by Hatch, so they
        # remain trusted code, not a sandbox for an attacker-controlled backend.
        _validate_build_inputs(Path(root))

    def update(self, metadata: dict) -> None:
        _validate_build_inputs(Path(self.root))


def _is_private_or_generated(relative: Path, *, allow_compose_example: bool = False) -> bool:
    if relative.name in EXCLUDED_NAMES or relative.suffix in {".pyc", ".pyo"}:
        return True
    for index, part in enumerate(relative.parts):
        if part in EXCLUDED_PARTS:
            return True
        if part == ".env" or part.startswith(".env.") or part.startswith(".env"):
            return True
        if ".local" in part:
            if (
                allow_compose_example
                and index == len(relative.parts) - 1
                and part == "compose.local.example.yaml"
            ):
                continue
            return True
    return False


def _regular_files(root: Path):
    """Yield regular files without traversing or including any symlink."""
    if not root.is_dir() or root.is_symlink():
        return
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name
            for name in directories
            if not (current_path / name).is_symlink()
            and not _is_private_or_generated((current_path / name).relative_to(root))
        ]
        for name in files:
            source = current_path / name
            relative = source.relative_to(root)
            if (
                source.is_symlink()
                or not source.is_file()
                or _is_private_or_generated(relative, allow_compose_example=True)
            ):
                continue
            yield source, relative


class CustomBuildHook(BuildHookInterface):
    """Select package and component resources through one fail-closed policy."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        project_root = Path(self.root)
        # Recheck if a caller cached metadata before changing source paths.
        _validate_build_inputs(project_root)
        force_include = build_data.setdefault("force_include", {})

        # Hatch copies force-included files even into editable wheels. A copied
        # package shadows the checkout on its .pth path, leaving CLI code stale.
        # Let Hatch expose live source; keep public resource packaging unchanged.
        editable = self.target_name == "wheel" and version == "editable"
        roots: list[str] = [*RESOURCE_ROOTS] if editable else [*PACKAGE_ROOTS, *RESOURCE_ROOTS]
        if self.target_name == "sdist":
            roots.extend(SDIST_ONLY_ROOTS)

        for root_name in roots:
            source_root = project_root / root_name
            for source, relative in _regular_files(source_root):
                if self.target_name == "wheel" and root_name in RESOURCE_ROOTS:
                    destination = Path("llmproxy/resources") / root_name / relative
                else:
                    destination = Path(root_name) / relative
                force_include[str(source)] = destination.as_posix()
