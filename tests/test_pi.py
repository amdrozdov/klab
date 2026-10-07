"""Unit tests for `lab pi` (pure logic: no network, no devices, no ssh)."""

import json
import os
import stat
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from labtool import clean, common, pi
from labtool.common import LabError


def mbr(*parts: tuple[int, int, int]) -> bytes:
    """A 512-byte MBR with the given (type, start LBA, sectors) partitions."""
    data = bytearray(512)
    for i, (ptype, start, sectors) in enumerate(parts):
        off = 446 + 16 * i
        data[off + 4] = ptype
        data[off + 8 : off + 16] = struct.pack("<II", start, sectors)
    data[510:512] = b"\x55\xaa"
    return bytes(data)


def disk(name: str, size: int, **kw: object) -> dict[str, object]:
    return {"name": name, "path": f"/dev/{name}", "type": "disk", "size": size, **kw}


class TempConfig(unittest.TestCase):
    """Point PI_FILE at a temp dir for the duration of a test."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / ".lab" / "pi.json"
        patcher = mock.patch.object(pi, "PI_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestConfig(TempConfig):
    def test_save_merges_and_is_private(self) -> None:
        pi.save_pi(user="alice", password="x")
        pi.save_pi(host="10.0.0.5")
        self.assertEqual(
            pi.load_pi(), {"user": "alice", "password": "x", "host": "10.0.0.5"}
        )
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)

    def test_dry_does_not_write(self) -> None:
        with mock.patch.object(common, "DRY", True):
            pi.save_pi(user="alice")
        self.assertFalse(self.file.exists())

    def test_missing_or_corrupt_is_empty(self) -> None:
        self.assertEqual(pi.load_pi(), {})
        self.file.parent.mkdir(parents=True)
        self.file.write_text("{not json")
        self.assertEqual(pi.load_pi(), {})


class TestRendering(unittest.TestCase):
    def test_yaml_scalars_survive_special_characters(self) -> None:
        for value in ['p@ss "quoted" \\back', "pass phrase: 123", "Home #1", "é ü"]:
            self.assertEqual(json.loads(pi.q(value)), value)

    def test_user_data(self) -> None:
        text = pi.render_user_data("alice", "$6$salt$hash", "mypi")
        self.assertIn('hostname: "mypi"', text)
        self.assertIn("ssh_pwauth: true", text)
        self.assertIn('- name: "alice"', text)
        self.assertIn('passwd: "$6$salt$hash"', text)
        self.assertIn("lock_passwd: false", text)

    def test_network_config_wpa_and_masking(self) -> None:
        text = pi.render_network_config("Home", "secret-pass", "fr")
        self.assertIn('regulatory-domain: "fr"', text)
        self.assertIn('"Home":', text)
        self.assertIn('password: "secret-pass"', text)
        masked = pi.render_network_config("Home", "secret-pass", "FR", mask=True)
        self.assertNotIn("secret-pass", masked)

    def test_network_config_open_network(self) -> None:
        text = pi.render_network_config("Cafe", "", "DE")
        self.assertIn('"Cafe": {}', text)
        self.assertNotIn("password", text)

    def test_first_boot_files(self) -> None:
        s = {
            "user": "a",
            "hostname": "h",
            "wifi_ssid": "",
            "wifi_psk": "",
            "wifi_country": "",
        }
        files = pi.first_boot_files(s, "$6$x")
        self.assertEqual(set(files), {"user-data", "ssh"})
        self.assertEqual(files["ssh"], "")
        s = {**s, "wifi_ssid": "Net", "wifi_psk": "12345678", "wifi_country": "DE"}
        self.assertIn("network-config", pi.first_boot_files(s, "$6$x"))

    def test_hash_password_is_a_crypt_hash(self) -> None:
        if not common.have("openssl"):
            self.skipTest("openssl not installed")
        h = pi.hash_password("hunter2")
        self.assertTrue(h.startswith("$6$"))
        self.assertNotIn("hunter2", h)


class TestImage(unittest.TestCase):
    ENTRY = {
        "name": "Raspberry Pi OS Lite (64-bit)",
        "url": "https://x/y.img.xz",
        "extract_sha256": "ab" * 32,
        "extract_size": 3_000_000_000,
        "image_download_size": 500_000_000,
        "release_date": "2026-09-15",
        "init_format": "cloudinit-rpi",
    }

    def listing(self, **over: object) -> dict[str, object]:
        entry = {**self.ENTRY, **over}
        return {"os_list": [{"name": "grp", "subitems": [{"name": "other"}, entry]}]}

    def test_find_entry_in_nested_list(self) -> None:
        found = pi.find_os_entry(self.listing()["os_list"], self.ENTRY["name"])  # type: ignore[arg-type]
        self.assertEqual(found["url"], "https://x/y.img.xz")  # type: ignore[index]
        self.assertIsNone(pi.find_os_entry([], "nope"))

    def test_resolve(self) -> None:
        with mock.patch.object(pi, "_fetch_json", return_value=self.listing()):
            img = pi.resolve_os_image("deb")
        self.assertEqual((img.release, img.size), ("2026-09-15", 3_000_000_000))
        self.assertEqual(img.sha256, "ab" * 32)

    def test_legacy_format_is_rejected(self) -> None:
        with mock.patch.object(
            pi, "_fetch_json", return_value=self.listing(init_format="systemd")
        ):
            with self.assertRaisesRegex(LabError, "only 'cloudinit-rpi'"):
                pi.resolve_os_image("deb")

    def test_unknown_os(self) -> None:
        with self.assertRaisesRegex(LabError, "unknown OS"):
            pi.resolve_os_image("nope")

    def test_mbr_and_boot_offset(self) -> None:
        data = mbr((0x0C, 16384, 1048576), (0x83, 1064960, 4915200))
        self.assertEqual(
            pi.parse_mbr(data), [(0x0C, 16384, 1048576), (0x83, 1064960, 4915200)]
        )
        with tempfile.NamedTemporaryFile() as f:
            f.write(data)
            f.flush()
            self.assertEqual(pi.boot_offset(Path(f.name)), 16384 * 512)

    def test_bad_mbr(self) -> None:
        with self.assertRaises(LabError):
            pi.parse_mbr(b"\x00" * 512)
        with tempfile.NamedTemporaryFile() as f:
            f.write(mbr((0x83, 2048, 100)))
            f.flush()
            with self.assertRaisesRegex(LabError, "no FAT boot partition"):
                pi.boot_offset(Path(f.name))


class TestDevices(unittest.TestCase):
    NVME = disk(
        "nvme0n1",
        1_000_000_000_000,
        rm=False,
        hotplug=False,
        tran="nvme",
        children=[
            {"name": "nvme0n1p1", "mountpoints": ["/boot/efi"]},
            {"name": "nvme0n1p5", "mountpoints": [None, "/"]},
        ],
    )

    def test_internal_disk_is_never_a_candidate(self) -> None:
        ok, skipped = pi.classify_disks([self.NVME])
        self.assertEqual((ok, skipped), ([], []))

    def test_removable_system_disk_is_refused(self) -> None:
        usb_root = disk("sdb", 64_000_000_000, rm=True, tran="usb", mountpoints=["/"])
        ok, skipped = pi.classify_disks([usb_root])
        self.assertEqual(ok, [])
        self.assertIn("system mounts", skipped[0])

    def test_card_reader_without_card(self) -> None:
        reader = disk(
            "sda",
            0,
            rm=True,
            hotplug=True,
            tran="usb",
            vendor="Mass    ",
            model="Storage Device",
        )
        ok, skipped = pi.classify_disks([reader])
        self.assertEqual(ok, [])
        self.assertIn("no media", skipped[0])
        self.assertIn("Mass Storage Device", skipped[0])

    def test_usb_stick_and_sd_card_are_candidates(self) -> None:
        stick = disk(
            "sdb", 32_000_000_000, rm=True, tran="usb", vendor="SanDisk", model="Ultra"
        )
        sd = disk("mmcblk0", 16_000_000_000, rm=False, tran=None)
        ok, _ = pi.classify_disks([self.NVME, stick, sd])
        self.assertEqual([d.name for d in ok], ["sdb", "mmcblk0"])
        self.assertEqual(ok[0].path, "/dev/sdb")
        self.assertEqual(ok[0].label, "SanDisk Ultra")

    def test_too_small(self) -> None:
        small = disk("sdc", 2_000_000_000, rm=True, tran="usb")
        ok, skipped = pi.classify_disks([small], min_bytes=3_000_000_000)
        self.assertEqual(ok, [])
        self.assertIn("too small", skipped[0])

    def test_auto_mounted_stick_keeps_its_partitions_for_unmount(self) -> None:
        stick = disk(
            "sdb",
            32_000_000_000,
            rm=True,
            tran="usb",
            children=[
                {"name": "sdb1", "path": "/dev/sdb1", "mountpoints": ["/media/me/BOOT"]}
            ],
        )
        ok, _ = pi.classify_disks([stick])
        self.assertEqual(ok[0].mounted, [("/dev/sdb1", "/media/me/BOOT")])

    def test_dry_run_continues_without_a_device(self) -> None:
        with (
            mock.patch.object(pi, "list_devices", return_value=[]),
            mock.patch.object(common, "DRY", True),
        ):
            with mock.patch.object(pi, "warn"):
                d = pi.choose_disk(None, 1)
        self.assertEqual(d.path, "/dev/sdX")

    def test_no_device_is_an_error(self) -> None:
        with mock.patch.object(pi, "list_devices", return_value=[]):
            with self.assertRaisesRegex(LabError, "no USB stick or SD card"):
                pi.choose_disk(None, 1)

    def test_explicit_device_must_be_usable(self) -> None:
        with mock.patch.object(pi, "list_devices", return_value=[self.NVME]):
            with self.assertRaisesRegex(LabError, "not a usable removable disk"):
                pi.pick_device("/dev/nvme0n1", 1)

    def test_confirmation_needs_the_device_name(self) -> None:
        d = pi.Disk("/dev/sdb", "sdb", 1, "x", "usb")
        with mock.patch.object(pi.sys, "stdin") as stdin:
            stdin.isatty.return_value = True
            with mock.patch("builtins.input", return_value="yes"):
                with self.assertRaisesRegex(LabError, "aborted"):
                    pi.confirm_erase(d, yes=False)
            with mock.patch("builtins.input", return_value="sdb"):
                pi.confirm_erase(d, yes=False)
        pi.confirm_erase(d, yes=True)
        with mock.patch.object(pi.sys, "stdin") as stdin:
            stdin.isatty.return_value = False
            with self.assertRaisesRegex(LabError, "no terminal"):
                pi.confirm_erase(d, yes=False)


class TestWrite(unittest.TestCase):
    def test_unmounts_then_writes_with_direct_io(self) -> None:
        d = pi.Disk("/dev/sdb", "sdb", 1, "x", "usb", [("/dev/sdb1", "/media/a")])
        calls: list[list[str]] = []
        with mock.patch.object(
            pi, "run", side_effect=lambda cmd, **kw: calls.append(cmd)
        ):
            with mock.patch.object(pi, "have", return_value=True):
                pi.write_image(Path("custom.img"), d)
        self.assertEqual(calls[0], ["udisksctl", "unmount", "-b", "/dev/sdb1"])
        dd = next(c for c in calls if "dd" in c)
        self.assertEqual(dd[:2], ["sudo", "dd"])
        self.assertIn("of=/dev/sdb", dd)
        self.assertIn("oflag=direct", dd)  # progress must reflect the real device speed
        self.assertIn("conv=fsync", dd)
        self.assertLess(calls.index(dd), calls.index(["sync"]))


class TestSettings(unittest.TestCase):
    FULL = {
        "user": "alice",
        "password": "pw",
        "hostname": "pi4",
        "wifi_ssid": "Net",
        "wifi_psk": "12345678",
        "wifi_country": "DE",
    }

    def tty(self, value: bool) -> "mock._patch[mock.MagicMock]":
        patcher = mock.patch.object(pi.sys, "stdin")
        stdin = patcher.start()
        self.addCleanup(patcher.stop)
        stdin.isatty.return_value = value
        return patcher

    def test_saved_values_are_not_asked_again(self) -> None:
        self.tty(True)
        with (
            mock.patch.object(pi, "ask_text", side_effect=AssertionError("asked")),
            mock.patch.object(pi, "ask_secret", side_effect=AssertionError("asked")),
        ):
            self.assertEqual(
                pi.collect_settings(dict(self.FULL), {}, ask=False), self.FULL
            )

    def test_flags_override_saved(self) -> None:
        got = pi.collect_settings(
            dict(self.FULL), {"user": "bob", "wifi_country": "fr"}, ask=False
        )
        self.assertEqual((got["user"], got["wifi_country"]), ("bob", "FR"))

    def test_asks_once_for_what_is_missing(self) -> None:
        self.tty(True)
        texts = iter(["alice", "MyWifi", "de"])
        with (
            mock.patch.object(pi, "ask_text", side_effect=lambda *a, **k: next(texts)),
            mock.patch.object(pi, "ask_secret", side_effect=["pw", "wifipass1"]),
        ):
            got = pi.collect_settings({}, {}, ask=False)
        self.assertEqual(
            got,
            {
                "user": "alice",
                "password": "pw",
                "hostname": "raspberrypi",
                "wifi_ssid": "MyWifi",
                "wifi_psk": "wifipass1",
                "wifi_country": "DE",
            },
        )

    def test_empty_ssid_means_no_wifi_and_is_remembered(self) -> None:
        self.tty(True)
        texts = iter(["alice", ""])
        with (
            mock.patch.object(pi, "ask_text", side_effect=lambda *a, **k: next(texts)),
            mock.patch.object(pi, "ask_secret", return_value="pw"),
        ):
            got = pi.collect_settings({}, {}, ask=False)
        self.assertEqual(
            (got["wifi_ssid"], got["wifi_psk"], got["wifi_country"]), ("", "", "")
        )
        with mock.patch.object(pi, "ask_text", side_effect=AssertionError("asked")):
            self.assertEqual(
                pi.collect_settings(
                    dict(got, user="alice", password="pw"), {}, ask=False
                ),
                got,
            )

    def test_ask_flag_reprompts_everything(self) -> None:
        self.tty(True)
        # Pressing Enter at a prompt accepts the saved value shown as the default.
        enter = mock.Mock(side_effect=lambda label, default=None: default or "")

        def secret(label: str, confirm: bool = False, keep: str | None = None) -> str:
            return "newpw" if label == "Password" else (keep or "")

        with (
            mock.patch.object(pi, "ask_text", enter),
            mock.patch.object(pi, "ask_secret", side_effect=secret),
        ):
            got = pi.collect_settings(dict(self.FULL), {}, ask=True)
        self.assertGreaterEqual(enter.call_count, 3)
        self.assertEqual(got, {**self.FULL, "password": "newpw"})

    def test_no_terminal_names_the_missing_flag(self) -> None:
        self.tty(False)
        with self.assertRaisesRegex(LabError, "--user"):
            pi.collect_settings({}, {}, ask=False)

    def test_validation(self) -> None:
        for bad in ("root", "Bad Name", "1abc", ""):
            with self.assertRaises(LabError):
                pi.valid_user(bad)
        self.assertEqual(pi.valid_user("pi_4"), "pi_4")
        self.assertEqual(pi.valid_country("de"), "DE")
        with self.assertRaises(LabError):
            pi.valid_country("DEU")
        with self.assertRaises(LabError):
            pi.valid_psk("short")
        self.assertEqual(pi.valid_psk(""), "")
        self.assertEqual(pi.valid_psk("a" * 63), "a" * 63)
        with self.assertRaises(LabError):
            pi.valid_hostname("-bad")

    def test_secret_prompt_keeps_saved_value_on_enter(self) -> None:
        with mock.patch.object(pi.getpass, "getpass", return_value=""):
            self.assertEqual(pi.ask_secret("Password", confirm=True, keep="old"), "old")
        with mock.patch.object(pi.getpass, "getpass", side_effect=["new", "new"]):
            self.assertEqual(pi.ask_secret("Password", confirm=True, keep="old"), "new")

    def test_secret_prompt_requires_matching_confirmation(self) -> None:
        answers = ["a", "b"] * 3
        with (
            mock.patch.object(pi.getpass, "getpass", side_effect=answers),
            mock.patch("builtins.print"),
        ):
            with self.assertRaisesRegex(LabError, "did not match"):
                pi.ask_secret("Password", confirm=True)

    def test_guess_country(self) -> None:
        self.assertEqual(pi.guess_country("fr_FR.UTF-8"), "FR")
        self.assertIsNone(pi.guess_country("C"))
        self.assertIsNone(pi.guess_country(None))


class TestShell(TempConfig):
    def test_not_configured(self) -> None:
        with self.assertRaisesRegex(LabError, "pi setup deb"):
            pi.shell()

    def test_unresolvable_hostname_explains_how_to_find_the_pi(self) -> None:
        pi.save_pi(user="alice", password="pw", hostname="raspberrypi")
        with (
            mock.patch.object(pi, "resolve_host", return_value=None),
            mock.patch.object(pi, "lan_network", return_value="192.168.7.0/24"),
        ):
            with self.assertRaises(LabError) as ctx:
                pi.shell()
        msg = str(ctx.exception)
        self.assertIn("raspberrypi.local", msg)
        self.assertIn("sudo nmap -sn 192.168.7.0/24", msg)
        self.assertIn("./lab pi host <ip>", msg)

    def test_mdns_hit_is_saved_and_used(self) -> None:
        pi.save_pi(user="alice", password="pw", hostname="mypi")
        with (
            mock.patch.object(pi, "resolve_host", return_value="10.0.0.9"),
            mock.patch.object(pi, "run_ssh", return_value=0) as run_ssh,
        ):
            self.assertEqual(pi.shell(["uptime"]), 0)
        argv = run_ssh.call_args.args[0]
        # connects by the address it just resolved, but saves the stable name
        self.assertEqual((argv[-2], argv[-1]), ("alice@10.0.0.9", "uptime"))
        self.assertEqual(pi.load_pi()["host"], "mypi.local")

    def test_flaky_mdns_falls_back_to_the_last_known_address(self) -> None:
        pi.save_pi(user="alice", password="pw", host="mypi.local", last_ip="10.0.0.7")
        with mock.patch.object(pi, "resolve_host", return_value=None):
            self.assertEqual(pi.connection().host, "10.0.0.7")

    def test_a_successful_lookup_refreshes_the_last_known_address(self) -> None:
        pi.save_pi(user="alice", password="pw", host="mypi.local", last_ip="10.0.0.7")
        with mock.patch.object(pi, "resolve_host", return_value="10.0.0.8"):
            self.assertEqual(pi.connection().host, "10.0.0.8")
        self.assertEqual(pi.load_pi()["last_ip"], "10.0.0.8")
        self.assertEqual(pi.load_pi()["host"], "mypi.local")  # the stable name stays

    def test_saved_host_and_port_are_used_without_resolving(self) -> None:
        pi.save_pi(user="alice", password="pw", host="10.0.0.5", port=2222)
        with (
            mock.patch.object(pi, "resolve_host", side_effect=AssertionError("resolved")),
            mock.patch.object(pi, "run_ssh", return_value=3) as run_ssh,
        ):
            self.assertEqual(pi.shell(), 3)
        argv, password = run_ssh.call_args.args
        self.assertIn("alice@10.0.0.5", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertEqual(password, "pw")
        self.assertNotIn("pw", argv)

    def test_password_is_passed_through_a_temporary_askpass_helper(self) -> None:
        real_run = subprocess.run
        seen: dict[str, str] = {}

        def fake_ssh(argv: list[str], env: dict[str, str]) -> mock.Mock:
            seen["helper_output"] = real_run(
                [env["SSH_ASKPASS"]], env=env, capture_output=True, text=True
            ).stdout
            seen["require"] = env["SSH_ASKPASS_REQUIRE"]
            seen["helper"] = env["SSH_ASKPASS"]
            return mock.Mock(returncode=0)

        with mock.patch.object(pi.subprocess, "run", side_effect=fake_ssh):
            self.assertEqual(pi.run_ssh(["ssh", "x"], 'pa$$ "w0rd"'), 0)
        self.assertEqual(seen["helper_output"], 'pa$$ "w0rd"\n')
        self.assertEqual(seen["require"], "force")
        self.assertFalse(os.path.exists(seen["helper"]))

    def test_ssh_options(self) -> None:
        argv = pi.ssh_command("u", "h", 22)
        for opt in ("StrictHostKeyChecking=accept-new", "PubkeyAuthentication=no"):
            self.assertIn(opt, argv)
        self.assertEqual(argv[-1], "u@h")

    def test_lan_detection(self) -> None:
        self.assertEqual(
            pi.parse_default_dev(
                "default via 192.168.1.254 dev wlp0s20f3 proto dhcp metric 600"
            ),
            "wlp0s20f3",
        )
        self.assertEqual(
            pi.parse_inet(
                "3: wlp0s20f3    inet 192.168.1.99/24 brd 192.168.1.255 scope global"
            ),
            "192.168.1.0/24",
        )
        self.assertIsNone(pi.parse_default_dev(""))
        self.assertIsNone(pi.parse_inet(""))
        self.assertIn("(adjust to your network)", pi.not_found_message("x.local", None))


class TestHostAndShow(TempConfig):
    def test_host_roundtrip(self) -> None:
        with mock.patch.object(pi.socket, "create_connection"):
            pi.host("192.168.1.50", 2200)
            self.assertEqual(
                (pi.load_pi()["host"], pi.load_pi()["port"]), ("192.168.1.50", 2200)
            )
            pi.host("pi.example.org", None)
        self.assertEqual(
            (pi.load_pi()["host"], pi.load_pi()["port"]), ("pi.example.org", 2200)
        )

    def test_host_validation(self) -> None:
        for bad in ("bad host!", "-x", ""):
            with self.assertRaises(LabError):
                pi.host(bad, None)
        with self.assertRaises(LabError):
            pi.host("10.0.0.1", 70000)

    def test_show_masks_secrets(self) -> None:
        pi.save_pi(
            user="alice", password="topsecret", wifi_ssid="Net", wifi_psk="wifisecret"
        )
        with mock.patch("builtins.print") as out:
            pi.show_config()
        text = "".join(str(c.args[0]) for c in out.call_args_list)
        self.assertNotIn("topsecret", text)
        self.assertNotIn("wifisecret", text)
        self.assertIn("alice", text)
        with mock.patch("builtins.print") as out:
            pi.show_config(secrets=True)
        self.assertIn("topsecret", "".join(str(c.args[0]) for c in out.call_args_list))


class TestCleanSparesPiConfig(unittest.TestCase):
    def test_pi_json_survives_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".lab").mkdir()
            (root / ".lab" / "pi.json").write_text("{}")
            (root / ".lab" / "state.json").write_text("{}")
            (root / "runs").mkdir()
            with mock.patch.object(clean, "ROOT", root):
                got = clean._spare_preserved([".lab/", "runs/"])
        self.assertEqual(sorted(got), [".lab/state.json", "runs/"])


if __name__ == "__main__":
    unittest.main()
