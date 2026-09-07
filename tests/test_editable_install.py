"""Offline packaging regressions (run with hatchling, editables, and uv available)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
import unittest
import venv
import zipfile


ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOTS = ("components", "presets")
BUILD_TOOLS_AVAILABLE = (
    importlib.util.find_spec("hatchling") is not None
    and importlib.util.find_spec("editables") is not None
    and shutil.which("uv") is not None
)


@unittest.skipUnless(BUILD_TOOLS_AVAILABLE, "requires hatchling, editables, and uv")
class EditableInstallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="llmproxy-packaging-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.checkout = self.work / "checkout"
        self.checkout.mkdir()
        for name in ("pyproject.toml", "hatch_build.py", "README.md", "LICENSE", ".gitignore"):
            shutil.copyfile(ROOT / name, self.checkout / name)
        self.write("llmproxy/__init__.py", "")
        self.write("llmproxy/probe.py", 'VALUE = "before-install"\n')
        self.write("llmproxy/core/__init__.py", "")
        shutil.copyfile(
            ROOT / "llmproxy/core/resources.py",
            self.checkout / "llmproxy/core/resources.py",
        )
        # Keep the project's actual entry-point metadata, with a dependency-free
        # probe CLI so command-namespace changes cannot mask a packaging failure.
        self.write(
            "llmproxy/cli.py",
            "import json\n"
            "import llmproxy\n"
            "from llmproxy import probe\n"
            "from llmproxy.core.resources import resource_root\n"
            "def main():\n"
            "    print(json.dumps({'value': probe.VALUE, 'package': llmproxy.__file__,\n"
            "                      'module': probe.__file__, 'resources': str(resource_root())}))\n",
        )
        for name in RESOURCE_ROOTS:
            self.write(f"{name}/public.txt", f"public {name}\n")
        self.environment = os.environ.copy()
        for name in ("PYTHONPATH", "PYTHONHOME", "LLMPROXY_ROOT"):
            self.environment.pop(name, None)
        self.environment.update(
            PYTHONSAFEPATH="1",
            PYTHONDONTWRITEBYTECODE="1",
            UV_OFFLINE="1",
            UV_CONCURRENT_BUILDS="2",
            UV_CONCURRENT_INSTALLS="2",
        )

    def write(self, relative, content):
        path = self.checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def run_command(self, *command):
        result = subprocess.run(
            command,
            cwd=self.work,
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def build_wheel(self, version):
        from hatchling.builders.wheel import WheelBuilder

        return Path(next(WheelBuilder(str(self.checkout)).build(
            directory=str(self.work / version), versions=[version],
        )))

    def install_wheel(self, wheel):
        environment = self.work / "installed"
        venv.EnvBuilder(with_pip=False).create(environment)
        python = environment / "bin/python"
        self.run_command(
            shutil.which("uv"), "pip", "install", "--offline", "--no-deps",
            "--python", str(python), str(wheel),
        )
        return environment

    def test_installed_console_script_sees_source_edits_without_reinstall(self):
        installed = self.install_wheel(self.build_wheel("editable"))
        console = str(installed / "bin/llmproxy")
        before = json.loads(self.run_command(console))
        self.assertEqual(before["value"], "before-install")

        # Change an existing module and add a new one after the installation.
        self.write("llmproxy/probe.py", "from .added_after_install import VALUE\n")
        self.write("llmproxy/added_after_install.py", 'VALUE = "after-source-edit"\n')
        after = json.loads(self.run_command(console))
        self.assertEqual(after["value"], "after-source-edit")
        self.assertEqual(Path(after["package"]), self.checkout / "llmproxy/__init__.py")
        self.assertEqual(Path(after["module"]), self.checkout / "llmproxy/probe.py")
        self.assertEqual(Path(after["resources"]), self.checkout)
        isolated_module = self.run_command(
            str(installed / "bin/python"), "-I", "-c",
            "import llmproxy.probe; print(llmproxy.probe.__file__)",
        ).strip()
        self.assertEqual(Path(isolated_module), self.checkout / "llmproxy/probe.py")

    def test_editable_archive_contains_resources_but_no_copied_package(self):
        with zipfile.ZipFile(self.build_wheel("editable")) as archive:
            names = set(archive.namelist())
            self.assertTrue(any(name.endswith(".pth") for name in names))
            self.assertFalse(any(
                name.startswith("llmproxy/") and not name.startswith("llmproxy/resources/")
                for name in names
            ))
            for root in RESOURCE_ROOTS:
                self.assertEqual(
                    archive.read(f"llmproxy/resources/{root}/public.txt"),
                    f"public {root}\n".encode(),
                )

    def test_retired_config_and_stacks_roots_are_not_packaged(self):
        for root in ("config", "stacks"):
            self.write(f"{root}/stale.txt", "retired resource\n")
        for version in ("editable", "standard"):
            with self.subTest(version=version):
                with zipfile.ZipFile(self.build_wheel(version)) as archive:
                    self.assertFalse(any(
                        name.startswith(("llmproxy/resources/config/", "llmproxy/resources/stacks/"))
                        for name in archive.namelist()
                    ))

    def test_regular_wheel_is_self_contained_outside_checkout(self):
        wheel = self.build_wheel("standard")
        with zipfile.ZipFile(wheel) as archive:
            self.assertIn("llmproxy/__init__.py", archive.namelist())
            self.assertIn("llmproxy/probe.py", archive.namelist())
            self.assertFalse(any(name.endswith(".pth") for name in archive.namelist()))
        installed = self.install_wheel(wheel)
        shutil.rmtree(self.checkout)
        result = json.loads(self.run_command(str(installed / "bin/llmproxy")))
        self.assertEqual(result["value"], "before-install")
        package = Path(result["package"]).parent
        self.assertTrue(package.is_relative_to(installed))
        self.assertEqual(Path(result["resources"]), package / "resources")
        for root in RESOURCE_ROOTS:
            self.assertEqual(
                (package / "resources" / root / "public.txt").read_text(),
                f"public {root}\n",
            )

    def test_mandatory_build_inputs_reject_symlinks_before_packaging(self):
        from hatchling.builders.sdist import SdistBuilder
        from hatchling.builders.wheel import WheelBuilder

        # Preserve valid syntax for the configuration and hook trust anchors.
        # All targets and canaries are synthetic and outside the checkout.
        for relative in (
            "llmproxy/__init__.py", "README.md", "LICENSE", "pyproject.toml",
            "hatch_build.py", "llmproxy/core/resources.py",
        ):
            path = self.checkout / relative
            original = path.read_bytes()
            outside = self.work / "external-input"
            outside.write_bytes(original + b"\n# EXTERNAL-BUILD-INPUT-CANARY\n")
            path.unlink()
            path.symlink_to(outside)
            try:
                for builder_type, version in (
                    (WheelBuilder, "standard"), (WheelBuilder, "editable"),
                    (SdistBuilder, "standard"),
                ):
                    with self.subTest(path=relative, builder=builder_type.__name__, version=version):
                        output = self.work / "rejected-artifacts"
                        with self.assertRaisesRegex(ValueError, "Unsafe build input"):
                            next(builder_type(str(self.checkout)).build(
                                directory=str(output), versions=[version],
                            ))
                        self.assertFalse(output.exists() and any(output.iterdir()))
            finally:
                path.unlink()
                path.write_bytes(original)
                shutil.rmtree(self.work / "rejected-artifacts", ignore_errors=True)

    def test_implicit_hatch_inputs_also_reject_symlinks(self):
        from hatchling.builders.sdist import SdistBuilder

        for relative in (".gitignore", ".hgignore", "LICENSE.extra", "NOTICE"):
            path = self.checkout / relative
            original = path.read_bytes() if path.exists() else None
            outside = self.work / "external-implicit-input"
            outside.write_bytes(b"# EXTERNAL-IMPLICIT-INPUT-CANARY\n")
            if path.exists():
                path.unlink()
            path.symlink_to(outside)
            try:
                for version in ("standard", "editable", "sdist"):
                    with self.subTest(path=relative, version=version):
                        with self.assertRaisesRegex(ValueError, "Unsafe build input"):
                            if version == "sdist":
                                next(SdistBuilder(str(self.checkout)).build(
                                    directory=str(self.work / version), versions=["standard"],
                                ))
                            else:
                                self.build_wheel(version)
            finally:
                path.unlink()
                if original is not None:
                    path.write_bytes(original)

    def test_private_implicit_license_inputs_fail_closed_before_reads(self):
        from hatchling.builders.sdist import SdistBuilder

        # Exercise both the checkout and a genuine sdist with a later injection.
        sdist = next(SdistBuilder(str(self.checkout)).build(
            directory=str(self.work / "clean-sdist"), versions=["standard"],
        ))
        unpacked = self.work / "license-unpacked"
        with tarfile.open(sdist) as archive:
            archive.extractall(unpacked, filter="data")
        rebuilt_source = next(unpacked.iterdir())
        script = """
