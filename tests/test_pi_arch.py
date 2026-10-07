"""Unit tests for the Arch Linux ARM paths of `lab pi` (ssh and the Pi are mocked)."""

import contextlib
import gzip
import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from labtool import bench, common, kernel, pi, piarch, pios, pistress
from labtool.common import LabError
from test_pi_kernel import FakePiBuild
from test_piarch import CONFIG, kernel_blob


class ArchPi(unittest.TestCase):
    """A saved Pi config in a temp dir, and a connection to a (mocked) Arch Pi."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for module, attr, value in (
            (pi, "PI_FILE", self.dir / "pi.json"),
            (pi, "CONFIG_CACHE", self.dir / "cache"),
            (kernel, "CONFIG_CACHE", self.dir / "cache"),
        ):
            p = mock.patch.object(module, attr, value)
            p.start()
            self.addCleanup(p.stop)
        pi._OS_CACHE.clear()
        self.conn = pi.Conn("alice", "pw", "10.0.0.5", 22)


class TestDetectOs(ArchPi):
    def detect(self, ids: str) -> tuple[pios.PiOs, mock.Mock]:
        with mock.patch.object(
            pi, "remote", return_value=mock.Mock(stdout=ids)
        ) as remote:
            return pi.detect_os(self.conn), remote

    def test_arch(self) -> None:
        found, _ = self.detect("archarm arch\n")
        self.assertIs(found, pios.ARCH)

    def test_raspberry_pi_os(self) -> None:
        found, _ = self.detect("debian \n")
        self.assertIs(found, pios.DEB)

    def test_a_different_saved_os_is_corrected_once(self) -> None:
        pi.save_pi(os="deb")
        found, _ = self.detect("archarm arch\n")
        self.assertIs(found, pios.ARCH)
        self.assertEqual(pi.load_pi()["os"], "arch")

    def test_it_asks_the_pi_only_once_per_host(self) -> None:
        _, remote = self.detect("archarm arch\n")
        pi.detect_os(self.conn)
        self.assertEqual(remote.call_count, 1)

    def test_dry_run_trusts_the_saved_os_without_ssh(self) -> None:
        pi.save_pi(os="arch")
        with (
            mock.patch.object(common, "DRY", True),
            mock.patch.object(pi, "remote") as remote,
        ):
            self.assertIs(pi.detect_os(self.conn), pios.ARCH)
        remote.assert_not_called()

    def test_an_old_config_without_os_means_raspberry_pi_os(self) -> None:
        with mock.patch.object(common, "DRY", True):
            self.assertIs(pi.detect_os(self.conn), pios.DEB)


class TestArchConfig(ArchPi):
    def fetch(self, image: bytes) -> tuple[Path, mock.Mock]:
        with (
            mock.patch.object(pi, "connection", return_value=self.conn),
            mock.patch.object(pi, "detect_os", return_value=pios.ARCH),
            mock.patch.object(pi, "remote_raw", return_value=image) as raw,
        ):
            return pi.fetch_kernel_config(), raw

    def test_the_config_comes_out_of_the_stock_kernel_image(self) -> None:
        path, raw = self.fetch(gzip.compress(kernel_blob()))
        self.assertEqual(path.name, "arch-7.1.6.config")
        self.assertEqual(path.read_text(), CONFIG)
        self.assertEqual(raw.call_args.args[1], pi.ARCH_STOCK_IMAGE)

    def test_it_never_reads_the_running_kernels_config(self) -> None:
        # once a klab kernel runs, /proc/config.gz would be our own previous build
        self.assertNotIn("config.gz", pi.ARCH_STOCK_IMAGE)
        self.assertNotIn("uname", pi.ARCH_STOCK_IMAGE)
        self.assertIn("/boot/Image", pi.ARCH_STOCK_IMAGE)

    def test_an_image_without_config_is_an_error_without_a_cache(self) -> None:
        with self.assertRaisesRegex(LabError, "no embedded config"):
            self.fetch(b"\0" * 100)

    def test_unreachable_pi_uses_the_cached_config_of_the_saved_os(self) -> None:
        cache = self.dir / "cache"
        cache.mkdir()
        (cache / "arch-7.1.6.config").write_text("CONFIG_A=y\n")
        (cache / "pi-6.18.50+rpt-rpi-v8.config").write_text("CONFIG_B=y\n")
        for saved, want in (
            ("arch", "arch-7.1.6.config"),
            ("deb", "pi-6.18.50+rpt-rpi-v8.config"),
        ):
            pi.save_pi(os=saved)
            with (
                mock.patch.object(pi, "connection", side_effect=LabError("no route")),
                mock.patch.object(pi, "warn"),
            ):
                self.assertEqual(pi.fetch_kernel_config().name, want, saved)

    def test_dry_run_prints_the_command_and_fetches_nothing(self) -> None:
        out = io.StringIO()
        with (
            mock.patch.object(common, "DRY", True),
            mock.patch.object(pi, "connection", return_value=self.conn),
            mock.patch.object(pi, "detect_os", return_value=pios.ARCH),
            mock.patch.object(pi, "remote_raw") as raw,
            contextlib.redirect_stdout(out),
        ):
            pi.fetch_kernel_config()
        raw.assert_not_called()
        self.assertIn("/boot/Image.gz", out.getvalue())


class TestArchBundle(FakePiBuild):
    def test_klab_files_go_to_boot_klab(self) -> None:
        self.make()
        with tarfile.open(pi.make_bundle(self.name, pios.ARCH, "fr")) as tf:
            names = set(tf.getnames())
            script = tf.extractfile("boot/klab/install.sh").read().decode()  # type: ignore[union-attr]
        for part in ("kernel8.img", "bcm2711-rpi-4-b.dtb", "overlays/README", "BUILD"):
            self.assertIn(f"boot/klab/{part}", names, part)
        self.assertFalse([n for n in names if n.startswith("boot/firmware")])
        self.assertIn(f"lib/modules/{self.release}/kernel/drivers/x.ko", names)
        self.assertIn("/boot/klab/cmdline.txt", script)
        self.assertIn("cfg80211.ieee80211_regdom=FR", script)
        self.assertIn(f"depmod -a {self.release}", script)

    def test_the_raspberry_pi_os_bundle_is_unchanged(self) -> None:
        self.make()
        with tarfile.open(pi.make_bundle(self.name)) as tf:
            names = set(tf.getnames())
            script = tf.extractfile("boot/firmware/klab/install.sh").read().decode()  # type: ignore[union-attr]
        self.assertIn("boot/firmware/klab/kernel8.img", names)
        self.assertIn(
            "cp /boot/firmware/cmdline.txt /boot/firmware/klab/cmdline.txt", script
        )


class TestArchKernelCommands(FakePiBuild):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(pi, "PI_FILE", Path(tmp.name) / "pi.json")
        p.start()
        self.addCleanup(p.stop)
        self.conn = pi.Conn("alice", "pw", "10.0.0.5", 22)
        self.calls: list[str] = []
        for name, value in (("connection", self.conn), ("detect_os", pios.ARCH)):
            q = mock.patch.object(pi, name, return_value=value)
            q.start()
            self.addCleanup(q.stop)

    def fake_remote(self, stdout: str = "") -> mock.Mock:
        def remote(conn: pi.Conn, command: str, stdin: Path | None = None) -> mock.Mock:
            self.calls.append(command)
            return mock.Mock(
                stdout="999999999\n" if command.startswith("df ") else stdout
            )

        return mock.Mock(side_effect=remote)

    def test_install_checks_both_filesystems_and_unpacks_into_boot(self) -> None:
        self.make()
        pi.save_pi(wifi_country="de")
        with mock.patch.object(pi, "remote", self.fake_remote()):
            pi.kernel_install(self.name)
        self.assertEqual(self.calls[0], "df --output=avail -B1 /boot | tail -1")
        self.assertEqual(self.calls[1], "df --output=avail -B1 /lib/modules | tail -1")
        self.assertEqual(
            self.calls[2], "sudo tar -xzf - -C / && sudo sh /boot/klab/install.sh"
        )
        self.assertEqual(pi.load_pi()["kernel"]["build"], self.name)

    def test_install_stops_when_the_modules_do_not_fit(self) -> None:
        self.make()

        def remote(conn: pi.Conn, command: str, stdin: Path | None = None) -> mock.Mock:
            self.calls.append(command)
            return mock.Mock(stdout="0\n" if "/lib/modules" in command else "999999999\n")

        with mock.patch.object(pi, "remote", side_effect=remote):
            with self.assertRaisesRegex(
                LabError, "/lib/modules on the Pi has .* the modules needs"
            ):
                pi.kernel_install(self.name)
        self.assertNotIn("sudo tar", " ".join(self.calls))

    def test_stock_edits_boot_config_txt(self) -> None:
        with mock.patch.object(pi, "remote", self.fake_remote()):
            pi.kernel_stock()
        self.assertEqual(self.calls, [pios.ARCH.stock])
        self.assertIn("/boot/config.txt", self.calls[0])

    def test_status_reads_boot_klab(self) -> None:
        printed: list[str] = []
        out = (
            "running=7.1.6-1-aarch64-ARCH\nbuilt=#1 SMP\nmodel=Raspberry Pi 4 Model B\n"
            "os=Arch Linux ARM\nselected=stock\n"
        )
        with (
            mock.patch.object(pi, "remote", self.fake_remote(out)),
            mock.patch(
                "builtins.print", side_effect=lambda *a, **k: printed.append(str(a[0]))
            ),
        ):
            pi.kernel_status()
        self.assertIn("/boot/klab/BUILD", self.calls[0])
        text = "\n".join(printed)
        self.assertIn("Arch Linux ARM", text)
        self.assertIn("stock kernel is selected", text)


class TestSetupDryRun(ArchPi):
    def test_arch_setup_prints_the_pipeline_and_writes_nothing(self) -> None:
        pi.save_pi(
            user="carol",
            password="pw",
            hostname="pi4",
            wifi_ssid="",
            wifi_psk="",
            wifi_country="",
        )
        disk = pi.Disk("/dev/sdX", "sdX", 0, "?", "usb")
        images = self.dir / "images"
        out = io.StringIO()
        with (
            mock.patch.object(common, "DRY", True),
            mock.patch.object(pi, "PI_IMAGES", images),
            mock.patch.object(pi, "choose_disk", return_value=disk),
            mock.patch.object(
                piarch, "fetch_bytes", return_value=b"d41d8cd98f00b204e9800998ecf8427e  x"
            ),
            mock.patch.object(
                piarch,
                "fetch_package",
                return_value=(images / "pkgs" / "sudo.pkg.tar.xz", ["glibc"]),
            ),
            mock.patch.object(pi, "have", return_value=True),
            contextlib.redirect_stdout(out),
        ):
            pi.setup("arch", {})
        text = out.getvalue()
        self.assertIn("ArchLinuxARM-rpi-aarch64-latest.tar.gz", text)
        self.assertIn("md5sum -c", text)
        self.assertIn("labtool.piarch customize", text)
        self.assertIn("mke2fs", text)
        self.assertIn("dd", text)
        self.assertFalse(images.exists())
        self.assertNotIn("installed_at", pi.load_pi())  # nothing was written to a card

    def test_writing_a_card_forgets_the_installed_custom_kernel(self) -> None:
        pi.save_pi(
            user="carol", password="pw", hostname="pi4", wifi_ssid="", wifi_psk="",
            wifi_country="", kernel={"build": "old", "release": "7.2.9-klab-old"},
        )  # fmt: skip
        image = self.dir / "custom.img"
        image.write_bytes(b"x")
        disk = pi.Disk("/dev/sdX", "sdX", 10**10, "?", "usb")
        with (
            mock.patch.object(pi, "choose_disk", return_value=disk),
            mock.patch.object(pi, "_prepare_arch", return_value=image),
            mock.patch.object(pi, "confirm_erase"),
            mock.patch.object(pi, "write_image"),
            mock.patch.object(pi, "KNOWN_HOSTS", self.dir / "kh"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            pi.setup("arch", {})
        cfg = pi.load_pi()
        self.assertIsNone(cfg["kernel"])
        self.assertEqual(cfg["os"], "arch")
        self.assertIn("installed_at", cfg)

    def test_unknown_os(self) -> None:
        with self.assertRaisesRegex(LabError, "unknown OS 'plan9'"):
            pi.setup("plan9", {})

    def test_the_needed_tools_differ_per_os(self) -> None:
        self.assertIn(("fakeroot", "fakeroot"), pi.REQUIRED_TOOLS["arch"])
        self.assertNotIn(("fakeroot", "fakeroot"), pi.REQUIRED_TOOLS["deb"])


class TestStressOnArch(unittest.TestCase):
    def test_install_command_is_pacman(self) -> None:
        self.assertIn("pacman", pios.ARCH.pkg_install)
        self.assertNotIn("apt", pios.ARCH.pkg_install)

    def test_facts_work_without_the_hostname_command_and_on_both_boot_layouts(
        self,
    ) -> None:
        self.assertIn("uname -n", pistress.FACTS)
        self.assertNotIn("$(hostname)", pistress.FACTS)
        self.assertIn("/boot/firmware/klab/BUILD", pistress.FACTS)
        self.assertIn("/boot/klab/BUILD", pistress.FACTS)

    def test_facts_succeed_on_a_stock_kernel_without_a_build_file(self) -> None:
        # the last command must not fail when /boot/klab/BUILD does not exist
        r = subprocess.run(
            ["sh", "-c", pistress.FACTS.replace("/boot/", "/nonexistent/")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_stress_ng_version_ignores_the_kernel_release(self) -> None:
        a = pistress.stress_ng_version(
            "stress-ng, version 0.19.02 (gcc 14.2.0, aarch64 Linux 6.18.55-klab-a)"
        )
        b = pistress.stress_ng_version(
            "stress-ng, version 0.19.02 (gcc 14.2.0, aarch64 Linux 7.1.6-ARCH)"
        )
        self.assertEqual((a, b), ("0.19.02", "0.19.02"))
        self.assertEqual(pistress.stress_ng_version("garbage"), "")

    def test_result_records_the_os(self) -> None:
        facts = {"kernel": "7.1.6-1-aarch64-ARCH", "os": "archarm", "cpus": "4",
                 "stress_ng": "stress-ng, version 0.22.01 (gcc 15)"}  # fmt: skip
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(kernel, "CONFIG_CACHE", Path(d)),
        ):
            r = pistress.build_result(
                "id", None, "quick", 1, 5, 4, facts, {}, {"cpu": [1.0]}
            )
        self.assertEqual(
            (r["os"], r["stress_ng_version"], r["build"]), ("arch", "0.22.01", "stock")
        )


class TestCompareWarnings(unittest.TestCase):
    def runs(self, **second: object) -> list[dict[str, object]]:
        base = {
            "id": "a", "describe": "d", "profile": "p", "config_hash": "c",
            "target": "pi", "guest": {"kernel_release": "k"},
            "vm": {"cpus": 4, "mem": "3790M"},
            "os": "deb", "stress_ng_version": "0.19.02",
            "results": {"stress-ng": {"cpu": {
                "unit": "u", "better": "higher", "values": [1.0]}}},
        }  # fmt: skip
        return [base, {**base, "id": "b", **second}]

    def compare(self, runs: list[dict[str, object]]) -> list[str]:
        warnings: list[str] = []
        with (
            mock.patch.object(bench, "load_run", side_effect=runs),
            mock.patch.object(bench, "resolve_run", side_effect=lambda r: r),
            mock.patch.object(bench, "warn", side_effect=warnings.append),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            bench.compare(["a", "b"])
        return warnings

    def test_same_os_and_version_is_quiet(self) -> None:
        self.assertEqual(self.compare(self.runs()), [])

    def test_different_operating_systems(self) -> None:
        warnings = self.compare(self.runs(os="arch"))
        self.assertTrue(
            any("different operating systems" in w for w in warnings), warnings
        )

    def test_different_stress_ng_versions(self) -> None:
        warnings = self.compare(self.runs(stress_ng_version="0.22.01"))
        self.assertTrue(
            any("0.19.02" in w and "0.22.01" in w for w in warnings), warnings
        )

    def test_x86_runs_are_not_affected(self) -> None:
        runs = self.runs()
        for r in runs:
            r.pop("os")
            r.pop("target")
            r.pop("stress_ng_version")
        self.assertEqual(self.compare(runs), [])


if __name__ == "__main__":
    unittest.main()
