"""Unit tests for the Arch Linux ARM side of `lab pi` (logic plus a tiny real image)."""

import gzip
import hashlib
import io
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from labtool import common, piarch
from labtool.common import LabError

CONFIG = (
    "#\n# Automatically generated file; DO NOT EDIT.\n"
    "# Linux/arm64 7.1.6 Kernel Configuration\n#\nCONFIG_IKCONFIG=y\nCONFIG_EXT4_FS=y\n"
)

PASSWD = (
    "root:x:0:0::/root:/usr/bin/bash\nnobody:x:65534:65534:Kernel Overflow User:/:"
    "/usr/bin/nologin\nalarm:x:1000:1000::/home/alarm:/bin/bash\n"
)
SHADOW = (
    "root:$y$j9T$oldroothash:19999:0:99999:7:::\n"
    "alarm:$y$j9T$oldalarmhash:19999:0:99999:7:::\n"
)
GROUP = "root:x:0:\nwheel:x:998:alarm\nusers:x:985:alarm,bob\nalarm:x:1000:\n"
GSHADOW = "root:!::\nwheel:!::alarm\nusers:!::alarm,bob\nalarm:!::\n"
NEW_HASH = "$6$salt$newhash"


def kernel_blob(config: str = CONFIG) -> bytes:
    """Bytes shaped like a kernel image that embeds its config."""
    return (
        b"\0" * 64
        + b"IKCFG_ST"
        + gzip.compress(config.encode())
        + b"IKCFG_ED"
        + b"\0" * 32
    )


class TestIkconfig(unittest.TestCase):
    def test_raw_image(self) -> None:
        self.assertEqual(piarch.extract_ikconfig(kernel_blob()), CONFIG)

    def test_gzipped_image(self) -> None:
        self.assertEqual(piarch.extract_ikconfig(gzip.compress(kernel_blob())), CONFIG)

    def test_skips_a_marker_that_is_not_followed_by_a_config(self) -> None:
        blob = b"junk IKCFG_ST not gzip data " + kernel_blob()
        self.assertEqual(piarch.extract_ikconfig(blob), CONFIG)

    def test_no_embedded_config(self) -> None:
        with self.assertRaisesRegex(LabError, "CONFIG_IKCONFIG=y"):
            piarch.extract_ikconfig(b"\0" * 1000)

    def test_broken_gzip(self) -> None:
        with self.assertRaisesRegex(LabError, "not valid gzip"):
            piarch.extract_ikconfig(b"\x1f\x8b\x08 truncated")

    def test_version(self) -> None:
        self.assertEqual(piarch.config_version(CONFIG), "7.1.6")
        self.assertEqual(piarch.config_version("CONFIG_A=y\n"), "unknown")


class TestWifiKey(unittest.TestCase):
    def test_ieee_80211i_test_vectors(self) -> None:
        for ssid, phrase, key in (
            (
                "IEEE",
                "password",
                "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e",
            ),
            (
                "ThisIsASSID",
                "ThisIsAPassword",
                "0dc0d6eb90555ed6419756b9a15ec3e3209b63df707dd508d14581f8982721af",
            ),
        ):
            self.assertEqual(piarch.wpa_psk_hex(ssid, phrase), key, ssid)

    def test_matches_openssls_pbkdf2_for_non_ascii_names(self) -> None:
        args = ["openssl", "kdf", "-keylen", "32", "-kdfopt", "digest:SHA1"]
        ssid, phrase = "Café 5G", "pässwörd 123"
        r = subprocess.run(
            [*args, "-kdfopt", f"pass:{phrase}", "-kdfopt", f"salt:{ssid}",
             "-kdfopt", "iter:4096", "PBKDF2"],
            capture_output=True, text=True,
        )  # fmt: skip
        if r.returncode != 0:
            self.skipTest("this openssl has no `kdf` command")
        want = r.stdout.strip().replace(":", "").lower()
        self.assertEqual(piarch.wpa_psk_hex(ssid, phrase), want)

    def test_a_64_digit_key_is_used_as_it_is(self) -> None:
        key = "AB" * 32
        self.assertEqual(piarch.wpa_psk_hex("net", key), key.lower())

    def test_wpa_conf(self) -> None:
        ssid = 'My "Net" #1'
        text = piarch.wpa_conf(ssid, "ab" * 32, "fr")
        self.assertIn("country=FR", text)
        self.assertIn("ssid=" + ssid.encode().hex(), text)  # hex: nothing to escape
        self.assertIn(f"psk={'ab' * 32}", text)
        self.assertNotIn(
            "My", text
        )  # the name and the passphrase are never in clear text

    def test_open_network(self) -> None:
        text = piarch.wpa_conf("Cafe", "", "de")
        self.assertIn("key_mgmt=NONE", text)
        self.assertNotIn("psk=", text)


