"""Arch Linux ARM for the Raspberry Pi 4: stock kernel config and the disk image.

Arch Linux ARM publishes a root filesystem tarball, not a disk image, so `lab pi setup
arch` builds the image itself, without root:

  1. partition a sparse file (FAT boot partition + ext4 root) with sfdisk,
  2. unpack the tarball with GNU tar under `fakeroot` (so every file is owned by root),
  3. customise the unpacked tree (user, hostname, ssh, Wi-Fi, first-boot service),
  4. fill the FAT partition with mcopy and the ext4 partition with `mke2fs -d`.

Only the final `dd` onto the card needs sudo. The image is small (about 3 GB); a
first-boot service grows the root partition to fill the card.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from . import common
from .common import LabError, check_disk, human, info

USER_AGENT = "klab (https://github.com/amdrozdov/klab)"
TARBALL = "ArchLinuxARM-rpi-aarch64-latest.tar.gz"
ARCH_URL = f"http://os.archlinuxarm.org/os/{TARBALL}"
MIRROR = "http://mirror.archlinuxarm.org/aarch64"
IMAGE_USER = "alarm"  # the default account of the tarball; `lab pi setup` renames it
FIRST_BOOT_PACKAGES = ("htop", "vim", "stress-ng")
MIN_DEVICE_BYTES = 4_000_000_000  # the image is about 3 GB

MIB = 1 << 20
SECTOR = 512
BOOT_START = 2048  # sectors (1 MiB)
BOOT_MIB = 512  # /boot is 223 MB, and a kernel upgrade writes a second copy


# ------------------------------------------------------------ stock kernel config


def extract_ikconfig(image: bytes) -> str:
    """The .config a kernel embeds (CONFIG_IKCONFIG=y), read from a kernel image file:
    what scripts/extract-ikconfig does, for a raw Image or a gzip'd Image.gz."""
    if image[:2] == b"\x1f\x8b":
        try:
            image = gzip.decompress(image)
        except (OSError, EOFError, zlib.error) as e:
            raise LabError(f"the kernel image is not valid gzip data: {e}") from e
    magic = b"IKCFG_ST"
    view = memoryview(image)
    pos = image.find(magic)
    while pos != -1:
        try:  # the gzip stream follows the marker; trailing data is ignored
            text = zlib.decompressobj(wbits=31).decompress(view[pos + len(magic) :])
            config = text.decode()
        except (zlib.error, UnicodeDecodeError):
            config = ""
        if "CONFIG_" in config:
            return config
        pos = image.find(magic, pos + 1)
    raise LabError(
        "the kernel image has no embedded config (the kernel needs CONFIG_IKCONFIG=y)"
    )


def config_version(config: str) -> str:
    """'7.1.6' from the '# Linux/arm64 7.1.6 Kernel Configuration' header."""
    m = re.search(r"^# Linux/\S+ (\S+) Kernel Configuration", config, re.M)
    return m.group(1) if m else "unknown"


# ------------------------------------------------------------------------ downloads


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def fetch_bytes(url: str) -> bytes:
    try:
        with urllib.request.urlopen(_request(url), timeout=60) as r:
            data: bytes = r.read()
            return data
    except OSError as e:
        raise LabError(f"could not download {url}: {e}") from e


def download(url: str, dest: Path, what: str) -> None:
    """Download url to dest (via dest.part), with a progress line on a terminal."""
    part = dest.with_name(dest.name + ".part")
    done = last = 0
    try:
        with (
            urllib.request.urlopen(_request(url), timeout=60) as r,
            open(part, "wb") as f,
        ):
            total = int(r.headers.get("Content-Length") or 0)
            while chunk := r.read(1 << 20):
                f.write(chunk)
                done += len(chunk)
                pct = done * 100 // total if total else 0
                if pct != last and sys.stderr.isatty():
                    last = pct
                    print(
                        f"\r  {what}: {pct:3d}%  {human(done)} / {human(total)}",
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )
        if sys.stderr.isatty():
            print(file=sys.stderr)
    except OSError as e:
        part.unlink(missing_ok=True)
        raise LabError(f"download of {url} failed: {e}") from e
    part.replace(dest)


