"""Unit tests for the per-OS profiles of `lab pi` (the generated shell is really run)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from labtool import pios

STOCK_CONFIG_TXT = "dtparam=audio=on\n[cm4]\notg_mode=1\n[all]\n"


def sh(
    script: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, env=env or os.environ
    )


class TestProfiles(unittest.TestCase):
    def test_paths(self) -> None:
        self.assertEqual(pios.DEB.fw, "/boot/firmware")
        self.assertEqual(pios.DEB.klab, "/boot/firmware/klab")
        self.assertEqual(pios.DEB.bundle_prefix, "boot/firmware/klab")
        self.assertEqual(pios.ARCH.fw, "/boot")
        self.assertEqual(pios.ARCH.klab, "/boot/klab")
        self.assertEqual(pios.ARCH.bundle_prefix, "boot/klab")
        self.assertEqual(pios.ARCH.config_txt, "/boot/config.txt")

    def test_registry(self) -> None:
        self.assertEqual(set(pios.OSES), {"deb", "arch"})
        self.assertIs(pios.OSES[pios.DEFAULT_OS], pios.DEB)

    def test_detects_the_os_from_os_release_ids(self) -> None:
        for ids, want in (
            ("archarm arch", pios.ARCH),
            ("arch ", pios.ARCH),
            ("archarm", pios.ARCH),
            ('"archarm" "arch"', pios.ARCH),
            ("raspbian debian", pios.DEB),
            ("debian ", pios.DEB),
            ("unknown ", pios.DEB),
            ("", pios.DEB),
        ):
            self.assertIs(pios.from_os_release(ids), want, ids)

    def test_install_command_per_package_manager(self) -> None:
        self.assertIn("apt-get", pios.DEB.pkg_install)
        self.assertIn("pacman -Syu --noconfirm --needed stress-ng", pios.ARCH.pkg_install)

    def test_unpack_and_stock_use_the_right_directory(self) -> None:
        self.assertIn("/boot/firmware/klab/install.sh", pios.DEB.unpack)
        self.assertIn("/boot/klab/install.sh", pios.ARCH.unpack)
        self.assertIn("/boot/firmware/config.txt", pios.DEB.stock)
        self.assertIn("/boot/config.txt", pios.ARCH.stock)
        self.assertNotIn("/boot/firmware", pios.ARCH.stock)
        self.assertIn("/boot/klab/BUILD", pios.ARCH.status_script)
        self.assertIn("/boot/firmware/klab/BUILD", pios.DEB.status_script)


class TestScripts(unittest.TestCase):
    def test_valid_shell(self) -> None:
        for os_ in pios.OSES.values():
            for script in (
                os_.install_script("6.18.55-klab-x", "fr"),
                os_.install_script("6.18.55-klab-x"),
                os_.stock,
                os_.status_script,
                os_.unpack,
            ):
                self.assertEqual(sh(f"sh -n -c {_q(script)}").returncode, 0, script)

    def test_config_block_is_idempotent_and_removable_on_both(self) -> None:
        for os_ in pios.OSES.values():
            with tempfile.TemporaryDirectory() as d:
                cfg = Path(d) / "config.txt"
                cfg.write_text(STOCK_CONFIG_TXT)
                for _ in range(2):
                    r = sh(os_.config_edit_script(str(cfg)))
                    self.assertEqual(r.returncode, 0, r.stderr)
                text = cfg.read_text()
                self.assertEqual(text.count("os_prefix=klab/"), 1, os_.name)
                self.assertEqual(text.count("auto_initramfs=0"), 1)
                self.assertTrue(text.startswith(STOCK_CONFIG_TXT))
                sh(os_.strip_block(str(cfg)))
                self.assertEqual(cfg.read_text().strip(), STOCK_CONFIG_TXT.strip())

    def run_install(
        self, os_: pios.PiOs, country: str | None, partuuid: str
    ) -> tuple[Path, Path]:
        """Run install.sh against a fake boot directory with stubbed depmod/findmnt."""
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(d)]))
        boot = d / "boot"
        (boot / "klab").mkdir(parents=True)
        fw = boot / "firmware" if os_ is pios.DEB else boot
        (fw / "klab").mkdir(parents=True, exist_ok=True)
        (fw / "config.txt").write_text(STOCK_CONFIG_TXT)
        (fw / "cmdline.txt").write_text("root=PARTUUID=cf638bb3-02 rootwait\n")
        bin_ = d / "bin"
        bin_.mkdir()
        (bin_ / "depmod").write_text(
            "#!/bin/sh\necho depmod \"$@\" >> '%s'\n" % (d / "log")
        )
        (bin_ / "findmnt").write_text(f"#!/bin/sh\necho {partuuid}\n")
        for f in bin_.iterdir():
            f.chmod(0o755)
        script = os_.install_script("6.18.55-klab-x", country).replace(str(boot), "")
        script = script.replace("/boot", str(boot))
        env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}
        r = sh(script, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("depmod -a 6.18.55-klab-x", (d / "log").read_text())
        return fw / "klab", fw / "config.txt"

    def test_arch_install_writes_a_cmdline_for_the_root_partition(self) -> None:
        klab, cfg = self.run_install(pios.ARCH, "fr", "1a2b3c4d-02")
        cmdline = (klab / "cmdline.txt").read_text()
        self.assertIn("root=PARTUUID=1a2b3c4d-02", cmdline)
        for part in ("rootfstype=ext4", "rw", "rootwait", "cfg80211.ieee80211_regdom=FR"):
            self.assertIn(part, cmdline)
        self.assertEqual(cmdline.count("\n"), 1)  # one line, as the firmware wants
        self.assertIn("os_prefix=klab/", cfg.read_text())

    def test_arch_cmdline_without_a_country_has_no_regdom(self) -> None:
        klab, _ = self.run_install(pios.ARCH, None, "1a2b3c4d-02")
        self.assertNotIn("regdom", (klab / "cmdline.txt").read_text())

    def test_arch_falls_back_to_the_device_when_there_is_no_partuuid(self) -> None:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(d)]))
        (d / "klab").mkdir()
        (d / "config.txt").write_text(STOCK_CONFIG_TXT)
        (d / "bin").mkdir()
        (d / "bin" / "depmod").write_text("#!/bin/sh\n")
        (d / "bin" / "findmnt").write_text(
            '#!/bin/sh\ncase "$*" in *PARTUUID*) echo;; *) echo /dev/mmcblk0p2;; esac\n'
        )
        for f in (d / "bin").iterdir():
            f.chmod(0o755)
        script = pios.ARCH.install_script("r").replace("/boot", str(d))
        r = sh(script, {**os.environ, "PATH": f"{d / 'bin'}:{os.environ['PATH']}"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("root=/dev/mmcblk0p2", (d / "klab" / "cmdline.txt").read_text())

    def test_deb_install_copies_the_live_cmdline(self) -> None:
        klab, _ = self.run_install(pios.DEB, "fr", "ignored")
        self.assertEqual(
            (klab / "cmdline.txt").read_text(), "root=PARTUUID=cf638bb3-02 rootwait\n"
        )

    def test_stock_removes_the_block_only_when_present(self) -> None:
        for os_ in pios.OSES.values():
            with tempfile.TemporaryDirectory() as d:
                cfg = Path(d) / "config.txt"
                cfg.write_text(STOCK_CONFIG_TXT)
                sudo = Path(d) / "sudo"
                sudo.write_text('#!/bin/sh\nexec "$@"\n')
                sudo.chmod(0o755)
                env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}"}
                cmd = os_.stock.replace(os_.config_txt, str(cfg))
                cmd = cmd.replace(os_.strip_block(), os_.strip_block(str(cfg)))
                before = cfg.stat().st_mtime_ns
                self.assertEqual(sh(cmd, env).returncode, 0)
                self.assertEqual(
                    cfg.stat().st_mtime_ns, before, "no block: file untouched"
                )
                sh(os_.config_edit_script(str(cfg)))
                self.assertIn("klab begin", cfg.read_text())
                self.assertEqual(sh(cmd, env).returncode, 0)
                self.assertNotIn("klab", cfg.read_text())


def _q(s: str) -> str:
    import shlex

    return shlex.quote(s)


if __name__ == "__main__":
    unittest.main()