import os
from pathlib import Path
import sys
os.environ.pop('PIP_BUILD_TRACKER', None)
import hatchling.build
from hatchling.builders.sdist import SdistBuilder
from hatchling.builders.wheel import WheelBuilder
checkout, name, operation, output = sys.argv[1:]
os.chdir(checkout)
builder = None
if operation.startswith('cached_'):
    kind = operation.removeprefix('cached_')
    builder_type = SdistBuilder if kind == 'sdist' else WheelBuilder
    builder = builder_type(checkout)
    builder.metadata.validate_fields()
    # Force the implicit license list and README into the metadata cache too.
    _ = builder.metadata.core.license_files, builder.metadata.core.readme
private = Path(checkout) / name
private.write_text('SYNTHETIC-PRIVATE-LICENSE-CANARY', encoding='utf-8')
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        if Path(os.fsdecode(args[0])).resolve() == private:
            raise AssertionError('private license input was opened before rejection')
sys.addaudithook(audit)
try:
    try:
        if builder is None:
            getattr(hatchling.build, operation)(output)
        else:
            version = 'editable' if kind == 'editable' else 'standard'
            next(builder.build(directory=output, versions=[version]))
    except ValueError as error:
        assert 'Unsafe build input' in str(error) and name in str(error), str(error)
    else:
        raise AssertionError('private license input was accepted')
    assert not Path(output).exists() or not any(Path(output).iterdir())