class TestAccounts(unittest.TestCase):
    def rename(self, new: str = "carol") -> tuple[str, str, str, str]:
        return piarch.rename_user(PASSWD, SHADOW, GROUP, GSHADOW, new, NEW_HASH, 20000)

    def test_user_is_renamed_with_home_and_password(self) -> None:
        passwd, shadow, group, gshadow = self.rename()
        self.assertIn("carol:x:1000:1000::/home/carol:/bin/bash", passwd)
        self.assertNotIn("alarm", passwd)
        self.assertIn(f"carol:{NEW_HASH}:20000:0:99999:7:::", shadow)
        self.assertNotIn("alarm", shadow + group + gshadow)
        self.assertIn("wheel:x:998:carol\n", group)
        self.assertIn("users:x:985:carol,bob\n", group)
        self.assertIn("carol:x:1000:\n", group)
        self.assertIn("wheel:!::carol\n", gshadow)

    def test_other_accounts_are_untouched(self) -> None:
        passwd, shadow, group, _ = self.rename()
        self.assertIn("root:x:0:0::/root:/usr/bin/bash", passwd)
        self.assertIn("nobody:x:65534", passwd)
        self.assertIn("root:$y$j9T$oldroothash", shadow)
        self.assertIn("root:x:0:\n", group)

    def test_keeping_the_name_alarm_only_changes_the_password(self) -> None:
        passwd, shadow, _, _ = self.rename("alarm")
        self.assertEqual(passwd, PASSWD)
        self.assertIn(f"alarm:{NEW_HASH}:20000", shadow)

    def test_root_is_locked(self) -> None:
        locked = piarch.lock_root(SHADOW)
        self.assertIn("root:!*:19999:0:99999:7:::", locked)
        self.assertIn("alarm:$y$j9T$oldalarmhash", locked)


class TestFiles(unittest.TestCase):
    def test_small_texts(self) -> None:
        self.assertIn("127.0.1.1\tpi4.localdomain\tpi4", piarch.hosts_text("pi4"))
        self.assertIn("PARTUUID=1a2b3c4d-01  /boot  vfat", piarch.fstab_text("1a2b3c4d"))
        self.assertNotIn("mmcblk", piarch.fstab_text("1a2b3c4d"))
        self.assertEqual(
            piarch.sudoers_text("carol"), "carol ALL=(ALL:ALL) NOPASSWD: ALL\n"
        )

    def test_mdns_is_added_once(self) -> None:
        net = "[Match]\nName=eth*\n\n[Network]\nDHCP=yes\nDNSSEC=no\n"
        once = piarch.add_mdns(net)
        self.assertIn("DHCP=yes\nMulticastDNS=yes\nDNSSEC=no", once)
        self.assertEqual(piarch.add_mdns(once), once)

    def test_first_boot_scripts_are_valid_shell_and_do_what_they_promise(self) -> None:
        for text in (piarch.FIRSTBOOT_SCRIPT, piarch.packages_script(("htop", "vim"))):
            self.assertEqual(
                subprocess.run(["sh", "-n"], input=text, text=True).returncode, 0
            )
        self.assertIn(
            "pacman -U --noconfirm --needed /var/cache/klab/", piarch.FIRSTBOOT_SCRIPT
        )
        self.assertIn("sfdisk --no-reread -N", piarch.FIRSTBOOT_SCRIPT)
        self.assertIn("resize2fs", piarch.FIRSTBOOT_SCRIPT)
        self.assertIn("pacman-key --populate archlinuxarm", piarch.FIRSTBOOT_SCRIPT)
        self.assertIn(
            "pacman -Syu --noconfirm --needed htop vim",
            piarch.packages_script(("htop", "vim")),
        )

    def test_the_package_step_is_retried_until_it_works(self) -> None:
        self.assertIn("Restart=on-failure", piarch.PACKAGES_SERVICE)
        self.assertIn("StartLimitIntervalSec=0", piarch.PACKAGES_SERVICE)
        self.assertIn(
            "ConditionPathExists=!/var/lib/klab-packages.done", piarch.PACKAGES_SERVICE
        )
        self.assertIn("network-online.target", piarch.PACKAGES_SERVICE)
        self.assertNotIn("network-online", piarch.FIRSTBOOT_SERVICE)  # part 1 needs none


