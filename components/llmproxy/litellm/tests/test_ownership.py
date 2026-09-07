"""Contracts for LiteLLM resources and implementation ownership."""

import argparse
import ast
import io
import json
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from llmproxy.core.component_modules import load_component_module
from llmproxy.core.resources import resource_root


ROOT = resource_root()
COMPONENT_DIR = ROOT / "components" / "llmproxy" / "litellm"


class LiteLLMOwnershipTest(unittest.TestCase):
    def test_source_modules_are_cli_independent_and_commands_are_adapters(self):
        backends = ("config_generate", "config_sync", "key_create", "key_limits")
        for name in backends:
            with self.subTest(backend=name):
                tree = ast.parse((COMPONENT_DIR / "src" / f"{name}.py").read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        self.assertFalse(any(alias.name == "argparse" for alias in node.names))
                    elif isinstance(node, ast.ImportFrom):
                        self.assertNotEqual(node.module, "argparse")
                    elif isinstance(node, ast.Name):
                        self.assertNotIn(node.id, {"COMMAND", "ALIASES", "MUTATING", "AppContext", "ComponentContext"})
        for command in ("config_generate", "config_sync", "create_key", "key_limits"):
            with self.subTest(command=command):
                tree = ast.parse((COMPONENT_DIR / "commands" / f"{command}.py").read_text())
                self.assertFalse(any(isinstance(node, (ast.For, ast.AsyncFor, ast.While)) for node in ast.walk(tree)))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertNotEqual(node.module, "llmproxy.core.http")
        self.assertFalse(list((COMPONENT_DIR / "configs").glob("*.py")))

    def test_configure_does_not_require_owned_libraries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component = root / "components" / "llmproxy" / "litellm"
            (component / "commands").mkdir(parents=True)
            (component / "compose.yaml").write_text("services: {}\n")
            for name in ("config_generate", "config_sync", "create_key", "key_limits"):
                with self.subTest(command=name):
                    shutil.copy2(
                        COMPONENT_DIR / "commands" / f"{name}.py",
                        component / "commands" / f"{name}.py",
                    )
                    command = load_component_module(root, "llmproxy/litellm", f"commands.{name}")
                    parser = argparse.ArgumentParser()
                    command.configure(parser)
                    if name.startswith("config_"):
                        args = parser.parse_args(["--config", "custom.json"])
                        self.assertEqual(args.config, Path("custom.json"))
                    else:
                        parser.parse_args(["user@example.invalid"] if name == "create_key" else [])
                    namespace = command.__name__.split(".")[0]
                    self.assertFalse(any(module.startswith(f"{namespace}.src") for module in sys.modules))

    def test_wrappers_run_against_context_root_with_private_owned_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            component = root / "components" / "llmproxy" / "litellm"
            component.mkdir(parents=True)
            (component / "compose.yaml").write_text("services: {}\n")
            (component / "src").mkdir()
            for source in (COMPONENT_DIR / "src").glob("*.py"):
                shutil.copy2(source, component / "src" / source.name)
            (component / "configs").mkdir()
            config_file = component / "configs" / "config.json"
            config_file.write_text('{"providers": {}, "router_settings": {"timeout": 42}}\n')
            context = SimpleNamespace(root=root, component_dir=component)

            for name in ("config_generate", "config_sync"):
                with self.subTest(command=name):
                    # Deliberately load the wrapper from a different root than its context.
                    command = load_component_module(ROOT, "llmproxy/litellm", f"commands.{name}")
                    parser = argparse.ArgumentParser()
                    command.configure(parser)
                    argv = ["--config", str(config_file)]
                    if name == "config_sync":
                        argv.append("--dry-run")
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(command.run(parser.parse_args(argv), context), 0)

            output = root / "build" / "llmproxy" / "litellm" / "config.gen.json"
            self.assertEqual(json.loads(output.read_text())["router_settings"], {"timeout": 42})
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            generator = load_component_module(root, "llmproxy/litellm", "src.config_generate")
            sync = load_component_module(root, "llmproxy/litellm", "src.config_sync")
            original = load_component_module(ROOT, "llmproxy/litellm", "src.config_generate")
            self.assertEqual(generator.REPO_ROOT, root)
            self.assertEqual(generator.DEFAULT_CONFIG_FILE, config_file)
            self.assertIs(sync.generate_config, generator.generate_config)
            self.assertIsNot(original, generator)

    def test_litellm_resources_libraries_and_tests_have_one_owner(self):
        expected = (
            "src/__init__.py",
            "src/config_generate.py",
            "src/config_sync.py",
            "src/key_create.py",
            "src/key_limits.py",
            "configs/config.json",
            "configs/config.schema.json",
            "configs/key-limits.json",
            "configs/key-limits.schema.json",
            "tests/__init__.py",
            "tests/test_config_generator.py",
            "tests/test_config_syncer.py",
            "tests/test_keys.py",
            "tests/test_key_create.py",
        )
        for relative in expected:
            with self.subTest(component_path=relative):
                self.assertTrue((COMPONENT_DIR / relative).is_file())
        retired = (
            "components/llmproxy/litellm/configuration",
            "components/llmproxy/litellm/management",
            "components/llmproxy/litellm/keys",
            "llmproxy/configuration",
            "llmproxy/management",
            "config/litellm",
            "config/keys",
            "tests/test_config_generator.py",
            "tests/test_config_syncer.py",
            "tests/test_keys.py",
            "tests/test_key_create.py",
        )
        for relative in retired:
            with self.subTest(retired_path=relative):
                self.assertFalse((ROOT / relative).exists())