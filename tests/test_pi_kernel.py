"""Unit tests for `lab build --arch pi` and `lab pi kernel` (no Pi, no toolchain)."""

import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from labtool import common, kernel, pi, vm
from labtool.common import LabError


class TestBuildSide(unittest.TestCase):
    def test_arch_table(self) -> None:
        self.assertEqual(kernel.ARCHES["x86"].image, "arch/x86/boot/bzImage")
        pi_arch = kernel.ARCHES["pi"]
        self.assertEqual(
            (pi_arch.make_arch, pi_arch.cross), ("arm64", "aarch64-linux-gnu-")
        )
        self.assertFalse(pi_arch.vm)

    def test_localversion_names_the_build(self) -> None:
        self.assertEqual(kernel.localversion("v6.18.55-pi"), "-klab-v6.18.55-pi")
        self.assertEqual(kernel.localversion("my build/1"), "-klab-my-build-1")

    def test_localversion_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = kernel.localversion_fragment(Path(d), "pi618", "6.18.55")
            self.assertEqual(path.read_text(), 'CONFIG_LOCALVERSION="-klab-pi618"\n')

    def test_release_must_fit_in_uname(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            self.assertRaisesRegex(LabError, "too long"),
        ):
            kernel.localversion_fragment(Path(d), "x" * 60, "6.18.55")

    def test_pi_rejects_x86_options(self) -> None:
        with (
            mock.patch.object(kernel, "default_tree", return_value="t"),
            mock.patch.object(kernel, "tree_path", return_value=Path("/nonexistent")),
        ):
            with self.assertRaisesRegex(LabError, "--raw"):
                kernel.build("t", arch="pi", raw=True)
            with self.assertRaisesRegex(LabError, "x86 config"):
                kernel.build("t", arch="pi", base_config="host")

    def test_old_builds_are_x86(self) -> None:
        with mock.patch.object(kernel, "build_meta", return_value={"tree": "t"}):
            self.assertEqual(kernel.build_arch("old"), "x86")
            kernel.require_x86("old")

    def test_pi_builds_cannot_boot_in_the_vm(self) -> None:
        with mock.patch.object(kernel, "build_meta", return_value={"arch": "pi"}):
            with self.assertRaisesRegex(LabError, "lab pi kernel install v1-pi"):
                vm.qemu_cmd("v1-pi", Path("/tmp"))
            with self.assertRaisesRegex(LabError, "Raspberry Pi"):
                vm.gdb("v1-pi")

    def test_removing_a_pi_build_clears_its_default(self) -> None:
        saved: dict[str, object] = {}
        with (
            mock.patch.object(
                kernel, "load_state", return_value={"pi_build": "x", "build": "y"}
            ),
            mock.patch.object(
                kernel, "save_state", side_effect=lambda **kw: saved.update(kw)
            ),
        ):
            kernel._forget_default(build="x")
        self.assertEqual(saved, {"pi_build": None})


class FakePiBuild(unittest.TestCase):
    """A synthetic, already-built `--arch pi` build under a temp BUILDS dir."""

    name = "v6.18.55-pi"
    release = "6.18.55-klab-v6.18.55-pi"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.builds = Path(tmp.name)
        patcher = mock.patch.object(kernel, "BUILDS", self.builds)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.out = self.builds / self.name

    def make(self, dtb: bool = True, built: bool = True) -> None:
        out = self.out
        (out / "arch/arm64/boot").mkdir(parents=True)
        meta = {"arch": "pi", "image": "arch/arm64/boot/Image.gz"}
        if built:
            meta["kernelrelease"] = self.release
            (out / "arch/arm64/boot/Image.gz").write_bytes(b"\x1f\x8bKERNEL")
            mods = out / "modroot/lib/modules" / self.release
            (mods / "kernel/drivers").mkdir(parents=True)
            (mods / "kernel/drivers/x.ko").write_bytes(b"ELF")
            (mods / "modules.dep").write_text("")
            (mods / "build").symlink_to("/home/me/linux")  # host-only links
            (mods / "source").symlink_to("/home/me/linux")
        if dtb:
            (out / "dtbroot/broadcom").mkdir(parents=True)
            (out / "dtbroot/broadcom/bcm2711-rpi-4-b.dtb").write_bytes(b"DTB")
            (out / "dtbroot/broadcom/bcm2712-rpi-5-b.dtb").write_bytes(b"other")
        (out / "lab-build.json").write_text(json.dumps(meta))