class TestLayout(unittest.TestCase):
    def test_sizes_and_alignment(self) -> None:
        layout = piarch.plan_layout(2154 * piarch.MIB)
        self.assertEqual(layout.boot_start, 2048)
        self.assertEqual(layout.root_start, 2048 + 512 * 2048)
        self.assertEqual(layout.root_sectors % 2048, 0)
        self.assertGreater(layout.root_sectors * 512, 2154 * piarch.MIB * 1.15)
        self.assertLess(layout.total_bytes, 3.8e9)  # about 3.5 GB: quick to flash

    def test_fat32_always_gets_enough_clusters(self) -> None:
        for mib in (64, 256, 512, 1024, 4096):
            size = mib * piarch.MIB
            spc = piarch.fat_cluster_sectors(size)
            self.assertGreaterEqual(size // (512 * spc), 65525 + 10000, f"{mib} MiB")
            self.assertEqual(spc & (spc - 1), 0, "a power of two")
        self.assertEqual(
            piarch.fat_cluster_sectors(512 * piarch.MIB), 8
        )  # 4 KiB clusters

    def test_sfdisk_accepts_the_script(self) -> None:
        if not shutil.which("sfdisk"):
            self.skipTest("sfdisk missing")
        layout = piarch.plan_layout(100 * piarch.MIB)
        with tempfile.NamedTemporaryFile() as img:
            img.truncate(layout.total_bytes)
            r = subprocess.run(
                ["sfdisk", "--quiet", "--no-reread", "--no-tell-kernel", img.name],
                input=piarch.sfdisk_script(layout, "1a2b3c4d"),
                text=True,
                capture_output=True,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            dump = subprocess.run(
                ["sfdisk", "-d", img.name], capture_output=True, text=True
            ).stdout
        self.assertIn("label-id: 0x1a2b3c4d", dump)
        self.assertRegex(dump, r"start=\s*2048, size=\s*1048576, type=c")
        self.assertRegex(
            dump, rf"start=\s*{layout.root_start}, size=\s*{layout.root_sectors}, type=83"
        )

    def test_assembly_order(self) -> None:
        layout = piarch.plan_layout(100 * piarch.MIB)
        script = piarch.assembly_script(
            Path("/t/a.tar.gz"),
            Path("/s"),
            Path("/i.img"),
            Path("/spec"),
            Path("/sf"),
            layout,
        )
        steps = [
            "tar -xpf",
            "labtool.piarch customize",
            "truncate -s",
            "sfdisk",
            "mkfs.vfat",
            "mcopy",
            "rm -rf /s/boot/*",
            "mke2fs",
        ]
        positions = [script.index(s) for s in steps]
        self.assertEqual(positions, sorted(positions), "steps out of order")
        self.assertIn(f"--offset {layout.boot_start}", script)
        self.assertIn("-s 8 ", script)  # explicit FAT cluster size
        self.assertIn(f"@@{layout.boot_start * 512}", script)
        self.assertIn(f"offset={layout.root_start * 512}", script)
        self.assertIn("-d /s", script)


class TestPackages(unittest.TestCase):
    def core_db(self, sha: str = "00") -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, desc in (
                (
                    "sudo-1.9.17.p2-6",
                    f"%NAME%\nsudo\n\n%FILENAME%\nsudo-1.9.17.p2-6-aarch64.pkg.tar.xz\n\n%SHA256SUM%\n{sha}\n\n%DEPENDS%\nglibc\npam\nlibcrypto.so=3-64\n",
                ),
                (
                    "sudo-rs-0.2-1",
                    "%NAME%\nsudo-rs\n\n%FILENAME%\nsudo-rs.pkg\n\n%SHA256SUM%\nff\n",
                ),
            ):
                data = desc.encode()
                ti = tarfile.TarInfo(f"{name}/desc")
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    def test_find_package_matches_the_exact_name(self) -> None:
        desc = piarch.find_package(self.core_db(), "sudo")
        self.assertEqual(desc["FILENAME"], ["sudo-1.9.17.p2-6-aarch64.pkg.tar.xz"])
        self.assertEqual(desc["DEPENDS"], ["glibc", "pam", "libcrypto.so=3-64"])
        with self.assertRaisesRegex(LabError, "not in the Arch Linux ARM repository"):
            piarch.find_package(self.core_db(), "nope")

    def fetch(self, content: bytes, sha: str) -> tuple[Path, list[str]]:
        images = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(images, ignore_errors=True))

        def fake_download(url: str, dest: Path, what: str) -> None:
            dest.write_bytes(content)

        with (
            mock.patch.object(piarch, "fetch_bytes", return_value=self.core_db(sha)),
            mock.patch.object(piarch, "download", fake_download),
        ):
            return piarch.fetch_package(images, "sudo")

    def test_a_good_download_is_verified_and_returns_the_dependencies(self) -> None:
        content = b"package bytes"
        path, deps = self.fetch(content, hashlib.sha256(content).hexdigest())
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(deps, ["glibc", "pam", "libcrypto.so=3-64"])

    def test_a_corrupt_download_is_rejected_and_removed(self) -> None:
        with self.assertRaisesRegex(LabError, "SHA-256 mismatch"):
            self.fetch(b"package bytes", "00" * 32)

    def make_root(self) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        for name, extra in (
            ("glibc-2.43-1", ""),
            ("openssl-3.6.3-1", "%PROVIDES%\nlibcrypto.so=3-64\n"),
        ):
            d = root / "var/lib/pacman/local" / name
            d.mkdir(parents=True)
            (d / "desc").write_text(f"%NAME%\n{name.rsplit('-', 2)[0]}\n\n{extra}")
        return root

    def test_dependencies_are_satisfied_by_names_and_provides(self) -> None:
        root = self.make_root()
        self.assertEqual(
            piarch.missing_dependencies(["glibc", "libcrypto.so=3-64"], root), []
        )
        self.assertEqual(
            piarch.missing_dependencies(["glibc", "pam>=1.7"], root), ["pam>=1.7"]
        )


def make_tarball(path: Path) -> None:
    """A tiny stand-in for the Arch Linux ARM tarball."""
    with tarfile.open(path, "w:gz") as tf:

        def d(name: str, uid: int = 0, mode: int = 0o755) -> None:
            ti = tarfile.TarInfo(name)
            ti.type, ti.mode, ti.uid, ti.gid = tarfile.DIRTYPE, mode, uid, uid
            tf.addfile(ti)

        def f(name: str, text: str, mode: int = 0o644, uid: int = 0) -> None:
            data = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.uid, ti.gid = len(data), mode, uid, uid
            tf.addfile(ti, io.BytesIO(data))

        def link(name: str, target: str) -> None:
            ti = tarfile.TarInfo(name)
            ti.type, ti.linkname = tarfile.SYMTYPE, target
            tf.addfile(ti)

        for name in (
            "etc",
            "usr",
            "usr/bin",
            "usr/lib",
            "var",
            "boot",
            "boot/dtbs",
            "boot/dtbs/broadcom",
        ):
            d(name)
        d("home")
        d("home/alarm", uid=1000, mode=0o700)
        link("lib", "usr/lib")
        link("bin", "usr/bin")
        f("etc/passwd", PASSWD)
        f("etc/shadow", SHADOW, 0o600)
        f("etc/group", GROUP)
        f("etc/gshadow", GSHADOW, 0o600)
        f("etc/hostname", "alarm\n")
        f("etc/fstab", "/dev/mmcblk0p1  /boot   vfat    defaults        0       0\n")
        for d_ in (
            "etc/systemd",
            "etc/systemd/network",
            "etc/ssh",
            "etc/ssh/sshd_config.d",
        ):
            d(d_)
        f(
            "etc/systemd/network/eth.network",
            "[Match]\nName=eth*\n\n[Network]\nDHCP=yes\n",
        )
        f("etc/systemd/network/en.network", "[Match]\nName=en*\n\n[Network]\nDHCP=yes\n")
        f("etc/ssh/sshd_config.d/99-archlinux.conf", "UsePAM yes\n")
        for d_ in (
            "var/lib",
            "var/lib/pacman",
            "var/lib/pacman/local",
            "var/lib/pacman/local/glibc-2.43-1",
            "var/lib/pacman/local/openssl-3.6-1",
        ):
            d(d_)
        f("var/lib/pacman/local/glibc-2.43-1/desc", "%NAME%\nglibc\n")
        f(
            "var/lib/pacman/local/openssl-3.6-1/desc",
            "%NAME%\nopenssl\n\n%PROVIDES%\nlibcrypto.so=3-64\n",
        )
        f("usr/bin/tool", "#!/bin/sh\n", 0o755)
        f("boot/kernel8.img", "U-Boot " * 100)
        f("boot/config.txt", "enable_uart=1\n")
        f("boot/boot.txt", "# boot script\n")
        f("boot/dtbs/broadcom/bcm2711-rpi-4-b.dtb", "dtb")


NEEDED = (
    "fakeroot",
    "mke2fs",
    "mkfs.vfat",
    "mcopy",
    "sfdisk",
    "debugfs",
    "truncate",
    "tar",
)


@unittest.skipUnless(all(shutil.which(t) for t in NEEDED), "image-building tools missing")
class TestImageBuild(unittest.TestCase):
    """Builds a real (tiny) image the way `lab pi setup arch` does and inspects it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp())
        tarball = cls.tmp / "alarm.tar.gz"
        make_tarball(tarball)
        sudo_pkg = cls.tmp / "sudo-1.9-6-aarch64.pkg.tar.xz"
        sudo_pkg.write_bytes(b"not a real package")
        cls.spec = piarch.Spec(
            user="carol",
            pw_hash=NEW_HASH,
            hostname="pi4",
            label_id="1a2b3c4d",
            wifi_ssid="Home",
            wifi_psk_hex=piarch.wpa_psk_hex("Home", "wifipassword"),
            wifi_country="fr",
            sudo_package=str(sudo_pkg),
            sudo_deps=("glibc", "libcrypto.so=3-64"),
        )
        cls.image = cls.tmp / "out" / "custom.img"
        with (
            mock.patch.object(common, "check_disk"),
            mock.patch.object(piarch, "check_disk"),
        ):
            piarch.build_image(tarball, cls.spec, cls.image, rootfs_bytes=10 * piarch.MIB)
        cls.layout = piarch.plan_layout(10 * piarch.MIB)
        cls.root = f"{cls.image}?offset={cls.layout.root_start * 512}"
        cls.boot = f"{cls.image}@@{cls.layout.boot_start * 512}"

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def debugfs(self, cmd: str) -> str:
        r = subprocess.run(
            ["debugfs", "-R", cmd, self.root], capture_output=True, text=True
        )
        return r.stdout

    def stat(self, path: str) -> tuple[str, int, int]:
        out = self.debugfs(f"stat {path}")
        mode = re.search(r"Mode:\s+(\d+)", out)
        user = re.search(r"User:\s+(\d+)\s+Group:\s+(\d+)", out)
        kind = re.search(r"Type:\s+(\w+)", out)
        assert mode and user and kind, out
        return mode.group(1), int(user.group(1)), kind.group(1)

    def test_no_temporary_files_are_left_behind(self) -> None:
        self.assertEqual([p.name for p in (self.tmp / "out").iterdir()], ["custom.img"])

    def test_partition_table(self) -> None:
        dump = subprocess.run(
            ["sfdisk", "-d", str(self.image)], capture_output=True, text=True
        ).stdout
        self.assertIn("label-id: 0x1a2b3c4d", dump)
        self.assertEqual(self.image.stat().st_size, self.layout.total_bytes)

    def mread(self, path: str) -> str:
        r = subprocess.run(
            ["mcopy", "-i", self.boot, f"::{path}", "-"], capture_output=True
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.decode()

    def test_boot_partition_holds_the_boot_files(self) -> None:
        self.assertIn("U-Boot", self.mread("kernel8.img"))
        self.assertEqual(self.mread("config.txt"), "enable_uart=1\n")
        self.assertEqual(self.mread("dtbs/broadcom/bcm2711-rpi-4-b.dtb"), "dtb")
        label = subprocess.run(
            ["minfo", "-i", self.boot, "::"], capture_output=True, text=True
        )
        self.assertIn(
            "BOOT",
            subprocess.run(
                ["mdir", "-i", self.boot, "::"], capture_output=True, text=True
            ).stdout,
        )
        self.assertEqual(label.returncode, 0)

    def test_boot_directory_of_the_root_partition_is_empty(self) -> None:
        entries = re.findall(
            r"/\d+/\d+/\d+/\d+/\d+/([^/]*)/", self.debugfs("ls -p /boot")
        )
        self.assertEqual(self.stat("/boot")[2], "directory")
        self.assertEqual([n for n in entries if n not in (".", "..")], [])

    def test_accounts_and_ownership(self) -> None:
        passwd = self.debugfs("cat /etc/passwd")
        self.assertIn("carol:x:1000:1000::/home/carol:/bin/bash", passwd)
        self.assertNotIn("alarm", passwd)
        self.assertIn(f"carol:{NEW_HASH}:", self.debugfs("cat /etc/shadow"))
        self.assertIn("root:!*:", self.debugfs("cat /etc/shadow"))
        self.assertEqual(
            self.stat("/etc/shadow")[:2], ("0600", 0)
        )  # root-owned (fakeroot)
        self.assertEqual(
            self.stat("/home/carol")[1], 1000
        )  # the user's home belongs to the user
        self.assertEqual(self.stat("/usr/bin/tool")[:2], ("0755", 0))

    def test_files_are_where_the_first_boot_expects_them(self) -> None:
        self.assertEqual(self.debugfs("cat /etc/hostname").strip(), "pi4")
        self.assertIn("PARTUUID=1a2b3c4d-01", self.debugfs("cat /etc/fstab"))
        self.assertIn(
            "PasswordAuthentication yes",
            self.debugfs("cat /etc/ssh/sshd_config.d/10-klab.conf"),
        )
        self.assertEqual(self.stat("/etc/sudoers.d/10-klab")[:2], ("0440", 0))
        self.assertIn(
            "carol ALL=(ALL:ALL) NOPASSWD: ALL",
            self.debugfs("cat /etc/sudoers.d/10-klab"),
        )
        self.assertIn(
            "MulticastDNS=yes", self.debugfs("cat /etc/systemd/network/eth.network")
        )
        self.assertEqual(
            self.stat("/etc/wpa_supplicant/wpa_supplicant-wlan0.conf")[:2], ("0600", 0)
        )
        self.assertIn(
            "country=FR",
            self.debugfs("cat /etc/wpa_supplicant/wpa_supplicant-wlan0.conf"),
        )
        self.assertNotIn(
            "wifipassword",
            self.debugfs("cat /etc/wpa_supplicant/wpa_supplicant-wlan0.conf"),
        )
        self.assertEqual(self.stat("/usr/local/sbin/klab-firstboot")[:2], ("0755", 0))
        self.assertIn("pacman -Syu", self.debugfs("cat /usr/local/sbin/klab-packages"))
        self.assertEqual(self.stat("/var/cache/klab/sudo-1.9-6-aarch64.pkg.tar.xz")[1], 0)

    def test_services_are_enabled(self) -> None:
        wants = "/etc/systemd/system/multi-user.target.wants"
        for unit in (
            "wpa_supplicant@wlan0.service",
            "klab-firstboot.service",
            "klab-packages.service",
        ):
            self.assertEqual(self.stat(f"{wants}/{unit}")[2], "symlink", unit)

    def test_unpacked_symlinks_survive(self) -> None:
        self.assertEqual(self.stat("/lib")[2], "symlink")
        self.assertEqual(self.stat("/bin")[2], "symlink")


class TestImageBuildErrors(unittest.TestCase):
    @unittest.skipUnless(
        all(shutil.which(t) for t in NEEDED), "image-building tools missing"
    )
    def test_missing_sudo_dependency_stops_the_build(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            make_tarball(tmp / "a.tar.gz")
            (tmp / "sudo.pkg").write_bytes(b"x")
            spec = piarch.Spec(
                "carol",
                NEW_HASH,
                "pi",
                "1a2b3c4d",
                sudo_package=str(tmp / "sudo.pkg"),
                sudo_deps=("pam",),
            )
            with mock.patch.object(piarch, "check_disk"):
                with self.assertRaisesRegex(LabError, "pam"):
                    piarch.build_image(
                        tmp / "a.tar.gz",
                        spec,
                        tmp / "o" / "i.img",
                        rootfs_bytes=piarch.MIB,
                    )
            self.assertEqual(
                list((tmp / "o").glob("arch-stage-*")), [], "staging dir removed"
            )

    def test_dry_run_prints_the_pipeline_and_creates_nothing(self) -> None:
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d, mock.patch.object(common, "DRY", True):
            spec = piarch.Spec("carol", NEW_HASH, "pi", "1a2b3c4d")
            with mock.patch("sys.stdout", out):
                piarch.build_image(Path(d) / "a.tar.gz", spec, Path(d) / "w" / "i.img")
            self.assertFalse((Path(d) / "w").exists())
        text = out.getvalue()
        self.assertIn("mke2fs", text)
        self.assertIn("labtool.piarch customize", text)


class TestTarball(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.remote = self.dir / "remote.tar.gz"
        self.remote.write_bytes(b"tarball bytes")
        self.md5 = hashlib.md5(b"tarball bytes").hexdigest()
        (self.dir / "remote.tar.gz.md5").write_text(f"{self.md5}  remote.tar.gz\n")
        p = mock.patch.object(piarch, "ARCH_URL", self.remote.as_uri())
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(piarch, "TARBALL", "cache.tar.gz")
        q.start()
        self.addCleanup(q.stop)
        r = mock.patch.object(piarch, "check_disk")
        r.start()
        self.addCleanup(r.stop)
        self.images = self.dir / "images"

    def test_downloads_and_verifies(self) -> None:
        path = piarch.fetch_tarball(self.images)
        self.assertEqual(path.read_bytes(), b"tarball bytes")

    def test_a_current_cached_copy_is_not_downloaded_again(self) -> None:
        piarch.fetch_tarball(self.images)
        with mock.patch.object(piarch, "download") as dl:
            piarch.fetch_tarball(self.images)
        dl.assert_not_called()

    def test_a_changed_latest_tarball_is_fetched_again(self) -> None:
        piarch.fetch_tarball(self.images)
        self.remote.write_bytes(b"newer tarball")
        (self.dir / "remote.tar.gz.md5").write_text(
            f"{hashlib.md5(b'newer tarball').hexdigest()}  x\n"
        )
        self.assertEqual(piarch.fetch_tarball(self.images).read_bytes(), b"newer tarball")

    def test_a_corrupt_download_is_rejected(self) -> None:
        (self.dir / "remote.tar.gz.md5").write_text("0" * 32 + "  x\n")
        with self.assertRaisesRegex(LabError, "MD5 mismatch"):
            piarch.fetch_tarball(self.images)
        self.assertFalse((self.images / "cache.tar.gz").exists())


if __name__ == "__main__":
    unittest.main()
