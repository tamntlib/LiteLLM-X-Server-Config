"""Generic source-owned integration path contracts (temporary fixtures only)."""

from pathlib import Path
import tempfile
import unittest

from llmproxy.core.integrations import resolve_integration_file


class IntegrationFileTest(unittest.TestCase):
    def test_destinations_and_sources_are_isolated_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            for target in ("stack/one", "stack/two", "other/one"):
                leaf = source / "integrations" / target / "rules.yaml"
                leaf.parent.mkdir(parents=True)
                leaf.write_text(target)
                self.assertEqual(resolve_integration_file(source, target, "rules.yaml"), leaf)
            self.assertIsNone(resolve_integration_file(source, "other/two", "rules.yaml"))
            self.assertIsNone(resolve_integration_file(source, "stack/one", "different.yaml"))
            self.assertIsNone(resolve_integration_file(Path(temporary) / "different-source", "stack/one", "rules.yaml"))
            other = source / "integrations" / "stack" / "two" / "rules.yaml"
            other.unlink()
            other.symlink_to(source / "integrations" / "stack" / "one" / "rules.yaml")
            with self.assertRaisesRegex(ValueError, "symlink"):
                resolve_integration_file(source, "stack/two", "rules.yaml")
            self.assertEqual(
                resolve_integration_file(source, "stack/one", "rules.yaml"),
                source / "integrations" / "stack" / "one" / "rules.yaml",
            )

    def test_source_parent_traversal_does_not_hide_a_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            (root / "linked").symlink_to(real)
            source = root / "linked" / ".." / "source"
            with self.assertRaisesRegex(ValueError, "symlink"):
                resolve_integration_file(source, "stack/target", "rules.yaml")

    def test_rejects_existing_nonregular_entries_at_every_depth(self):
        import os
        import socket

        for depth in range(6):
            kinds = ("directory", "fifo", "socket") if depth == 5 else ("file", "fifo", "socket")
            for kind in kinds:
                with self.subTest(depth=depth, kind=kind), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source = root / "ancestor" / "source"
                    leaf = source / "integrations" / "stack" / "target" / "rules.yaml"
                    entries = (root / "ancestor", source, source / "integrations",
                               leaf.parent.parent, leaf.parent, leaf)
                    entry = entries[depth]
                    entry.parent.mkdir(parents=True, exist_ok=True)
                    with socket.socket(socket.AF_UNIX) as sock:
                        if kind == "directory":
                            entry.mkdir()
                        elif kind == "fifo":
                            os.mkfifo(entry)
                        elif kind == "socket":
                            sock.bind(str(entry))
                        else:
                            entry.write_text("not a directory")
                        with self.assertRaisesRegex(ValueError, "directory|regular file"):
                            resolve_integration_file(source, "stack/target", "rules.yaml")

    def test_rejects_symlinks_at_every_depth_even_internal_or_dangling(self):
        for depth in range(6):
            for destination in ("sibling", "outside", "dangling"):
                with self.subTest(depth=depth, destination=destination):
                    with tempfile.TemporaryDirectory() as temporary:
                        root = Path(temporary)
                        source = root / "ancestor" / "source"
                        leaf = source / "integrations" / "stack" / "target" / "rules.yaml"
                        leaf.parent.mkdir(parents=True)
                        leaf.write_text("opaque")
                        entries = (root / "ancestor", source, source / "integrations",
                                   leaf.parent.parent, leaf.parent, leaf)
                        entry = entries[depth]
                        moved = entry.with_name(entry.name + "-real")
                        if destination == "outside":
                            moved = root / "elsewhere"
                        entry.rename(moved)
                        entry.symlink_to(root / "absent" if destination == "dangling" else moved)
                        with self.assertRaisesRegex(ValueError, "symlink"):
                            resolve_integration_file(source, "stack/target", "rules.yaml")

    def test_invalid_filenames_cannot_escape_destination(self):
        invalid = ("", ".", "..", "../rules.yaml", "/rules.yaml", "sub/rules.yaml",
                   r"sub\rules.yaml", r"..\rules.yaml", "rules..yaml", "rules\x00.yaml", None, 1)
        with tempfile.TemporaryDirectory() as temporary:
            for filename in invalid:
                with self.subTest(filename=filename), self.assertRaisesRegex(ValueError, "filename"):
                    resolve_integration_file(Path(temporary) / "missing", "stack/target", filename)

    def test_invalid_targets_are_rejected_even_when_source_is_missing(self):
        invalid = ("", "target", "/stack/target", "stack/target/", "stack//target",
                   "stack/target/extra", "../target", "stack/..", "./target",
                   "stack\\target", "stack/target.json", "-stack/target", "stack/_target",
                   "stack/with space", "stáck/target", "stack/target\n", None, 1)
        with tempfile.TemporaryDirectory() as temporary:
            for target in invalid:
                with self.subTest(target=target), self.assertRaisesRegex(ValueError, "target"):
                    resolve_integration_file(Path(temporary) / "missing", target, "rules.yaml")

    def test_missing_path_at_any_depth_returns_none(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            directories = (source, source / "integrations", source / "integrations" / "stack",
                           source / "integrations" / "stack" / "target")
            for directory in directories:
                with self.subTest(missing=directory):
                    self.assertIsNone(resolve_integration_file(source, "stack/target", "rules.yaml"))
                directory.mkdir()
            self.assertIsNone(resolve_integration_file(source, "stack/target", "rules.yaml"))

    def test_resolves_exact_destination_without_interpreting_file_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            expected = source / "integrations" / "Stack_1" / "Target-2" / "rules.yaml"
            expected.parent.mkdir(parents=True)
            expected.write_text("opaque bytes: not a service schema\n")
            self.assertEqual(
                resolve_integration_file(source, "Stack_1/Target-2", "rules.yaml"),
                expected,
            )