class TestBundle(FakePiBuild):
    def test_layout_and_contents(self) -> None:
        self.make()
        bundle = pi.make_bundle(self.name)
        with tarfile.open(bundle) as tf:
            names = set(tf.getnames())
            self.assertIn("boot/firmware/klab/kernel8.img", names)
            self.assertIn("boot/firmware/klab/bcm2711-rpi-4-b.dtb", names)  # flattened
            self.assertIn("boot/firmware/klab/overlays/README", names)
            self.assertIn("boot/firmware/klab/install.sh", names)
            self.assertIn(f"lib/modules/{self.release}/kernel/drivers/x.ko", names)
            self.assertFalse([n for n in names if n.endswith(("/build", "/source"))])
            self.assertFalse([n for n in names if "bcm2712" in n])  # Pi 4 only
            self.assertEqual(
                tf.extractfile("boot/firmware/klab/overlays/README").read(), b""
            )  # type: ignore[union-attr]
            build_file = tf.extractfile("boot/firmware/klab/BUILD").read().decode()  # type: ignore[union-attr]
            self.assertEqual(
                pi.parse_kv(build_file), {"build": self.name, "release": self.release}
            )
            self.assertTrue(
                all(m.uid == 0 and m.uname == "root" for m in tf.getmembers())
            )

    def test_install_script_is_shipped_for_this_release(self) -> None:
        self.make()
        with tarfile.open(pi.make_bundle(self.name)) as tf:
            script = tf.extractfile("boot/firmware/klab/install.sh").read().decode()  # type: ignore[union-attr]
        self.assertIn(f"depmod -a {self.release}", script)
        self.assertIn(
            "cp /boot/firmware/cmdline.txt /boot/firmware/klab/cmdline.txt", script
        )
        self.assertTrue(script.startswith("#!/bin/sh\n"))

    def test_refuses_unbuilt_or_incomplete_builds(self) -> None:
        self.make(built=False)
        with self.assertRaisesRegex(LabError, "not built yet"):
            pi.make_bundle(self.name)

    def test_refuses_build_without_the_pi4_dtb(self) -> None:
        self.make(dtb=False)
        (self.out / "dtbroot").mkdir()
        with self.assertRaisesRegex(LabError, "bcm2711-rpi-4-b.dtb"):
            pi.make_bundle(self.name)

    def test_refuses_x86_builds(self) -> None:
        self.out.mkdir(parents=True)
        (self.out / "lab-build.json").write_text(json.dumps({"arch": "x86"}))
        with self.assertRaisesRegex(LabError, "not a Raspberry Pi build"):
            pi.make_bundle(self.name)


class TestConfigBlock(unittest.TestCase):
    """The klab block in config.txt is added, replaced and removed with real sh/sed."""

    STOCK = "dtparam=audio=on\nauto_initramfs=1\n[cm4]\notg_mode=1\n[all]\n"

    def run_sh(self, script: str) -> None:
        subprocess.run(["sh", "-c", script], check=True)

    def test_add_is_idempotent_and_removal_restores_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.txt"
            cfg.write_text(self.STOCK)
            for _ in range(2):  # installing twice must not stack blocks
                self.run_sh(pi.config_edit_script(str(cfg)))
            text = cfg.read_text()
            self.assertEqual(text.count("os_prefix=klab/"), 1)
            self.assertEqual(text.count("# klab begin"), 1)
            self.assertIn("auto_initramfs=0", text)
            self.assertTrue(
                text.startswith(self.STOCK)
            )  # stock lines untouched, block last
            self.run_sh(pi.STRIP_BLOCK.replace("/boot/firmware/config.txt", str(cfg)))
            self.assertEqual(cfg.read_text().strip(), (self.STOCK + "\n").strip())

    def test_stock_command_only_touches_files_that_have_the_block(self) -> None:
        self.assertIn("grep -q '^# klab begin' /boot/firmware/config.txt", pi.STOCK)
        self.assertIn("sudo sed -i", pi.STOCK)


