"""`lab pi`: put Raspberry Pi OS on a USB/SD stick, then ssh into the Pi.

`lab pi setup deb` looks up the current Raspberry Pi OS Lite image in the Raspberry Pi
Imager OS list, downloads and checksum-verifies it, writes a first-boot configuration
into the boot partition of a copy and flashes that copy to a removable disk:

  user-data       cloud-init: your user (password hash), hostname, ssh password login
  network-config  cloud-init/netplan: Wi-Fi (SSID, password, regulatory domain)
  ssh             empty marker; the image's sshswitch service then enables sshd

The boot partition is patched inside the copy with mtools (no root); only the final
`dd` needs sudo. Settings are saved in .lab/pi.json (mode 0600) so later runs don't
ask again, and `lab pi shell` logs in with them. Only current images that use
cloud-init ("cloudinit-rpi") are supported, not the legacy ones.
"""

from __future__ import annotations

import getpass
import hashlib
import io
import ipaddress
import json
import lzma
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import common, piarch, pios
from .common import (
    IMAGES,
    PI_FILE,
    ROOT,
    LabError,
    check_disk,
    have,
    human,
    info,
    load_state,
    output,
    run,
    show,
    table,
    warn,
)
from .kernel import CONFIG_CACHE, build_arch, build_dir, build_meta
from .pios import OSES, PiOs

OS_LIST_URL = "https://downloads.raspberrypi.com/os_list_imagingutility_v4.json"
USER_AGENT = "klab (https://github.com/amdrozdov/klab)"
# `lab pi setup <os>`: the operating systems it can install.
OS_CHOICES = {name: o.title for name, o in OSES.items()}
# The ones that come as a ready-made image in the Raspberry Pi Imager OS list.
IMAGER_ENTRIES = {"deb": "Raspberry Pi OS Lite (64-bit)"}
CLOUD_INIT_FORMAT = "cloudinit-rpi"

PI_IMAGES = IMAGES / "pi"
KNOWN_HOSTS = ROOT / ".lab" / "pi_known_hosts"
DEFAULT_USER = "pi"
DEFAULT_HOSTNAME = "raspberrypi"
DEFAULT_PORT = 22
# Raspberry Pi OS images put the FAT boot partition at sector 16384.
DEFAULT_BOOT_OFFSET = 16384 * 512
# Groups of the image's own default user (/etc/cloud/cloud.cfg).
USER_GROUPS = (
    "adm, dialout, cdrom, audio, users, sudo, video, games, plugdev, input, gpio, spi, "
    "i2c, netdev, render, lpadmin"
)

USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
# Mount points that mean "this disk is part of the running system": never a target.
SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/home", "/usr", "/var", "/etc", "/opt"}
LSBLK_COLUMNS = "NAME,PATH,TYPE,RM,HOTPLUG,TRAN,SIZE,VENDOR,MODEL,MOUNTPOINTS"


# --------------------------------------------------------------------------- config


