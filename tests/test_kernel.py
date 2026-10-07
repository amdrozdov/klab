"""Unit tests for kernel build bookkeeping (no compiler needed)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from labtool import kernel


class TestCompileCommandsLink(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.cc = root / "builds" / "new" / "compile_commands.json"
        self.cc.parent.mkdir(parents=True)
        self.cc.write_text("[]")
        self.tree = root / "tree"
        self.tree.mkdir()
        self.link = self.tree / "compile_commands.json"

    def test_creates_the_link(self) -> None:
        kernel.link_compile_commands(self.cc, self.link)
        self.assertEqual(self.link.resolve(), self.cc.resolve())

    def test_replaces_a_link_left_by_a_removed_build(self) -> None:
        # the reported crash: exists() is False for a dangling link, so the old
        # code tried to create it again and failed with FileExistsError
        self.link.symlink_to(self.cc.parent.parent / "gone" / "compile_commands.json")
        self.assertTrue(self.link.is_symlink() and not self.link.exists())
        kernel.link_compile_commands(self.cc, self.link)
        self.assertEqual(self.link.resolve(), self.cc.resolve())

    def test_keeps_a_working_link_to_another_build(self) -> None:
        other = self.cc.parent.parent / "other" / "compile_commands.json"
        other.parent.mkdir()
        other.write_text("[]")
        self.link.symlink_to(other)
        kernel.link_compile_commands(self.cc, self.link)
        self.assertEqual(self.link.resolve(), other.resolve())

    def test_keeps_a_real_file(self) -> None:
        self.link.write_text("mine")
        kernel.link_compile_commands(self.cc, self.link)
        self.assertFalse(self.link.is_symlink())
        self.assertEqual(self.link.read_text(), "mine")

    def test_does_nothing_without_a_compile_commands_file(self) -> None:
        self.cc.unlink()
        kernel.link_compile_commands(self.cc, self.link)
        self.assertFalse(self.link.is_symlink())

    def test_a_failure_to_link_only_warns(self) -> None:
        with mock.patch.object(Path, "symlink_to", side_effect=PermissionError("no")):
            with mock.patch.object(kernel, "warn") as warn:
                kernel.link_compile_commands(self.cc, self.link)
        self.assertIn("could not link", warn.call_args.args[0])


class TestBuildFinishesProperly(unittest.TestCase):
    """build() with make mocked out: the end of a build, after the compile."""

    def test_metadata_is_saved_before_compile_commands_and_a_stale_link_is_survived(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tree = root / "trees" / "t"
            tree.mkdir(parents=True)
            (tree / "Makefile").write_text("")
            (tree / "compile_commands.json").symlink_to(
                root / "builds" / "old" / "x.json"
            )
            out = root / "builds" / "b"
            out.mkdir(parents=True)
            (out / ".config").write_text("CONFIG_X=y\n")
            seen: dict[str, object] = {}

            def fake_run(cmd: list[object], **kw: object) -> mock.Mock:
                args = [str(c) for c in cmd]
                if args[-1] == "compile_commands.json":
                    meta = json.loads((out / "lab-build.json").read_text())
                    seen["release_when_compile_commands_ran"] = meta.get("kernelrelease")
                    (out / "compile_commands.json").write_text("[]")
                return mock.Mock(stdout="")

            def fake_output(cmd: list[object], cwd: object = None) -> str:
                return "7.0.0-test" if "kernelrelease" in [str(c) for c in cmd] else ""

            info = {"commit": "abc", "describe": "v7.0.0"}
            with mock.patch.multiple(
                kernel,
                BUILDS=root / "builds",
                TREES=root / "trees",
                run=fake_run,
                output=fake_output,
                have=mock.Mock(return_value=False),
                tree_info=mock.Mock(return_value=info),
                check_disk=mock.DEFAULT,
                verify_config=mock.Mock(return_value=True),
                check_build_deps=mock.DEFAULT,
                save_state=mock.DEFAULT,
            ):
                name = kernel.build("t", name="b", jobs=1)

            self.assertEqual(name, "b")
            self.assertEqual(seen["release_when_compile_commands_ran"], "7.0.0-test")
            final = json.loads((out / "lab-build.json").read_text())
            self.assertEqual(final["kernelrelease"], "7.0.0-test")
            link = tree / "compile_commands.json"
            self.assertEqual(link.resolve(), (out / "compile_commands.json").resolve())


if __name__ == "__main__":
    unittest.main()
