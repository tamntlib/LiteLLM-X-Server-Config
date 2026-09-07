"""Include component-owned suites in the standard repository test command."""

from pathlib import Path
import unittest

from llmproxy.core.component_modules import load_component_module
from llmproxy.deployment.discovery import discover_components


def component_test_suite(root, loader, pattern="test_*.py"):
    suite = unittest.TestSuite()
    for component in discover_components(root).values():
        directory = component.directory / "tests"
        for path in sorted(directory.glob(pattern)):
            module = load_component_module(root, component.identifier, f"tests.{path.stem}")
            suite.addTests(loader.loadTestsFromModule(module))
    return suite


def load_tests(loader, standard_tests, pattern):
    root = Path(__file__).resolve().parents[1]
    standard_tests.addTests(component_test_suite(root, loader, pattern or "test_*.py"))
    return standard_tests