def file_hash(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def fetch_tarball(images: Path) -> Path:
    """The Arch Linux ARM Pi tarball in `images`, verified against its published MD5
    (re-downloaded when the 'latest' tarball changed)."""
    dest = images / TARBALL
    want = fetch_bytes(f"{ARCH_URL}.md5").decode().split()[0]
    if dest.is_file() and file_hash(dest, "md5") == want:
        info(f"using cached {dest.name}")
        return dest
    check_disk(8, "the Arch Linux ARM image")
    info(f"downloading {TARBALL} (about 850 MB)")
    if common.DRY:
        print(f"curl -fsSL -o {shlex.quote(str(dest))} {ARCH_URL}")
        print(f"echo '{want}  {dest}' | md5sum -c -")
        return dest
    images.mkdir(parents=True, exist_ok=True)
    download(ARCH_URL, dest, "tarball")
    if file_hash(dest, "md5") != want:
        dest.unlink(missing_ok=True)
        raise LabError("the downloaded tarball is corrupt (MD5 mismatch); try again")
    info("tarball verified (MD5)")
    return dest


def parse_desc(text: str) -> dict[str, list[str]]:
    """A pacman database `desc` file: '%KEY%' headings followed by value lines."""
    out: dict[str, list[str]] = {}
    key = ""
    for line in text.splitlines():
        if line.startswith("%") and line.endswith("%") and len(line) > 2:
            key = line.strip("%")
            out[key] = []
        elif line and key:
            out[key].append(line)
    return out


def find_package(db: bytes, name: str) -> dict[str, list[str]]:
    """The `desc` entry of package `name` in a repository database (core.db)."""
    with tarfile.open(fileobj=io.BytesIO(db), mode="r:*") as tf:
        for m in tf.getmembers():
            pkg = m.name.split("/")[0].rsplit("-", 2)[0]
            if m.isfile() and m.name.endswith("/desc") and pkg == name:
                f = tf.extractfile(m)
                assert f is not None
                return parse_desc(f.read().decode())
    raise LabError(f"package '{name}' is not in the Arch Linux ARM repository")


def fetch_package(images: Path, name: str, repo: str = "core") -> tuple[Path, list[str]]:
    """Download one package from the mirror, verified against the SHA-256 in the
    repository database. Returns (file, its dependencies)."""
    pkgs = images / "pkgs"
    desc = find_package(fetch_bytes(f"{MIRROR}/{repo}/{repo}.db"), name)
    filename = desc["FILENAME"][0]
    url = f"{MIRROR}/{repo}/{filename}"
    dest = pkgs / filename
    deps = desc.get("DEPENDS", [])
    if dest.is_file() and file_hash(dest, "sha256") == desc["SHA256SUM"][0]:
        return dest, deps
    info(f"downloading {filename}")
    if common.DRY:
        print(f"curl -fsSL -o {shlex.quote(str(dest))} {url}")
        return dest, deps
    pkgs.mkdir(parents=True, exist_ok=True)
    download(url, dest, name)
    if file_hash(dest, "sha256") != desc["SHA256SUM"][0]:
        dest.unlink(missing_ok=True)
        raise LabError(f"{filename} is corrupt (SHA-256 mismatch); try again")
    return dest, deps


def installed_packages(root: Path) -> set[str]:
    """Package names and everything they provide, from the pacman database in `root`."""
    names: set[str] = set()
    local = root / "var" / "lib" / "pacman" / "local"
    for d in local.iterdir() if local.is_dir() else []:
        names.add(d.name.rsplit("-", 2)[0])
        desc = d / "desc"
        if desc.is_file():
            provided = parse_desc(desc.read_text()).get("PROVIDES", [])
            names.update(p.split("=")[0] for p in provided)
    return names


def missing_dependencies(deps: list[str], root: Path) -> list[str]:
    have = installed_packages(root)
    return [d for d in deps if re.split(r"[<>=]", d)[0] not in have]


# ------------------------------------------------------- the customised root tree


@dataclass
class Spec:
    """Everything the first boot of the Arch image is configured with."""

    user: str
    pw_hash: str
    hostname: str
    label_id: str  # the MBR disk id (8 hex digits): PARTUUID is <id>-01 / <id>-02
    wifi_ssid: str = ""
    wifi_psk_hex: str = ""  # 64 hex digits; empty with an SSID = open network
    wifi_country: str = ""
    sudo_package: str = ""  # path of the sudo package file on the host
    sudo_deps: tuple[str, ...] = ()  # what it needs; the image must already have them
    packages: tuple[str, ...] = FIRST_BOOT_PACKAGES


def new_label_id() -> str:
    return secrets.token_hex(4)


def wpa_psk_hex(ssid: str, passphrase: str) -> str:
    """The WPA2 pre-shared key (what wpa_passphrase prints), so the passphrase itself is
    not stored on the card. A 64-digit hex string is already a key."""
    if re.fullmatch(r"[0-9a-fA-F]{64}", passphrase):
        return passphrase.lower()
    return hashlib.pbkdf2_hmac("sha1", passphrase.encode(), ssid.encode(), 4096, 32).hex()


def _fields(text: str, key: str, replace: Callable[[list[str]], list[str]]) -> str:
    """Apply replace(fields) to the ':'-separated line whose first field is `key`."""
    out = []
    for line in text.splitlines():
        f = line.split(":")
        out.append(":".join(replace(f)) if f[0] == key else line)
    return "\n".join(out) + "\n"


def rename_user(
    passwd: str, shadow: str, group: str, gshadow: str, new: str, pw_hash: str, today: int
) -> tuple[str, str, str, str]:
    """Rename the default account to `new` (home, group, wheel membership) and give it
    the password hash. The files are returned as new text."""
    old = IMAGE_USER

    def passwd_line(f: list[str]) -> list[str]:
        return [new, *f[1:5], f"/home/{new}", *f[6:]]

    def shadow_line(f: list[str]) -> list[str]:
        return [new, pw_hash, str(today), *f[3:]]

    def rename_member(text: str) -> str:
        out = []
        for line in text.splitlines():
            f = line.split(":")
            if f[0] == old:
                f[0] = new
            for i in range(2, len(f)):  # members (and admins, in gshadow)
                f[i] = ",".join(new if m == old else m for m in f[i].split(",") if m)
            out.append(":".join(f))
        return "\n".join(out) + "\n"

    return (
        _fields(passwd, old, passwd_line),
        _fields(shadow, old, shadow_line),
        rename_member(group),
        rename_member(gshadow),
    )


def lock_root(shadow: str) -> str:
    """No password login for root (sudo is the way in)."""
    return _fields(shadow, "root", lambda f: [f[0], "!*", *f[2:]])


def hosts_text(hostname: str) -> str:
    return (
        "127.0.0.1\tlocalhost\n::1\t\tlocalhost\n"
        f"127.0.1.1\t{hostname}.localdomain\t{hostname}\n"
    )


def fstab_text(label_id: str) -> str:
    """Mount the boot partition by PARTUUID: the stock fstab says /dev/mmcblk0p1, which
    is wrong when the card sits in a USB reader."""
    return (
        "# Static information about the filesystems.\n"
        "# <file system>  <dir>  <type>  <options>  <dump>  <pass>\n"
        f"PARTUUID={label_id}-01  /boot  vfat  defaults  0  0\n"
    )


def wpa_conf(ssid: str, psk_hex: str, country: str) -> str:
    """wpa_supplicant-wlan0.conf. The SSID is written as hex so no character needs
    escaping."""
    secret = f"psk={psk_hex}" if psk_hex else "key_mgmt=NONE"
    return (
        "ctrl_interface=/run/wpa_supplicant\n"
        "update_config=1\n"
        f"country={country.upper()}\n\n"
        f"network={{\n    ssid={ssid.encode().hex()}\n    {secret}\n}}\n"
    )


LINK_WLAN = "[Match]\nType=wlan\n\n[Link]\nName=wlan0\n"
NETWORK_WLAN = "[Match]\nName=wlan0\n\n[Network]\nDHCP=yes\nMulticastDNS=yes\n"
RESOLVED_MDNS = "[Resolve]\nMulticastDNS=yes\n"
SSHD_DROPIN = "PasswordAuthentication yes\nPermitRootLogin no\n"


def sudoers_text(user: str) -> str:
    return f"{user} ALL=(ALL:ALL) NOPASSWD: ALL\n"


def add_mdns(network: str) -> str:
    """Make a .network file answer mDNS, so <hostname>.local resolves (idempotent)."""
    if "MulticastDNS" in network:
        return network
    return re.sub(r"^(DHCP=.*)$", r"\1\nMulticastDNS=yes", network, count=1, flags=re.M)


FIRSTBOOT_SCRIPT = """#!/bin/sh
# klab first boot, part 1 (needs no network): sudo, a root partition that fills the
# card, and the pacman keyring. Runs once; the log is /var/log/klab-firstboot.log.
exec >>/var/log/klab-firstboot.log 2>&1
set -u
echo "== klab first boot, $(date)"

# sudo is not part of the image: install the package shipped with it (offline; its
# dependencies are already there).
pacman -U --noconfirm --needed /var/cache/klab/*.pkg.tar.* || echo "sudo: FAILED"
chmod 0440 /etc/sudoers.d/10-klab

grow_root() {
    dev=$(findmnt -no SOURCE /) || return 1
    disk="/dev/$(lsblk -no PKNAME "$dev")"
    num=$(cat "/sys/class/block/$(basename "$dev")/partition") || return 1
    echo ', +' | sfdisk --no-reread -N "$num" "$disk" && partx -u "$disk" &&
        resize2fs "$dev"
}
grow_root || echo "growing the root partition: FAILED"

pacman-key --init && pacman-key --populate archlinuxarm || echo "keyring: FAILED"
touch /var/lib/klab-firstboot.done
echo "== klab first boot done"
"""

FIRSTBOOT_SERVICE = """[Unit]
Description=klab first boot: sudo, grow the root partition, pacman keyring
ConditionPathExists=!/var/lib/klab-firstboot.done
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/klab-firstboot
TimeoutStartSec=900

[Install]
WantedBy=multi-user.target
"""


def packages_script(packages: tuple[str, ...]) -> str:
    return (
        "#!/bin/sh\n"
        "# klab first boot, part 2 (needs the network): a full upgrade, as Arch wants\n"
        "# it, plus the packages klab uses. Retried at every boot until it works.\n"
        "exec >>/var/log/klab-firstboot.log 2>&1\n"
        'echo "== klab packages, $(date)"\n'
        f"pacman -Syu --noconfirm --needed {' '.join(packages)} || exit 1\n"
        "touch /var/lib/klab-packages.done\n"
        'echo "== klab packages done"\n'
    )


PACKAGES_SERVICE = """[Unit]
Description=klab first boot: install packages (needs the network)
Wants=network-online.target
After=network-online.target klab-firstboot.service
ConditionPathExists=!/var/lib/klab-packages.done
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/klab-packages
Restart=on-failure
RestartSec=60
TimeoutStartSec=3600

[Install]
WantedBy=multi-user.target
"""


def _write(root: Path, rel: str, text: str, mode: int = 0o644) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def _enable(root: Path, unit_file: str, wants: str, name: str) -> None:
    link = root / "etc" / "systemd" / "system" / f"{wants}.wants" / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(unit_file)


def customize(root: Path, spec: Spec, today: int | None = None) -> None:
    """Apply `spec` to an unpacked Arch Linux ARM tree. It runs under fakeroot, so the
    files it creates come out owned by root; accounts and permissions are set here."""

    def read(rel: str) -> str:
        path = root / rel
        return path.read_text() if path.is_file() else ""

    days = today if today is not None else int(time.time() // 86400)
    passwd, shadow, group, gshadow = rename_user(
        read("etc/passwd"),
        read("etc/shadow"),
        read("etc/group"),
        read("etc/gshadow"),
        spec.user,
        spec.pw_hash,
        days,
    )
    _write(root, "etc/passwd", passwd)
    _write(root, "etc/shadow", lock_root(shadow), 0o600)
    _write(root, "etc/group", group)
    if gshadow:
        _write(root, "etc/gshadow", gshadow, 0o600)
    home, old_home = root / "home" / spec.user, root / "home" / IMAGE_USER
    if spec.user != IMAGE_USER and old_home.exists() and not home.exists():
        old_home.rename(home)

    _write(root, "etc/hostname", spec.hostname + "\n")
    _write(root, "etc/hosts", hosts_text(spec.hostname))
    _write(root, "etc/fstab", fstab_text(spec.label_id))

    # ssh is enabled in the image already; make password login explicit, root login off
    _write(root, "etc/ssh/sshd_config.d/10-klab.conf", SSHD_DROPIN)
    # sudo itself is installed at first boot; its configuration can be put in place now
    (root / "etc/sudoers.d").mkdir(parents=True, exist_ok=True)
    (root / "etc/sudoers.d").chmod(0o750)
    _write(root, "etc/sudoers.d/10-klab", sudoers_text(spec.user), 0o440)

    # networking: mDNS on the wired links, Wi-Fi through wpa_supplicant + networkd
    for name in ("eth.network", "en.network"):
        net = root / "etc/systemd/network" / name
        if net.is_file():
            net.write_text(add_mdns(net.read_text()))
    _write(root, "etc/systemd/resolved.conf.d/10-klab-mdns.conf", RESOLVED_MDNS)
    if spec.wifi_ssid:
        _write(root, "etc/systemd/network/10-wlan0.link", LINK_WLAN)
        _write(root, "etc/systemd/network/wlan.network", NETWORK_WLAN)
        _write(
            root,
            "etc/wpa_supplicant/wpa_supplicant-wlan0.conf",
            wpa_conf(spec.wifi_ssid, spec.wifi_psk_hex, spec.wifi_country),
            0o600,
        )
        _enable(
            root,
            "/usr/lib/systemd/system/wpa_supplicant@.service",
            "multi-user.target",
            "wpa_supplicant@wlan0.service",
        )

    # first boot: sudo, root partition, keyring, then the packages
    if spec.sudo_package:
        missing = missing_dependencies(list(spec.sudo_deps), root)
        if missing:
            raise LabError(f"sudo needs packages the image lacks: {', '.join(missing)}")
        cache = root / "var" / "cache" / "klab"
        cache.mkdir(parents=True, exist_ok=True)
        shutil.copy2(spec.sudo_package, cache / Path(spec.sudo_package).name)
    _write(root, "usr/local/sbin/klab-firstboot", FIRSTBOOT_SCRIPT, 0o755)
    _write(root, "usr/local/sbin/klab-packages", packages_script(spec.packages), 0o755)
    _write(root, "etc/systemd/system/klab-firstboot.service", FIRSTBOOT_SERVICE)
    _write(root, "etc/systemd/system/klab-packages.service", PACKAGES_SERVICE)
    for unit in ("klab-firstboot", "klab-packages"):
        _enable(
            root,
            f"/etc/systemd/system/{unit}.service",
            "multi-user.target",
            f"{unit}.service",
        )


# --------------------------------------------------------------- the disk image


@dataclass(frozen=True)
class Layout:
    boot_start: int
    boot_sectors: int
    root_start: int
    root_sectors: int

    @property
    def total_bytes(self) -> int:
        return (self.root_start + self.root_sectors) * SECTOR


def fat_cluster_sectors(boot_bytes: int) -> int:
    """Sectors per cluster that keep a FAT32 filesystem of this size above the 65525
    clusters FAT32 needs (mkfs.vfat sizes them from the whole file, not the partition)."""
    spc = 1
    while spc < 128 and boot_bytes // (SECTOR * spc * 2) >= 80000:
        spc *= 2
    return spc


def plan_layout(rootfs_bytes: int) -> Layout:
    """A 512 MiB FAT boot partition and a root partition with room to spare. The first
    boot grows the root partition to the card, so the image only has to hold the files."""
    boot_sectors = BOOT_MIB * MIB // SECTOR
    root_bytes = int(rootfs_bytes * 1.15) + 384 * MIB
    root_sectors = -(-root_bytes // SECTOR)
    root_sectors += -root_sectors % BOOT_START  # align to 1 MiB
    return Layout(BOOT_START, boot_sectors, BOOT_START + boot_sectors, root_sectors)


def sfdisk_script(layout: Layout, label_id: str) -> str:
    return (
        f"label: dos\nlabel-id: 0x{label_id}\nunit: sectors\n\n"
        f"start={layout.boot_start}, size={layout.boot_sectors}, type=c\n"
        f"start={layout.root_start}, size={layout.root_sectors}, type=83\n"
    )


def assembly_script(
    tarball: Path,
    stage: Path,
    image: Path,
    spec_file: Path,
    sfdisk_file: Path,
    layout: Layout,
) -> str:
    """The commands that turn the tarball into the image (run under fakeroot)."""
    q = shlex.quote
    boot_kib = layout.boot_sectors * SECTOR // 1024
    root_kib = layout.root_sectors * SECTOR // 1024
    boot_off = layout.boot_start * SECTOR
    root_off = layout.root_start * SECTOR
    return "\n".join(
        [
            "set -e",
            f"mkdir -p {q(str(stage))}",
            f"tar -xpf {q(str(tarball))} -C {q(str(stage))} --numeric-owner "
            "--warning=no-unknown-keyword",
            f"{q(sys.executable)} -m labtool.piarch customize {q(str(stage))} "
            f"{q(str(spec_file))}",
            f"rm -f {q(str(image))}",
            f"truncate -s {layout.total_bytes} {q(str(image))}",
            f"sfdisk --quiet --no-reread --no-tell-kernel {q(str(image))} "
            f"< {q(str(sfdisk_file))}",
            f"mkfs.vfat -F 32 -n BOOT -s {fat_cluster_sectors(boot_kib * 1024)} "
            f"--offset {layout.boot_start} {q(str(image))} {boot_kib} >/dev/null",
            f"mcopy -s -m -n -i {q(str(image))}@@{boot_off} {q(str(stage))}/boot/* ::",
            f"rm -rf {q(str(stage))}/boot/*",  # the boot partition now holds /boot
            f"mke2fs -q -t ext4 -L root -E root_owner=0:0,offset={root_off} "
            f"-d {q(str(stage))} {q(str(image))} {root_kib}k",
            "",
        ]
    )


def _rmtree(path: Path) -> None:
    subprocess.run(["chmod", "-R", "u+rwX", str(path)], capture_output=True)
    shutil.rmtree(path, ignore_errors=True)


def build_image(
    tarball: Path, spec: Spec, image: Path, rootfs_bytes: int | None = None
) -> Path:
    """Assemble the bootable image from the tarball (no root needed)."""
    work = image.parent
    stage = work / f"arch-stage-{os.getpid()}"
    if common.DRY:
        layout = plan_layout(rootfs_bytes or 2300 * MIB)
        info(f"building {image.name} ({human(layout.total_bytes)}) from the tarball")
        print(
            assembly_script(
                tarball, stage, image, work / "spec.json", work / "spec.sfdisk", layout
            )
        )
        return image

    check_disk(8, "the Arch Linux ARM image")
    work.mkdir(parents=True, exist_ok=True)
    for tool in ("fakeroot", "tar", "sfdisk", "mkfs.vfat", "mcopy", "mke2fs", "truncate"):
        if not shutil.which(tool):
            raise LabError(f"{tool} is required to build the image (see `lab doctor`)")
    if rootfs_bytes is None:
        with tarfile.open(tarball) as tf:
            rootfs_bytes = sum(m.size for m in tf if m.isfile())
    layout = plan_layout(rootfs_bytes)
    fd, name = tempfile.mkstemp(dir=work, prefix="spec-", suffix=".json")
    spec_file, sfdisk_file = Path(name), Path(name + ".sfdisk")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(spec), f)  # holds the password hash and the Wi-Fi key: 0600
        sfdisk_file.write_text(sfdisk_script(layout, spec.label_id))
        info(f"building {image.name} ({human(layout.total_bytes)}); a few minutes")
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
        script = assembly_script(tarball, stage, image, spec_file, sfdisk_file, layout)
        r = subprocess.run(
            ["fakeroot", "sh", "-c", script], env=env, capture_output=True, text=True
        )
        if r.returncode != 0:
            raise LabError(
                f"building the image failed:\n{(r.stderr or r.stdout)[-1500:]}"
            )
    finally:
        spec_file.unlink(missing_ok=True)
        sfdisk_file.unlink(missing_ok=True)
        if stage.exists():
            _rmtree(stage)
    return image


def main(argv: list[str]) -> int:
    """`python -m labtool.piarch customize <root> <spec.json>` (run under fakeroot)."""
    if len(argv) == 3 and argv[0] == "customize":
        data = json.loads(Path(argv[2]).read_text())
        data["packages"] = tuple(data["packages"])
        data["sudo_deps"] = tuple(data["sudo_deps"])
        customize(Path(argv[1]), Spec(**data))
        return 0
    print("usage: python -m labtool.piarch customize <root> <spec.json>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
