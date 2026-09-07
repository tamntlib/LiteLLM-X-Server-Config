"""Execution contexts passed to generic and component commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType


@dataclass(frozen=True)
class AppContext:
    root: Path


@dataclass(frozen=True)
class ComponentContext(AppContext):
    stack_name: str
    component_name: str
    component_dir: Path

    def load_module(self, name: str) -> ModuleType:
        from llmproxy.core.component_modules import load_component_module

        return load_component_module(
            self.root, f"{self.stack_name}/{self.component_name}", name
        )
