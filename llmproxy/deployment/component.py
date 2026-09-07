"""Generic deployment component and Docker config models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


_FEATURE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class DockerConfigSpec:
    source: Path
    name: str
    environment: str


@dataclass(frozen=True)
class Component:
    identifier: str
    stack: str
    name: str
    directory: Path
    compose_file: Path
    services: tuple[str, ...]
    requires: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    required_files: tuple[Path, ...] = ()
    required_environment: tuple[str, ...] = ()
    required_features: tuple[str, ...] = ()
    docker_configs: tuple[DockerConfigSpec, ...] = ()
    features: tuple[str, ...] = ()
    commands: tuple[Path, ...] = ()

    def overlay(self, feature: str) -> Path | None:
        if not _FEATURE_NAME.fullmatch(feature):
            raise ValueError(f"Invalid feature name: {feature}")
        overlays = (self.directory / "overlays").resolve()
        path = (overlays / f"{feature}.yaml").resolve()
        try:
            path.relative_to(overlays)
        except ValueError as exc:
            raise ValueError(f"Feature overlay escapes component: {feature}") from exc
        return path if path.is_file() else None

    @property
    def local_compose_file(self) -> Path | None:
        root = self.directory.resolve()
        path = (root / "compose.local.yaml").resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("Local Compose file escapes component directory") from exc
        return path if path.is_file() else None