def load_pi() -> dict[str, Any]:
    try:
        return json.loads(PI_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_pi(**updates: Any) -> None:
    """Merge updates into .lab/pi.json (written atomically, mode 0600: it holds
    credentials)."""
    if common.DRY:
        return
    cfg = load_pi()
    cfg.update(updates)
    PI_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PI_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(PI_FILE)


# --------------------------------------------------------------------------- prompts


def ask_text(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{label}{suffix}: ").strip()
    return answer or (default or "")


def ask_secret(label: str, confirm: bool = False, keep: str | None = None) -> str:
    """Hidden prompt. `keep` is the saved value: Enter alone keeps it."""
    hint = " (Enter keeps the saved one)" if keep else ""
    for _ in range(3):
        first = getpass.getpass(f"{label}{hint}: ")
        if not first and keep:
            return keep
        if not confirm or getpass.getpass(f"{label} (again): ") == first:
            return first
        print("The two entries differ, try again.")
    raise LabError("passwords did not match")


def valid_user(name: str) -> str:
    if name == "root" or not USER_RE.match(name):
        raise LabError(
            f"invalid user name '{name}' (lowercase letters, digits, '_' and '-'; "
            "not root)"
        )
    return name


def valid_hostname(name: str) -> str:
    if not HOSTNAME_RE.match(name):
        raise LabError(f"invalid hostname '{name}' (letters, digits and '-')")
    return name


def valid_country(code: str) -> str:
    if not COUNTRY_RE.match(code):
        raise LabError(f"invalid Wi-Fi country '{code}' (2 letters, e.g. DE)")
    return code.upper()


def valid_psk(psk: str) -> str:
    """A WPA passphrase is 8-63 characters (or 64 hex digits); empty = open network."""
    if psk and not (8 <= len(psk) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", psk)):
        raise LabError("the Wi-Fi password must be 8-63 characters (or empty if open)")
    return psk


def guess_country(lang: str | None) -> str | None:
    """'fr_FR.UTF-8' -> 'FR'."""
    m = re.match(r"^[a-z]{2,3}_([A-Z]{2})", lang or "")
    return m.group(1) if m else None


def _field(
    key: str,
    flag: str | None,
    cfg: dict[str, Any],
    ask: bool,
    prompt: Callable[[str | None], str],
    check: Callable[[str], str],
    default: str | None = None,
    silent: bool = False,
) -> str:
    """Value for one setting: flag > saved config > prompt (> default / placeholder).

    silent: never prompt, fall back to the default (settings the user rarely changes)."""
    if flag is not None:
        return check(flag)
    saved = cfg.get(key)
    if saved is not None and not ask:
        return str(saved)
    if common.DRY:
        return str(saved if saved is not None else (default or f"<{key}>"))
    if silent:
        return check(str(saved) if saved is not None else (default or ""))
    if not sys.stdin.isatty():
        raise LabError(
            f"missing '{key}': pass --{key.replace('_', '-')} (no terminal to ask on)"
        )
    shown = str(saved) if saved is not None else default
    return check(prompt(shown))


def collect_settings(
    cfg: dict[str, Any], flags: dict[str, str | None], ask: bool
) -> dict[str, str]:
    """Settings for the Pi: from flags, else saved config, else asked interactively.

    Anything already saved is not asked again (unless ask=True)."""
    s: dict[str, str] = {}
    s["user"] = _field(
        "user",
        flags.get("user"),
        cfg,
        ask,
        lambda d: ask_text("Username", d),
        valid_user,
        default=DEFAULT_USER,
    )
    s["password"] = _field(
        "password",
        flags.get("password"),
        cfg,
        ask,
        lambda d: ask_secret("Password", confirm=True, keep=d),
        lambda v: v if v else _fail("the password must not be empty"),
    )
    s["hostname"] = _field(
        "hostname",
        flags.get("hostname"),
        cfg,
        ask,
        lambda d: ask_text("Hostname", d),
        valid_hostname,
        default=DEFAULT_HOSTNAME,
        silent=True,
    )
    s["wifi_ssid"] = _field(
        "wifi_ssid",
        flags.get("wifi_ssid"),
        cfg,
        ask,
        lambda d: ask_text("Wi-Fi network name (SSID; empty = no Wi-Fi)", d),
        lambda v: v,
        default="",
    )
    if not s["wifi_ssid"]:
        return {**s, "wifi_psk": "", "wifi_country": ""}
    s["wifi_psk"] = _field(
        "wifi_psk",
        flags.get("wifi_psk"),
        cfg,
        ask,
        lambda d: ask_secret("Wi-Fi password (empty = open network)", keep=d),
        valid_psk,
    )
    s["wifi_country"] = _field(
        "wifi_country",
        flags.get("wifi_country"),
        cfg,
        ask,
        lambda d: ask_text("Wi-Fi country code (2 letters, e.g. DE)", d),
        valid_country,
        default=guess_country(os.environ.get("LANG")),
    )
    return s


def _fail(msg: str) -> str:
    raise LabError(msg)


# --------------------------------------------------------------------------- OS image


@dataclass
class OsImage:
    name: str
    url: str
    sha256: str  # of the extracted .img
    size: int  # extracted size in bytes
    download_size: int
    release: str


def find_os_entry(items: Sequence[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """Depth-first search of the Imager OS list for the entry called `name`."""
    for item in items:
        if "subitems" in item:
            hit = find_os_entry(item["subitems"], name)
            if hit:
                return hit
        elif item.get("name") == name:
            return item
    return None


def _fetch_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except (OSError, ValueError) as e:
        raise LabError(f"could not read the Raspberry Pi OS list ({url}): {e}") from e


def resolve_os_image(os_name: str) -> OsImage:
    wanted = IMAGER_ENTRIES.get(os_name)
    if wanted is None:
        raise LabError(
            f"unknown OS '{os_name}' (images are listed for: {', '.join(IMAGER_ENTRIES)})"
        )
    data = _fetch_json(OS_LIST_URL)
    entry = find_os_entry(data.get("os_list", []), wanted)
    if entry is None:
        raise LabError(f"'{wanted}' is not in the Raspberry Pi Imager OS list")
    fmt = entry.get("init_format")
    if fmt != CLOUD_INIT_FORMAT:
        raise LabError(
            f"'{wanted}' uses the '{fmt}' first-boot format; only "
            f"'{CLOUD_INIT_FORMAT}' images are supported"
        )
    return OsImage(
        name=wanted,
        url=entry["url"],
        sha256=entry["extract_sha256"],
        size=int(entry["extract_size"]),
        download_size=int(entry.get("image_download_size", 0)),
        release=str(entry.get("release_date", "")),
    )


def fetch_image(img: OsImage) -> Path:
    """Download + decompress the image into images/pi/ (verified, cached)."""
    name = Path(urllib.parse.urlparse(img.url).path).name.removesuffix(".xz")
    dest = PI_IMAGES / name
    marker = dest.with_name(dest.name + ".sha256")
    cached = (
        dest.exists()
        and dest.stat().st_size == img.size
        and marker.exists()
        and marker.read_text().strip() == img.sha256
    )
    if cached:
        info(f"using cached {dest.name}")
        return dest
    check_disk(img.size * 2 / 1e9 + 1, "the Raspberry Pi OS image")
    info(f"downloading {img.name} {img.release} ({human(img.download_size)})")
    if common.DRY:
        print(f"curl -fsSL {shlex.quote(img.url)} | xz -d > {shlex.quote(str(dest))}")
        print(f"echo '{img.sha256}  {dest}' | sha256sum -c -")
        return dest

    PI_IMAGES.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    dec = lzma.LZMADecompressor()
    digest = hashlib.sha256()
    done = last_pct = 0
    req = urllib.request.Request(img.url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as out:
            while chunk := r.read(1 << 20):
                done += len(chunk)
                data = dec.decompress(chunk)
                digest.update(data)
                out.write(data)
                pct = done * 100 // img.download_size if img.download_size else 0
                if pct != last_pct and sys.stderr.isatty():
                    last_pct = pct
                    print(
                        f"\r  {pct:3d}%  {human(done)} / {human(img.download_size)}",
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )
        if sys.stderr.isatty():
            print(file=sys.stderr)
    except (OSError, lzma.LZMAError, EOFError) as e:
        part.unlink(missing_ok=True)
        raise LabError(f"download failed: {e}") from e
    if not dec.eof or digest.hexdigest() != img.sha256:
        part.unlink(missing_ok=True)
        raise LabError("the downloaded image is corrupt (checksum mismatch); try again")
    part.replace(dest)
    marker.write_text(img.sha256 + "\n")
    info(f"image verified: {dest.name}")
    return dest


# ----------------------------------------------------------------------- first-boot files


def q(value: str) -> str:
    """A YAML double-quoted scalar (JSON strings are valid YAML)."""
    return json.dumps(value, ensure_ascii=False)


def hash_password(password: str) -> str:
    """SHA-512 crypt hash for cloud-init's `passwd:` (the plain password never goes
    on the image). Piped through stdin so it doesn't show up in `ps`."""
    if common.DRY:
        return "$6$dry$<password-hash>"
    try:
        r = subprocess.run(
            ["openssl", "passwd", "-6", "-stdin"],
            input=password + "\n",
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        raise LabError("openssl is required (sudo apt install openssl)") from e
    out = r.stdout.strip()
    if r.returncode != 0 or not out.startswith("$6$"):
        raise LabError(f"openssl could not hash the password: {r.stderr.strip()}")
    return out


def render_user_data(user: str, pw_hash: str, hostname: str) -> str:
    return "\n".join(
        [
            "#cloud-config",
            "# Written by `lab pi setup`; applied once by cloud-init on first boot.",
            f"hostname: {q(hostname)}",
            "manage_etc_hosts: true",
            "ssh_pwauth: true",
            "users:",
            f"  - name: {q(user)}",
            f"    groups: [{USER_GROUPS}]",
            "    shell: /bin/bash",
            "    lock_passwd: false",
            f"    passwd: {q(pw_hash)}",
            '    sudo: ["ALL=(ALL) NOPASSWD:ALL"]',
            "",
        ]
    )


def render_network_config(ssid: str, psk: str, country: str, mask: bool = False) -> str:
    access = "{}" if not psk else ""
    lines = [
        "# Written by `lab pi setup`; applied once by cloud-init on first boot.",
        "network:",
        "  version: 2",
        "  ethernets:",
        "    eth0:",
        "      dhcp4: true",
        "      optional: true",
        "  wifis:",
        "    wlan0:",
        "      dhcp4: true",
        "      optional: true",
        f"      regulatory-domain: {q(country)}",
        "      access-points:",
        f"        {q(ssid)}: {access}".rstrip(),
    ]
    if psk:
        lines.append(f"          password: {q('********' if mask else psk)}")
    return "\n".join([*lines, ""])


def first_boot_files(
    s: dict[str, str], pw_hash: str, mask: bool = False
) -> dict[str, str]:
    """name -> content of the files to put on the boot partition."""
    files = {"user-data": render_user_data(s["user"], pw_hash, s["hostname"])}
    if s["wifi_ssid"]:
        files["network-config"] = render_network_config(
            s["wifi_ssid"], s["wifi_psk"], s["wifi_country"], mask=mask
        )
    files["ssh"] = ""  # marker: the image's sshswitch service enables sshd
    return files


def parse_mbr(mbr: bytes) -> list[tuple[int, int, int]]:
    """MBR partition table -> [(type, start LBA, sectors)] for the used entries."""
    if len(mbr) < 512 or mbr[510:512] != b"\x55\xaa":
        raise LabError("the image has no MBR partition table")
    parts = []
    for i in range(4):
        entry = mbr[446 + 16 * i : 462 + 16 * i]
        start, sectors = struct.unpack("<II", entry[8:16])
        if entry[4] and sectors:
            parts.append((entry[4], start, sectors))
    return parts


def boot_offset(image: Path) -> int:
    """Byte offset of the FAT boot partition inside the image."""
    with open(image, "rb") as f:
        parts = parse_mbr(f.read(512))
    for ptype, start, _ in parts:
        if ptype in (0x0B, 0x0C, 0x0E):  # FAT32 / FAT16 LBA
            return start * 512
    raise LabError("no FAT boot partition found in the image")


def customize_image(base: Path, files: dict[str, str], shown: dict[str, str]) -> Path:
    """Copy the base image and put the first-boot files on its boot partition (with
    mtools: no root, no mounting). `shown` is the secret-free text printed by --dry."""
    custom = PI_IMAGES / "custom.img"
    offset = boot_offset(base) if base.exists() else DEFAULT_BOOT_OFFSET
    target = f"{custom}@@{offset}"
    info("preparing a copy of the image with the first-boot files")
    if not common.DRY:
        PI_IMAGES.mkdir(parents=True, exist_ok=True)
    run(["cp", "--reflink=auto", "--sparse=auto", base, custom])
    if common.DRY:
        for name, text in shown.items():
            if text:
                print(f"cat > /tmp/{name} <<'EOF'\n{text.rstrip()}\nEOF")
            else:
                print(f": > /tmp/{name}")
            show(["mcopy", "-o", "-i", target, f"/tmp/{name}", f"::{name}"])
        return custom
    with tempfile.TemporaryDirectory() as d:  # 0700: holds the password hash
        for name, text in files.items():
            src = Path(d) / name
            src.write_text(text)
            run(["mcopy", "-o", "-i", target, src, f"::{name}"])
    return custom


# --------------------------------------------------------------------------- devices


@dataclass
class Disk:
    path: str
    name: str
    size: int
    label: str
    tran: str
    mounted: list[tuple[str, str]] = field(default_factory=list)  # (partition, mount)


def _mounted(dev: dict[str, Any]) -> list[tuple[str, str]]:
    out = [
        (str(dev.get("path") or f"/dev/{dev.get('name')}"), m)
        for m in dev.get("mountpoints") or []
        if m
    ]
    for child in dev.get("children") or []:
        out += _mounted(child)
    return out


def classify_disks(
    devices: Sequence[dict[str, Any]], min_bytes: int = 0
) -> tuple[list[Disk], list[str]]:
    """Pick the disks `lab pi setup` may write to: removable / USB / SD, with media,
    big enough, and not holding a system mount. Returns (candidates, skipped notes)."""
    ok: list[Disk] = []
    skipped: list[str] = []
    for d in devices:
        if d.get("type") != "disk":
            continue
        name = str(d["name"])
        removable = (
            bool(d.get("rm") or d.get("hotplug"))
            or d.get("tran") == "usb"
            or name.startswith("mmcblk")
        )
        if not removable:
            continue
        label = " ".join(f"{d.get('vendor') or ''} {d.get('model') or ''}".split())
        label = label or "unknown device"
        size = int(d.get("size") or 0)
        mounted = _mounted(d)
        system = sorted(SYSTEM_MOUNTS & {m for _, m in mounted})
        if system:
            skipped.append(f"{name}: holds system mounts ({', '.join(system)})")
        elif size == 0:
            skipped.append(
                f"{name} ({label}): no media (0 B), e.g. a card reader with no card"
            )
        elif size < min_bytes:
            skipped.append(
                f"{name} ({label}, {human(size)}): too small, the image needs "
                f"{human(min_bytes)}"
            )
        else:
            ok.append(
                Disk(
                    path=str(d.get("path") or f"/dev/{name}"),
                    name=name,
                    size=size,
                    label=label,
                    tran=str(d.get("tran") or "-"),
                    mounted=mounted,
                )
            )
    return ok, skipped


def list_devices() -> list[dict[str, Any]]:
    text = output(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS, "-e", "7"])
    try:
        devices: list[dict[str, Any]] = json.loads(text)["blockdevices"]
    except (ValueError, KeyError) as e:
        raise LabError("could not list disks with lsblk") from e
    return devices


def pick_device(wanted: str | None, min_bytes: int) -> Disk:
    ok, skipped = classify_disks(list_devices(), min_bytes)
    if wanted:
        for d in ok:
            if d.path == wanted:
                return d
        why = [s for s in skipped if s.startswith(Path(wanted).name)]
        raise LabError(
            f"{wanted} is not a usable removable disk"
            + ("".join(f"\n  {s}" for s in why) or " (see `lsblk`)")
        )
    if len(ok) == 1:
        return ok[0]
    if not ok:
        raise LabError(
            "no USB stick or SD card found"
            + "".join(f"\n  skipped {s}" for s in skipped)
            + "\nplug one in (a card reader shows 0 B until a card is inserted), or "
            "pass --device /dev/sdX"
        )
    if not sys.stdin.isatty():
        raise LabError("several removable disks found; pass --device /dev/sdX")
    print(
        table(
            [[i + 1, d.path, human(d.size), d.tran, d.label] for i, d in enumerate(ok)],
            ["#", "device", "size", "bus", "model"],
        )
    )
    answer = ask_text("Which one (number)")
    if not answer.isdigit() or not 1 <= int(answer) <= len(ok):
        raise LabError("no disk chosen")
    return ok[int(answer) - 1]


def choose_disk(wanted: str | None, min_bytes: int) -> Disk:
    """pick_device, but --dry only warns and uses a placeholder device."""
    try:
        return pick_device(wanted, min_bytes)
    except LabError as e:
        if not common.DRY:
            raise
        warn(f"{e}\n(--dry: continuing with a placeholder device)")
        return Disk(wanted or "/dev/sdX", Path(wanted or "sdX").name, 0, "?", "usb")


def confirm_erase(disk: Disk, yes: bool) -> None:
    if common.DRY or yes:
        return
    if not sys.stdin.isatty():
        raise LabError("refusing to write without confirmation (no terminal); pass --yes")
    print(f"\nAbout to ERASE {disk.path}: {disk.label}, {human(disk.size)}")
    if input(f"Type '{disk.name}' to continue: ").strip() != disk.name:
        raise LabError("aborted, nothing written")


def write_image(image: Path, disk: Disk) -> None:
    for part, _mount in disk.mounted:  # the desktop usually auto-mounts the stick
        if have("udisksctl"):
            run(["udisksctl", "unmount", "-b", part])
        else:
            run(["sudo", "umount", part])
    info(f"writing {image.name} to {disk.path} (sudo; takes a few minutes)")
    # oflag=direct bypasses the page cache, so the progress shows the stick's real speed
    # (without it dd "finishes" 3 GB in 2 s and then sits silently in the final fsync).
    run(
        [
            "sudo",
            "dd",
            f"if={image}",
            f"of={disk.path}",
            "bs=4M",
            "oflag=direct",
            "conv=fsync",
            "status=progress",
        ]
    )
    run(["sync"])
    if have("udisksctl"):
        run(["udisksctl", "power-off", "-b", disk.path], check=False)


# --------------------------------------------------------------------------- setup


REQUIRED_TOOLS: dict[str, tuple[tuple[str, str], ...]] = {
    "deb": (("mcopy", "mtools"), ("openssl", "openssl"), ("lsblk", "util-linux")),
    "arch": (
        ("mcopy", "mtools"),
        ("openssl", "openssl"),
        ("lsblk", "util-linux"),
        ("fakeroot", "fakeroot"),
        ("mke2fs", "e2fsprogs"),
        ("mkfs.vfat", "dosfstools"),
        ("sfdisk", "fdisk"),
    ),
}


def _prepare_deb(img: OsImage, s: dict[str, str]) -> Path:
    """Raspberry Pi OS: a ready image, plus cloud-init files on its boot partition."""
    base = fetch_image(img)
    pw_hash = hash_password(s["password"])
    return customize_image(
        base, first_boot_files(s, pw_hash), first_boot_files(s, pw_hash, mask=True)
    )


def _prepare_arch(s: dict[str, str]) -> Path:
    """Arch Linux ARM: build the disk image from the root filesystem tarball."""
    tarball = piarch.fetch_tarball(PI_IMAGES)
    sudo_pkg, deps = piarch.fetch_package(PI_IMAGES, "sudo")
    have_key = bool(s["wifi_ssid"] and s["wifi_psk"])
    spec = piarch.Spec(
        user=s["user"],
        pw_hash=hash_password(s["password"]),
        hostname=s["hostname"],
        label_id=piarch.new_label_id(),
        wifi_ssid=s["wifi_ssid"],
        wifi_psk_hex=piarch.wpa_psk_hex(s["wifi_ssid"], s["wifi_psk"])
        if have_key
        else "",
        wifi_country=s["wifi_country"],
        sudo_package=str(sudo_pkg),
        sudo_deps=tuple(deps),
    )
    return piarch.build_image(tarball, spec, PI_IMAGES / "custom.img")


def setup(
    os_name: str,
    flags: dict[str, str | None],
    device: str | None = None,
    ask: bool = False,
    yes: bool = False,
    no_write: bool = False,
    keep_image: bool = False,
) -> None:
    """Write an operating system (user, ssh and Wi-Fi preconfigured) to a stick."""
    if os_name not in OSES:
        raise LabError(f"unknown OS '{os_name}' (available: {', '.join(OSES)})")
    for tool, pkg in REQUIRED_TOOLS[os_name]:
        if not have(tool):
            common.dry_or_raise(f"{tool} is required (sudo apt install {pkg})")
    title = OSES[os_name].title
    img: OsImage | None = None
    if os_name == "deb":
        info("looking up the current Raspberry Pi OS image")
        img = resolve_os_image(os_name)
        need = img.size
        source: dict[str, Any] = {
            "url": img.url,
            "sha256": img.sha256,
            "release": img.release,
        }
    else:
        need = piarch.MIN_DEVICE_BYTES
        source = {"url": piarch.ARCH_URL, "sha256": "", "release": "latest"}
    disk = None if no_write else choose_disk(device, need)
    if disk:
        info(f"target: {disk.path}  {disk.label}  {human(disk.size)}")

    cfg = load_pi()
    s = collect_settings(cfg, flags, ask)
    save_pi(os=os_name, **source, **s)

    custom = _prepare_deb(img, s) if img else _prepare_arch(s)
    if disk and not common.DRY and disk.size < custom.stat().st_size:
        raise LabError(
            f"{disk.path} ({human(disk.size)}) is smaller than the image "
            f"({human(custom.stat().st_size)})"
        )
    if disk:
        confirm_erase(disk, yes)
        write_image(custom, disk)
        if not common.DRY:
            KNOWN_HOSTS.unlink(missing_ok=True)  # a reflash gets new ssh host keys
            # a fresh card runs its own stock kernel
            save_pi(installed_at=time.strftime("%Y-%m-%dT%H:%M:%S"), kernel=None)
    if not (keep_image or no_write) and not common.DRY:
        custom.unlink(missing_ok=True)  # holds the password hash and the Wi-Fi key

    if disk:
        info(f"done: {title} is on {disk.path}")
        packages = ", ".join(piarch.FIRST_BOOT_PACKAGES)
        if os_name == "deb":
            wait = (
                "wait 2-3 minutes: the first boot resizes the filesystem and applies "
                "your settings"
            )
        else:
            wait = (
                "wait about 5 minutes: the first boot installs sudo and grows the root\n"
                "     partition; once the network is up it upgrades the system and\n"
                f"     installs {packages} (log: /var/log/klab-firstboot.log)"
            )
        print(
            "\nNext:\n"
            "  1. put the stick / SD card in the Pi and power it on\n"
            f"  2. {wait}\n"
            "  3. run: ./lab pi shell"
        )
    else:
        info(f"image prepared: {custom} (not written; flash it with dd or Imager)")


# --------------------------------------------------------------------------- ssh


def parse_default_dev(route: str) -> str | None:
    """`ip -4 -o route show default` -> the interface name."""
    m = re.search(r"\bdev (\S+)", route)
    return m.group(1) if m else None


def parse_inet(addr: str) -> str | None:
    """`ip -4 -o addr show dev X` -> the interface's network, e.g. 192.168.1.0/24."""
    m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+/\d+)", addr)
    return str(ipaddress.ip_interface(m.group(1)).network) if m else None


def lan_network() -> str | None:
    dev = parse_default_dev(output(["ip", "-4", "-o", "route", "show", "default"]))
    if not dev:
        return None
    return parse_inet(output(["ip", "-4", "-o", "addr", "show", "dev", dev]))


def not_found_message(name: str, network: str | None) -> str:
    net = network or "192.168.1.0/24"
    guess = "" if network else "  (adjust to your network)"
    return (
        f"cannot find '{name}' on the network. The Pi may be off, still booting "
        "(the first boot takes 2-3 minutes) or on another network.\n\n"
        f"Look for it on your network ({net}):\n"
        f"    sudo nmap -sn {net}{guess}\n"
        "  (sudo apt install nmap; the Pi shows up as 'Raspberry Pi Trading Ltd'.\n"
        "   Without nmap: `ip neigh`, or your router's list of connected devices.)\n\n"
        "Then save its address:\n"
        "    ./lab pi host <ip>"
    )


def resolve_host(name: str, tries: int = 3, delay: float = 0.7) -> str | None:
    """Resolve a name (mDNS lookups of <host>.local fail now and then, so retry)."""
    for attempt in range(tries):
        try:
            return socket.gethostbyname(name)
        except OSError:
            if attempt + 1 < tries:
                time.sleep(delay)
    return None


def ssh_command(user: str, host: str, port: int, remote: Sequence[str] = ()) -> list[str]:
    return [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",  # fmt: skip
        "-o",
        f"UserKnownHostsFile={KNOWN_HOSTS}",  # fmt: skip
        "-o",
        "PreferredAuthentications=password,keyboard-interactive",  # fmt: skip
        "-o",
        "PubkeyAuthentication=no",  # fmt: skip
        "-p",
        str(port),  # fmt: skip
        f"{user}@{host}",
        *remote,
    ]


def sweep_stale_askpass(base: Path | None = None) -> None:
    """Remove askpass-<pid>-* directories whose process is gone (an interrupted ssh)."""
    base = base or ROOT / ".lab"
    for d in base.glob("askpass-*"):
        parts = d.name.split("-")
        if (
            len(parts) > 2
            and parts[1].isdigit()
            and not Path(f"/proc/{parts[1]}").exists()
        ):
            shutil.rmtree(d, ignore_errors=True)


@contextmanager
def askpass_env(password: str) -> Iterator[dict[str, str]]:
    """Environment that makes ssh take `password` from a throwaway helper script:
    SSH_ASKPASS_REQUIRE=force (OpenSSH >= 8.4) needs no terminal and no sshpass."""
    ROOT.joinpath(".lab").mkdir(exist_ok=True)
    sweep_stale_askpass()
    with tempfile.TemporaryDirectory(
        prefix=f"askpass-{os.getpid()}-", dir=ROOT / ".lab"
    ) as d:
        helper = Path(d) / "askpass"
        helper.write_text("#!/bin/sh\nprintf '%s\\n' \"$KLAB_PI_PASSWORD\"\n")
        helper.chmod(0o700)
        yield {
            **os.environ,
            "SSH_ASKPASS": str(helper),
            "SSH_ASKPASS_REQUIRE": "force",
            "KLAB_PI_PASSWORD": password,
        }


def run_ssh(argv: list[str], password: str) -> int:
    """Run an interactive ssh with the saved password."""
    with askpass_env(password) as env:
        return subprocess.run(argv, env=env).returncode


@dataclass
class Conn:
    """Where and how to reach the saved Pi."""

    user: str
    password: str
    host: str
    port: int

    def argv(self, remote: Sequence[str] = ()) -> list[str]:
        return ssh_command(self.user, self.host, self.port, remote)


def connection() -> Conn:
    """The saved Pi. Without a saved host it tries <hostname>.local, else explains how
    to find the Pi on the network."""
    cfg = load_pi()
    if not cfg.get("user") or not cfg.get("password"):
        raise LabError("no Raspberry Pi configured yet; run `./lab pi setup deb`")
    port = int(cfg.get("port") or DEFAULT_PORT)
    host = cfg.get("host")
    if not host:
        name = f"{cfg.get('hostname') or DEFAULT_HOSTNAME}.local"
        if common.DRY:
            return Conn(cfg["user"], cfg["password"], name, port)
        address = resolve_host(name)
        if not address:
            raise LabError(not_found_message(name, lan_network()))
        info(f"found {name}")
        save_pi(host=name, last_ip=address)  # keep the name: the address can change
        return Conn(cfg["user"], cfg["password"], address, port)
    if host.endswith(".local") and not common.DRY:
        address = resolve_host(host)
        if address:
            if cfg.get("last_ip") != address:
                save_pi(last_ip=address)
            host = address
        elif cfg.get("last_ip"):
            # mDNS answers come and go (the Pi's Wi-Fi power saving drops multicast):
            # use the address the name had last time instead of failing.
            host = str(cfg["last_ip"])
    return Conn(cfg["user"], cfg["password"], host, port)


def shell(remote: Sequence[str] = ()) -> int:
    """ssh into the saved Pi (user, password, host and port come from the config)."""
    conn = connection()
    argv = conn.argv(remote)
    if common.DRY:
        show(argv)
        return 0
    info(f"connecting to {conn.user}@{conn.host}:{conn.port}")
    rc = run_ssh(argv, conn.password)
    if rc == 255:
        warn(
            "ssh could not connect: the Pi may still be booting, or the saved host is "
            "wrong (check it with `./lab pi host`)"
        )
    return rc


def remote(
    conn: Conn, command: str, stdin: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a shell command on the Pi (optionally feeding a file to its stdin) and
    capture the output. A failure raises LabError with what the Pi said."""
    argv = conn.argv([command])
    with askpass_env(conn.password) as env:
        if stdin is None:
            r = subprocess.run(argv, env=env, capture_output=True, text=True)
        else:
            with open(stdin, "rb") as f:
                r = subprocess.run(argv, env=env, stdin=f, capture_output=True, text=True)
    if r.returncode != 0:
        why = r.stderr.strip() or f"exit {r.returncode}"
        raise LabError(f"on the Pi ({conn.host}): {why}")
    return r


def valid_host(value: str) -> str:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not HOST_RE.match(value):
            raise LabError(f"'{value}' is not an IP address or hostname") from None
    return value


def host(addr: str | None, port: int | None) -> None:
    """Show or save the Pi's address (and ssh port) for `lab pi shell`."""
    cfg = load_pi()
    if addr is None and port is None:
        shown_port = cfg.get("port") or DEFAULT_PORT
        print(f"host: {cfg.get('host') or '(not set)'}   port: {shown_port}")
        return
    updates: dict[str, Any] = {}
    if addr is not None:
        updates["host"] = valid_host(addr)
    if port is not None:
        if not 1 <= port <= 65535:
            raise LabError(f"invalid port {port}")
        updates["port"] = port
    save_pi(**updates)
    target = updates.get("host") or cfg.get("host")
    use_port = int(updates.get("port") or cfg.get("port") or DEFAULT_PORT)
    info(f"saved: {target or '(no host yet)'}:{use_port}")
    if target and not common.DRY:
        try:
            socket.create_connection((target, use_port), timeout=3).close()
        except OSError:
            warn(f"{target} does not answer on port {use_port} yet (saved anyway)")


def show_config(secrets: bool = False) -> None:
    cfg = load_pi()
    if not cfg:
        print("(no Raspberry Pi configured; `./lab pi setup deb`)")
        return
    hidden = {"password", "wifi_psk"}
    keys = [
        "os", "release", "url", "user", "password", "hostname", "host", "port",
        "wifi_ssid", "wifi_psk", "wifi_country", "installed_at", "kernel",
    ]  # fmt: skip
    k = cfg.get("kernel")
    if isinstance(k, dict):
        cfg["kernel"] = f"{k.get('build')} ({k.get('release')})"
    rows = [
        [k, "********" if k in hidden and cfg[k] and not secrets else cfg[k]]
        for k in keys
        if cfg.get(k) is not None
    ]
    print(table(rows, ["setting", "value"]))
    where = PI_FILE.relative_to(ROOT) if PI_FILE.is_relative_to(ROOT) else PI_FILE
    print(f"\nstored in {where} (mode 0600)")


# ------------------------------------------------------------------- custom kernels
#
# Per-OS paths and snippets live in pios.py; see there for how a kernel is installed.

PI4_DTB = "bcm2711-rpi-4-b.dtb"

# Raspberry Pi OS: the stock config is a file in /boot. Pick the Pi 4 flavour (-rpi-v8),
# else the newest /boot/config-* that is not one of ours. It must not follow `uname -r`:
# once a klab kernel runs, that would hand back our own previous build as the next base.
DEB_STOCK_CONFIG = "; ".join(
    [
        "f=$(ls -v /boot/config-*-rpi-v8 2>/dev/null | tail -1)",
        'if [ -z "$f" ]; then '
        "f=$(ls -v /boot/config-* 2>/dev/null | grep -v -- -klab- | tail -1); fi",
        '[ -n "$f" ] || exit 3',
        'basename "$f" | sed s/^config-//',
        'cat "$f"',
    ]
)
# Arch Linux ARM has no config file at all, but the stock kernel embeds its own config
# (CONFIG_IKCONFIG=y): read it out of the stock image in /boot, which our install never
# touches (the firmware loads ours from klab/ instead).
ARCH_STOCK_IMAGE = "cat /boot/Image.gz 2>/dev/null || cat /boot/Image"

DETECT_OS = '. /etc/os-release 2>/dev/null; echo "${ID:-unknown} ${ID_LIKE:-}"'
_OS_CACHE: dict[str, PiOs] = {}


def detect_os(conn: Conn) -> PiOs:
    """The OS running on the Pi, from its /etc/os-release (--dry trusts the saved one)."""
    saved = str(load_pi().get("os") or pios.DEFAULT_OS)
    if common.DRY:
        return OSES.get(saved, OSES[pios.DEFAULT_OS])
    if conn.host not in _OS_CACHE:
        found = pios.from_os_release(remote(conn, DETECT_OS).stdout)
        if found.name != saved:
            info(f"the Pi runs {found.title} (the saved setting said '{saved}')")
            save_pi(os=found.name)
        _OS_CACHE[conn.host] = found
    return _OS_CACHE[conn.host]


def cached_kernel_config() -> Path | None:
    """The most recently fetched Pi config of the saved OS, if any."""
    pattern = "arch-*.config" if load_pi().get("os") == "arch" else "*pi-*.config"
    found = sorted(CONFIG_CACHE.glob(pattern), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def remote_raw(conn: Conn, command: str) -> bytes:
    """Like remote(), but for binary output (a kernel image)."""
    with askpass_env(conn.password) as env:
        r = subprocess.run(conn.argv([command]), env=env, capture_output=True)
    if r.returncode != 0:
        why = r.stderr.decode(errors="replace").strip() or f"exit {r.returncode}"
        raise LabError(f"on the Pi ({conn.host}): {why}")
    return r.stdout


def fetch_kernel_config() -> Path:
    """The stock kernel config of the saved Pi, cached in configs/.cache/. It is the
    base of `lab build --arch pi`; if the Pi cannot be reached the cached copy is used."""
    try:
        conn = connection()
        os_ = detect_os(conn)
        info(f"reading the stock kernel config of {conn.user}@{conn.host} ({os_.name})")
        if common.DRY:
            show(conn.argv([DEB_STOCK_CONFIG if os_ is pios.DEB else ARCH_STOCK_IMAGE]))
            return CONFIG_CACHE / "pi-<release>.config"
        if os_ is pios.ARCH:
            config = piarch.extract_ikconfig(remote_raw(conn, ARCH_STOCK_IMAGE))
            release = "arch-" + piarch.config_version(config)
        else:
            release, _, config = remote(conn, DEB_STOCK_CONFIG).stdout.partition("\n")
            if "CONFIG_" not in config:
                raise LabError(
                    "the Pi did not return a kernel config (/boot/config-* missing?)"
                )
            release = release.strip()
    except LabError as e:
        cached = cached_kernel_config()
        if cached is None:
            raise
        warn(f"{e}\nusing the cached Pi config {cached.name} instead")
        return cached
    CONFIG_CACHE.mkdir(parents=True, exist_ok=True)
    path = CONFIG_CACHE / (release if release.startswith("arch-") else f"pi-{release}")
    path = path.with_name(path.name + ".config")
    path.write_text(config)
    return path


def _as_root(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    return ti


def make_bundle(build: str, os_: PiOs = pios.DEB, country: str | None = None) -> Path:
    """builds/<build>/pi-bundle.tar.gz: everything `lab pi kernel install` puts on the
    Pi, laid out relative to / (kernel + DTBs into klab/, modules, install.sh)."""
    meta = build_meta(build)
    if build_arch(build) != "pi":
        raise LabError(
            f"build '{build}' is not a Raspberry Pi build (lab build --arch pi)"
        )
    out = build_dir(build)
    release = meta.get("kernelrelease")
    image = out / str(meta.get("image", ""))
    modules = out / "modroot" / "lib" / "modules" / str(release)
    dtbs = {p.name: p for p in sorted((out / "dtbroot").rglob("bcm2711-rpi-*.dtb"))}
    if not release or not image.is_file() or not modules.is_dir():
        raise LabError(f"build '{build}' is not built yet; run `./lab build --arch pi`")
    if PI4_DTB not in dtbs:
        raise LabError(f"build '{build}' has no {PI4_DTB}; is CONFIG_ARCH_BCM2835 set?")

    bundle = out / "pi-bundle.tar.gz"
    klab = os_.bundle_prefix
    skip = {f"lib/modules/{release}/build", f"lib/modules/{release}/source"}

    def keep(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        return None if ti.name in skip else _as_root(ti)  # symlinks to the host tree

    def add_bytes(tf: tarfile.TarFile, name: str, data: bytes) -> None:
        ti = tarfile.TarInfo(name)
        ti.size, ti.mode = len(data), 0o644
        tf.addfile(_as_root(ti), io.BytesIO(data))

    script = os_.install_script(str(release), country)
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(image, f"{klab}/kernel8.img", filter=_as_root)
        for name, path in dtbs.items():
            tf.add(path, f"{klab}/{name}", filter=_as_root)
        # Overlays are only loaded from klab/ if this file exists; stock overlays are
        # written for the stock kernel's device tree, not for ours.
        add_bytes(tf, f"{klab}/overlays/README", b"")
        add_bytes(tf, f"{klab}/BUILD", f"build={build}\nrelease={release}\n".encode())
        add_bytes(tf, f"{klab}/install.sh", script.encode())
        tf.add(modules, f"lib/modules/{release}", filter=keep)
    return bundle


def parse_kv(text: str) -> dict[str, str]:
    """'key=value' lines -> dict (other lines ignored)."""
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def reboot_pi(conn: Conn) -> None:
    with askpass_env(conn.password) as env:  # the connection drops as it goes down
        subprocess.run(conn.argv(["sudo systemctl reboot"]), env=env, capture_output=True)


def kernel_install(build: str | None, reboot: bool = False) -> None:
    """Copy a `--arch pi` build to the saved Pi and make it the kernel it boots."""
    build = build or load_state().get("pi_build")
    if not build:
        raise LabError("no Pi build given (and none built yet): `lab build --arch pi`")
    conn = connection()
    os_ = detect_os(conn)
    release = str(build_meta(build).get("kernelrelease") or "<release>")
    if common.DRY:
        info(
            f"bundle: builds/{build}/pi-bundle.tar.gz (kernel, DTBs, modules, install.sh)"
        )
        show([*conn.argv([os_.unpack]), "<", f"builds/{build}/pi-bundle.tar.gz"])
        return

    bundle = make_bundle(build, os_, load_pi().get("wifi_country") or None)
    size = human(bundle.stat().st_size)
    info(f"installing {build} ({release}) on {conn.user}@{conn.host} ({size})")
    with tarfile.open(bundle) as tf:
        members = tf.getmembers()
    need_boot = sum(m.size for m in members if m.name.startswith(os_.bundle_prefix))
    need_root = sum(m.size for m in members if m.name.startswith("lib/modules/"))
    for path, need, what in (
        (os_.fw, need_boot, "the kernel"),
        ("/lib/modules", need_root, "the modules"),
    ):
        free = int(remote(conn, f"df --output=avail -B1 {path} | tail -1").stdout)
        if free < need * 1.2:
            raise LabError(
                f"{path} on the Pi has {human(free)} free, {what} needs {human(need)}"
            )
    remote(conn, os_.unpack, stdin=bundle)
    save_pi(kernel={"build": build, "release": release, "installed_at": _now()})
    if reboot:
        info("rebooting the Pi")
        reboot_pi(conn)
        print("\nWhen it is back (about a minute): ./lab pi kernel status")
    else:
        print(
            "\nInstalled. Reboot the Pi to run it:  ./lab pi kernel install --reboot "
            f"{build}\nor:  ./lab pi shell -- sudo reboot\n"
            f"Then check:  ./lab pi kernel status   (uname -r should be {release})"
        )


def kernel_stock(reboot: bool = False) -> None:
    """Go back to the stock kernel: remove the klab block from config.txt."""
    conn = connection()
    os_ = detect_os(conn)
    if common.DRY:
        show(conn.argv([os_.stock]))
        return
    remote(conn, os_.stock)
    save_pi(kernel=None)
    info(f"{os_.config_txt} no longer selects the klab kernel (files stay in {os_.klab})")
    if reboot:
        reboot_pi(conn)
        print("rebooting; check with ./lab pi kernel status in a minute")
    else:
        print("Reboot to use the stock kernel:  ./lab pi shell -- sudo reboot")


def kernel_status() -> None:
    conn = connection()
    os_ = detect_os(conn)
    if common.DRY:
        show(conn.argv([os_.status_script]))
        return
    s = parse_kv(remote(conn, os_.status_script).stdout)
    running, installed = s.get("running", "?"), s.get("release")
    rows = [
        ["running kernel", running],
        ["built", s.get("built", "?")],
        ["board", s.get("model", "?")],
        ["operating system", s.get("os", os_.title)],
        [
            "config.txt boots",
            "klab kernel" if s.get("selected") == "klab" else "stock kernel",
        ],
        ["installed build", f"{s['build']} ({installed})" if "build" in s else "(none)"],
    ]
    print(table(rows, ["", ""]))
    if s.get("selected") == "klab" and running == installed:
        print(f"\nYou are running your build '{s['build']}'.")
    elif s.get("selected") == "klab":
        print(
            "\nThe klab kernel is selected but the Pi booted a different one: reboot it, "
            "or the firmware could not use klab/ (kernel8.img or the DTB is missing)."
        )
    else:
        print("\nThe stock kernel is selected; `lab pi kernel install <build>` switches.")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