class TestRemoteInstall(FakePiBuild):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target, value in (("PI_FILE", Path(tmp.name) / "pi.json"),):
            patcher = mock.patch.object(pi, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.conn = pi.Conn("alice", "pw", "10.0.0.5", 22)
        self.calls: list[tuple[str, Path | None]] = []

    def fake_remote(self, free: int = 10**9) -> mock.Mock:
        def remote(conn: pi.Conn, command: str, stdin: Path | None = None) -> mock.Mock:
            self.calls.append((command, stdin))
            out = f"{free}\n" if command.startswith("df ") else ""
            return mock.Mock(stdout=out)

        return mock.Mock(side_effect=remote)

    def test_install_streams_the_bundle_then_records_it(self) -> None:
        self.make()
        with (
            mock.patch.object(pi, "connection", return_value=self.conn),
            mock.patch.object(pi, "remote", self.fake_remote()),
        ):
            pi.kernel_install(self.name)
        commands = [c for c, _ in self.calls]
        self.assertTrue(commands[0].startswith("df --output=avail"))
        self.assertEqual(commands[1], pi.UNPACK)
        self.assertEqual(self.calls[1][1], self.out / "pi-bundle.tar.gz")  # fed to stdin
        saved = pi.load_pi()["kernel"]
        self.assertEqual((saved["build"], saved["release"]), (self.name, self.release))

    def test_install_defaults_to_the_last_pi_build(self) -> None:
        self.make()
        with (
            mock.patch.object(pi, "load_state", return_value={"pi_build": self.name}),
            mock.patch.object(pi, "connection", return_value=self.conn),
            mock.patch.object(pi, "remote", self.fake_remote()),
        ):
            pi.kernel_install(None)
        self.assertEqual(pi.load_pi()["kernel"]["build"], self.name)

    def test_install_without_any_pi_build(self) -> None:
        with mock.patch.object(pi, "load_state", return_value={}):
            with self.assertRaisesRegex(LabError, "lab build --arch pi"):
                pi.kernel_install(None)

    def test_install_stops_when_the_boot_partition_is_too_small(self) -> None:
        self.make()
        with (
            mock.patch.object(pi, "connection", return_value=self.conn),
            mock.patch.object(pi, "remote", self.fake_remote(free=10)),
        ):
            with self.assertRaisesRegex(LabError, "free"):
                pi.kernel_install(self.name)
        self.assertEqual(len(self.calls), 1)  # nothing was unpacked
        self.assertNotIn("kernel", pi.load_pi())

    def test_reboot_flag(self) -> None:
        self.make()
        with (
            mock.patch.object(pi, "connection", return_value=self.conn),
            mock.patch.object(pi, "remote", self.fake_remote()),
            mock.patch.object(pi, "reboot_pi") as reboot,
        ):
            pi.kernel_install(self.name, reboot=True)
        reboot.assert_called_once_with(self.conn)

    def test_dry_run_changes_nothing(self) -> None:
        with (
            mock.patch.object(common, "DRY", True),
            mock.patch.object(pi, "connection", return_value=self.conn),
            mock.patch.object(pi, "remote") as remote,
        ):
            pi.kernel_install(self.name)
        remote.assert_not_called()

    def test_stock_removes_the_block_and_forgets_the_install(self) -> None:
        pi.save_pi(kernel={"build": "x", "release": "r"})
        with (
            mock.patch.object(pi, "connection", return_value=self.conn),
            mock.patch.object(pi, "remote", self.fake_remote()),
        ):
            pi.kernel_stock()
        self.assertEqual(self.calls[0][0], pi.STOCK)
        self.assertIsNone(pi.load_pi()["kernel"])

    def test_status_verdicts(self) -> None:
        def status(out: str) -> str:
            with (
                mock.patch.object(pi, "connection", return_value=self.conn),
                mock.patch.object(pi, "remote", return_value=mock.Mock(stdout=out)),
                mock.patch("builtins.print") as printed,
            ):
                pi.kernel_status()
            return "\n".join(str(c.args[0]) for c in printed.call_args_list)

        base = "running=6.18.50+rpt-rpi-v8\nbuilt=#1\nmodel=Raspberry Pi 4 Model B\n"
        mine = "build=pi618\nrelease=6.18.55-klab-pi618\n"
        self.assertIn("stock kernel is selected", status(base + "selected=stock\n"))
        self.assertIn(
            "running your build 'pi618'",
            status("running=6.18.55-klab-pi618\nbuilt=#1\nselected=klab\n" + mine),
        )
        self.assertIn("booted a different one", status(base + "selected=klab\n" + mine))


class TestFetchConfig(unittest.TestCase):
    def test_reads_the_running_kernels_config_and_caches_it(self) -> None:
        conn = pi.Conn("a", "p", "h", 22)
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(pi, "CONFIG_CACHE", Path(d)),
            mock.patch.object(pi, "connection", return_value=conn),
            mock.patch.object(
                pi,
                "remote",
                return_value=mock.Mock(stdout="6.18.50+rpt-rpi-v8\nCONFIG_A=y\n"),
            ),
        ):
            path = pi.fetch_kernel_config()
            self.assertEqual(path.name, "pi-6.18.50+rpt-rpi-v8.config")
            self.assertEqual(path.read_text(), "CONFIG_A=y\n")

    def test_always_asks_for_the_stock_config_not_the_running_one(self) -> None:
        # a running klab kernel must never become the next build's base
        self.assertIn("/boot/config-*-rpi-v8", pi.STOCK_CONFIG)
        self.assertIn("grep -v -- -klab-", pi.STOCK_CONFIG)
        self.assertNotIn("uname", pi.STOCK_CONFIG)
        self.assertNotIn("/proc/config.gz", pi.STOCK_CONFIG)

    def test_stock_config_selection_with_real_sh(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            boot = Path(d)
            for name, body in (
                ("config-6.18.50+rpt-rpi-2712", "CONFIG_PI5=y\n"),
                ("config-6.18.50+rpt-rpi-v8", "CONFIG_PI4=y\n"),
                ("config-6.18.55-klab-mine", "CONFIG_MINE=y\n"),
            ):
                (boot / name).write_text(body)
            script = pi.STOCK_CONFIG.replace("/boot/", f"{d}/")
            out = subprocess.run(
                ["sh", "-c", script], capture_output=True, text=True
            ).stdout
        self.assertEqual(out, "6.18.50+rpt-rpi-v8\nCONFIG_PI4=y\n")

    def test_unreachable_pi_falls_back_to_the_cached_config(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(pi, "CONFIG_CACHE", Path(d)),
        ):
            cached = Path(d) / "pi-6.18.50+rpt-rpi-v8.config"
            cached.write_text("CONFIG_A=y\n")
            with (
                mock.patch.object(pi, "connection", side_effect=LabError("no route")),
                mock.patch.object(pi, "warn") as warn,
            ):
                self.assertEqual(pi.fetch_kernel_config(), cached)
            self.assertIn("using the cached Pi config", warn.call_args.args[0])

    def test_unreachable_pi_without_a_cache_is_an_error(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(pi, "CONFIG_CACHE", Path(d)),
        ):
            with mock.patch.object(pi, "connection", side_effect=LabError("no route")):
                with self.assertRaisesRegex(LabError, "no route"):
                    pi.fetch_kernel_config()

    def test_garbage_is_not_a_config(self) -> None:
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(pi, "CONFIG_CACHE", Path(d)),
        ):
            with (
                mock.patch.object(
                    pi, "connection", return_value=pi.Conn("a", "p", "h", 22)
                ),
                mock.patch.object(
                    pi, "remote", return_value=mock.Mock(stdout="6.18\nnot a config")
                ),
            ):
                with self.assertRaisesRegex(LabError, "did not return a kernel config"):
                    pi.fetch_kernel_config()


class TestHostLookup(unittest.TestCase):
    def test_retries_a_flaky_mdns_lookup(self) -> None:
        answers = [OSError("boom"), OSError("boom"), "192.168.1.50"]

        def lookup(name: str) -> str:
            a = answers.pop(0)
            if isinstance(a, OSError):
                raise a
            return a

        with (
            mock.patch.object(pi.socket, "gethostbyname", side_effect=lookup),
            mock.patch.object(pi.time, "sleep"),
        ):
            self.assertEqual(pi.resolve_host("raspberrypi.local"), "192.168.1.50")

    def test_gives_up_after_the_retries(self) -> None:
        with (
            mock.patch.object(pi.socket, "gethostbyname", side_effect=OSError),
            mock.patch.object(pi.time, "sleep") as sleep,
        ):
            self.assertIsNone(pi.resolve_host("x.local", tries=3))
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
