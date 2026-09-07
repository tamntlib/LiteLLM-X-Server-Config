"""Generic component-library loading without service-specific core imports."""

import importlib.util
import tempfile
import unittest
from pathlib import Path


class ComponentModuleTest(unittest.TestCase):
    def test_resource_root_needs_only_components_and_presets(self):
        from llmproxy.core.resources import ResourceError, validate_resource_root

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "components").mkdir()
            (root / "presets").mkdir()
            try:
                validate_resource_root(root)
            except ResourceError as exc:
                self.fail(str(exc))

    def test_orphan_bytecode_is_not_a_public_resource(self):
        from llmproxy.core.resources import is_public_resource

        with tempfile.TemporaryDirectory() as tmpdir:
            for suffix in (".pyc", ".pyo"):
                path = Path(tmpdir) / ("orphan" + suffix)
                path.write_bytes(b"synthetic bytecode canary")
                with self.subTest(suffix=suffix):
                    self.assertFalse(is_public_resource(path))

    def _component(self, root, value):
        component = root / "components" / "app" / "worker"
        library = component / "library"
        library.mkdir(parents=True)
        (component / "compose.yaml").write_text("services:\n  worker:\n    image: example\n")
        (library / "__init__.py").write_text("")
        (library / "settings.py").write_text(f"VALUE = {value!r}\n")
        (library / "reader.py").write_text("from .settings import VALUE\n")
        return component

    def _load(self, root, name="library.reader", identifier="app/worker"):
        self.assertIsNotNone(importlib.util.find_spec("llmproxy.core.component_modules"))
        from llmproxy.core.component_modules import load_component_module

        return load_component_module(root, identifier, name)

    def test_relative_imports_and_caches_are_isolated_by_application_root(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            root_one, root_two = Path(first), Path(second)
            self._component(root_one, "first")
            self._component(root_two, "second")
            one = self._load(root_one)
            two = self._load(root_two)
            self.assertEqual(one.VALUE, "first")
            self.assertEqual(two.VALUE, "second")
            self.assertNotEqual(one.__name__, two.__name__)
            self.assertIs(self._load(root_one), one)
            self.assertTrue(Path(one.__file__).is_relative_to(root_one))

    def test_relative_import_cannot_follow_a_symlink_outside_the_component(self):
        from llmproxy.core.command import CommandError

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component = self._component(root, "inside")
            external = root / "external.py"
            external.write_text("VALUE = 'outside'\n")
            settings = component / "library" / "settings.py"
            settings.unlink()
            settings.symlink_to(external)
            with self.assertRaisesRegex(CommandError, "symlink"):
                self._load(root)

    def test_context_loads_library_from_its_own_component(self):
        from llmproxy.core.context import ComponentContext

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component = self._component(root, "owned")
            context = ComponentContext(root, "app", "worker", component)
            self.assertTrue(hasattr(context, "load_module"))
            self.assertEqual(context.load_module("library.reader").VALUE, "owned")

    def test_component_test_discovery_includes_owned_tests(self):
        self.assertIsNotNone(importlib.util.find_spec("tests.test_components"))
        from tests.test_components import component_test_suite

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component = self._component(root, "owned")
            tests = component / "tests"
            tests.mkdir()
            (tests / "test_owned.py").write_text(
                "import unittest\nfrom ..library.reader import VALUE\n"
                "class OwnedTest(unittest.TestCase):\n"
                "    def test_value(self): self.assertEqual(VALUE, 'owned')\n"
            )
            suite = component_test_suite(root, unittest.TestLoader())
            self.assertEqual(suite.countTestCases(), 1)
            result = unittest.TestResult()
            suite.run(result)
            self.assertTrue(result.wasSuccessful(), result.errors + result.failures)

    def test_invalid_identifiers_and_module_paths_are_rejected(self):
        from llmproxy.core.command import CommandError

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._component(root, "owned")
            for identifier in ("../worker", "app/../worker", "/app/worker", "app/worker/extra"):
                with self.subTest(identifier=identifier), self.assertRaises(CommandError):
                    self._load(root, identifier=identifier)
            for name in ("", "..reader", "library/reader", "library.reader.py", "/tmp/external"):
                with self.subTest(module=name), self.assertRaises(CommandError):
                    self._load(root, name=name)

    def test_failed_import_can_be_retried_without_a_partial_namespace(self):
        from llmproxy.core.command import CommandError

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component = self._component(root, "owned")
            (component / "library" / "reader.py").write_text("from .missing import VALUE\n")
            with self.assertRaisesRegex(CommandError, "Cannot import"):
                self._load(root)
            (component / "library" / "missing.py").write_text("VALUE = 'repaired'\n")
            self.assertEqual(self._load(root).VALUE, "repaired")

    def test_symlinked_application_root_is_rejected(self):
        from llmproxy.core.command import CommandError

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            actual = root / "actual"
            self._component(actual, "owned")
            link = root / "alias"
            link.symlink_to(actual, target_is_directory=True)
            with self.assertRaisesRegex(CommandError, "symlink"):
                self._load(link)


if __name__ == "__main__":
    unittest.main()