finally:
    private.unlink()
"""
        for source in (self.checkout, rebuilt_source):
            for name in (
                "LICENSE.local.txt", "COPYING.local.txt", "AUTHORS.local.json", "NOTICE.local",
            ):
                for operation in (
                    "build_wheel", "build_editable", "build_sdist",
                    "prepare_metadata_for_build_wheel", "prepare_metadata_for_build_editable",
                    "cached_wheel", "cached_editable", "cached_sdist",
                ):
                    with self.subTest(source=source.name, name=name, operation=operation):
                        output = self.work / "private-license-output"
                        output.mkdir()
                        try:
                            self.run_command(
                                sys.executable, "-c", script, str(source), name,
                                operation, str(output),
                            )
                        finally:
                            shutil.rmtree(output, ignore_errors=True)

    def test_metadata_symlinks_are_rejected_without_opening_the_target(self):
        script = """
import os
from pathlib import Path
import sys
os.environ.pop('PIP_BUILD_TRACKER', None)
import hatchling.build
checkout, external, operation, output = sys.argv[1:]
external = Path(external)
def audit(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        if Path(os.fsdecode(args[0])).resolve() == external:
            raise AssertionError('symlink target was opened before rejection')
sys.addaudithook(audit)
os.chdir(checkout)
try:
    getattr(hatchling.build, operation)(output)
except ValueError as error:
    assert 'Unsafe build input' in str(error), str(error)
else:
    raise AssertionError('unsafe metadata input was accepted')
"""
        outside = self.work / "never-open-this-canary"
        outside.write_bytes(b"EXTERNAL-METADATA-CANARY\xff\n")
        for relative in ("README.md", "LICENSE"):
            path = self.checkout / relative
            original = path.read_bytes()
            path.unlink()
            path.symlink_to(outside)
            try:
                for operation in (
                    "build_wheel", "build_editable", "build_sdist",
                    "prepare_metadata_for_build_wheel", "prepare_metadata_for_build_editable",
                ):
                    with self.subTest(path=relative, operation=operation):
                        self.run_command(
                            sys.executable, "-c", script, str(self.checkout), str(outside),
                            operation, str(self.work / "metadata-output"),
                        )
            finally:
                path.unlink()
                path.write_bytes(original)

    def test_mandatory_package_parents_cannot_be_symlinked(self):
        from hatchling.builders.sdist import SdistBuilder

        for relative in ("llmproxy", "llmproxy/core"):
            path = self.checkout / relative
            outside = self.work / "external-package"
            path.rename(outside)
            path.symlink_to(outside, target_is_directory=True)
            try:
                for version in ("standard", "editable", "sdist"):
                    with self.subTest(path=relative, version=version):
                        with self.assertRaisesRegex(ValueError, "Unsafe build input"):
                            if version == "sdist":
                                next(SdistBuilder(str(self.checkout)).build(
                                    directory=str(self.work / version), versions=["standard"],
                                ))
                            else:
                                self.build_wheel(version)
            finally:
                path.unlink()
                outside.rename(path)

    def test_cached_metadata_does_not_bypass_build_input_validation(self):
        from hatchling.builders.wheel import WheelBuilder

        builder = WheelBuilder(str(self.checkout))
        builder.metadata.validate_fields()
        path = self.checkout / "llmproxy/__init__.py"
        path.unlink()
        path.symlink_to(self.work / "missing-external-target")
        with self.assertRaisesRegex(ValueError, "Unsafe build input"):
            next(builder.build(directory=str(self.work / "cached"), versions=["standard"]))

    def test_all_artifacts_exclude_private_generated_and_symlinked_files(self):
        from hatchling.builders.sdist import SdistBuilder
        from hatchling.builders.wheel import WheelBuilder

        # Synthetic canaries only: never read or copy actual local configuration.
        canary = b"PRIVATE-PACKAGING-TEST-CANARY"
        outside = self.work / "outside"
        outside.mkdir()
        (outside / "payload").write_bytes(canary)
        integration = "components/app/source/integrations/target/sink"
        self.write(f"{integration}/config.json", '{"public": true}\n')
        self.write(f"{integration}/config.local.json", canary.decode())
        for root in ("llmproxy", "components", "stacks", "presets", "config", "tests"):
            self.write(f"{root}/public.txt", f"public {root}\n")
            self.write(f"{root}/compose.local.example.yaml", "services: {}\n")
            for name in (
                ".env", ".env.example", "config.local.json", "config.gen.json", "openapi.json",
                "nested.local/payload", ".env.d/payload", "__pycache__/probe.pyc",
                "orphan.pyc", "orphan.pyo",
                "build/payload", "dist/payload",
                "compose.local.yaml", "compose.local.example.yaml.bak",
                "config.local.example.json",
                "nested.local/compose.local.example.yaml",
                "nested/compose.local.example.yaml/payload",
                ".env.d/compose.local.example.yaml",
            ):
                self.write(f"{root}/{name}", canary.decode())
            (self.checkout / root / "linked-file").symlink_to(outside / "payload")
            (self.checkout / root / "linked-directory").symlink_to(outside, target_is_directory=True)

        for version in ("editable", "standard"):
            with self.subTest(wheel=version):
                with zipfile.ZipFile(self.build_wheel(version)) as archive:
                    self.assertEqual(
                        archive.read(next(name for name in archive.namelist()
                                          if name.endswith(".dist-info/licenses/LICENSE"))),
                        (self.checkout / "LICENSE").read_bytes(),
                    )
                    self.assertEqual(
                        archive.read(f"llmproxy/resources/{integration}/config.json"),
                        b'{"public": true}\n',
                    )
                    for name in archive.namelist():
                        self.assertNotIn(canary, archive.read(name), name)
                        self.assertNotIn("linked-", name)
                        self.assertFalse(name.startswith("tests/"), name)
                    for root in RESOURCE_ROOTS:
                        self.assertIn(f"llmproxy/resources/{root}/public.txt", archive.namelist())
                        self.assertEqual(
                            archive.read(f"llmproxy/resources/{root}/compose.local.example.yaml"),
                            b"services: {}\n",
                        )

        sdist = next(SdistBuilder(str(self.checkout)).build(
            directory=str(self.work / "sdist"), versions=["standard"],
        ))
        unpacked = self.work / "unpacked"
        with tarfile.open(sdist) as archive:
            names = set()
            for member in archive.getmembers():
                relative = Path(member.name)
                self.assertFalse(relative.is_absolute())
                self.assertNotIn("..", relative.parts)
                self.assertTrue(member.isfile() or member.isdir(), member.name)
                names.add(Path(*relative.parts[1:]).as_posix())
                if member.isfile():
                    stream = archive.extractfile(member)
                    assert stream is not None
                    with stream:
                        self.assertNotIn(canary, stream.read(), member.name)
                    self.assertNotIn("linked-", member.name)
            for root in ("llmproxy", *RESOURCE_ROOTS, "tests"):
                self.assertIn(f"{root}/public.txt", names)
                self.assertIn(f"{root}/compose.local.example.yaml", names)
            self.assertIn(".gitignore", names)
            self.assertIn("LICENSE", names)
            self.assertIn(f"{integration}/config.json", names)
            self.assertNotIn(f"{integration}/config.local.json", names)
            self.assertFalse(any(name.startswith(("config/", "stacks/")) for name in names))
            # All member paths and types were checked above, including link rejection.
            archive.extractall(unpacked)
        source = next(unpacked.iterdir())
        rebuilt = next(WheelBuilder(str(source)).build(
            directory=str(self.work / "rebuilt"), versions=["standard"],
        ))
        with zipfile.ZipFile(rebuilt) as archive:
            self.assertEqual(
                archive.read(next(name for name in archive.namelist()
                                  if name.endswith(".dist-info/licenses/LICENSE"))),
                (self.checkout / "LICENSE").read_bytes(),
            )
            self.assertIn("llmproxy/cli.py", archive.namelist())
            self.assertEqual(
                archive.read(f"llmproxy/resources/{integration}/config.json"),
                b'{"public": true}\n',
            )
            for root in RESOURCE_ROOTS:
                self.assertIn(f"llmproxy/resources/{root}/public.txt", archive.namelist())
                self.assertEqual(
                    archive.read(f"llmproxy/resources/{root}/compose.local.example.yaml"),
                    b"services: {}\n",
                )
            for name in archive.namelist():
                self.assertNotIn(canary, archive.read(name), name)


if __name__ == "__main__":
    unittest.main()
