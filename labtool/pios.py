"""What differs between the operating systems `lab pi` can put on a Raspberry Pi.

Raspberry Pi OS ("deb") and Arch Linux ARM ("arch") mount the boot partition in different
places, boot differently (Arch starts U-Boot first and has no cmdline.txt) and use
different package managers. Everything OS-specific that `lab pi kernel` and
`lab pi stress` need is plain data and shell snippets here, so it can be tested without a
Pi.

A custom kernel is installed NEXT TO the stock one, never over it. The firmware's
`os_prefix=klab/` makes it load kernel8.img, the .dtb files and cmdline.txt from the
`klab/` directory of the boot partition instead of its root; if the kernel or DTB is
missing there the firmware ignores the prefix and boots the stock kernel (on Arch that
means U-Boot, as before). The modules go to /lib/modules/<release>, where <release>
carries the build name (CONFIG_LOCALVERSION=-klab-<build>).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

KLAB_BEGIN = "# klab begin (managed by lab pi kernel)"
KLAB_END = "# klab end"
# Appended to config.txt: `[all]` makes it apply to every model; auto_initramfs=0 stops
# the firmware from pairing our kernel with a stock initramfs (our boot drivers are
# built in, see configs/pi.config).
KLAB_BLOCK = "\n".join(
    [KLAB_BEGIN, "[all]", "os_prefix=klab/", "auto_initramfs=0", KLAB_END]
)

# The kernel command line written for Arch, which has no cmdline.txt of its own (U-Boot
# passes it). `$root` is filled in on the Pi from the root filesystem's PARTUUID.
ARCH_CMDLINE = (
    "console=serial0,115200 console=tty1 root=$root rootfstype=ext4 rw rootwait "
    "fsck.repair=yes"
)

PACMAN_STRESS = "sudo pacman -Syu --noconfirm --needed stress-ng"
APT_STRESS = (
    "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y stress-ng || "
    "{ sudo apt-get update && "
    "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y stress-ng; }"
)


@dataclass(frozen=True)
class PiOs:
    name: str  # "deb" | "arch": the `lab pi setup <name>` argument
    title: str
    fw: str  # where the Pi mounts its boot partition
    pkg_install: str  # shell command (run as the user) that installs stress-ng
    gen_cmdline: bool  # no cmdline.txt on the Pi: the installer writes klab/cmdline.txt

    @property
    def klab(self) -> str:
        """The directory the firmware loads our kernel from (on the Pi)."""
        return f"{self.fw}/klab"

    @property
    def bundle_prefix(self) -> str:
        """The same directory inside the install bundle (paths relative to /)."""
        return self.klab.lstrip("/")

    @property
    def config_txt(self) -> str:
        return f"{self.fw}/config.txt"

    def strip_block(self, config: str | None = None) -> str:
        """sed command that removes the klab block from config.txt."""
        return f"sed -i '/^# klab begin/,/^# klab end/d' {config or self.config_txt}"

    def config_edit_script(self, config: str | None = None) -> str:
        """Shell lines that (re)write the klab block at the end of config.txt."""
        path = config or self.config_txt
        return "\n".join(
            [
                self.strip_block(path),
                f"printf '\\n%s\\n' {shlex.quote(KLAB_BLOCK)} >> {path}",
            ]
        )

    def install_script(self, release: str, country: str | None = None) -> str:
        """klab/install.sh: shipped in the bundle, run as root on the Pi after it is
        unpacked."""
        lines = [
            "#!/bin/sh",
            "# Written by `lab pi kernel install`.",
            "set -e",
            f"depmod -a {shlex.quote(release)}",
        ]
        if self.gen_cmdline:
            regdom = f" cfg80211.ieee80211_regdom={country.upper()}" if country else ""
            lines += [
                "# This OS has no cmdline.txt (U-Boot passes the arguments): write one.",
                "uuid=$(findmnt -no PARTUUID /)",
                'if [ -n "$uuid" ]; then root="PARTUUID=$uuid"; '
                "else root=$(findmnt -no SOURCE /); fi",
                f"printf '%s\\n' \"{ARCH_CMDLINE}{regdom}\" > {self.klab}/cmdline.txt",
            ]
        else:
            lines += [
                "# cmdline.txt carries a PARTUUID that changes at the image's first",
                "# boot, so copy the live file instead of shipping one.",
                f"cp {self.fw}/cmdline.txt {self.klab}/cmdline.txt",
            ]
        return "\n".join([*lines, self.config_edit_script(), "sync", ""])

    @property
    def unpack(self) -> str:
        return f"sudo tar -xzf - -C / && sudo sh {self.klab}/install.sh"

    @property
    def stock(self) -> str:
        """Command that makes the Pi boot its stock kernel again."""
        return (
            f"if grep -q '^# klab begin' {self.config_txt}; then "
            f"sudo {self.strip_block()}; fi; sync"
        )

    @property
    def status_script(self) -> str:
        return "; ".join(
            [
                'echo "running=$(uname -r)"',
                'echo "built=$(uname -v)"',
                "echo \"model=$(tr -d '\\\\0' </proc/device-tree/model)\"",
                'echo "os=$(. /etc/os-release; echo $PRETTY_NAME)"',
                f"if grep -q '^# klab begin' {self.config_txt}; then echo selected=klab; "
                "else echo selected=stock; fi",
                f"cat {self.klab}/BUILD 2>/dev/null || true",
            ]
        )


DEB = PiOs(
    "deb",
    "Raspberry Pi OS Lite (Debian)",
    "/boot/firmware",
    APT_STRESS,
    gen_cmdline=False,
)
ARCH = PiOs(
    "arch",
    "Arch Linux ARM",
    "/boot",
    PACMAN_STRESS,
    gen_cmdline=True,
)
OSES: dict[str, PiOs] = {o.name: o for o in (DEB, ARCH)}
DEFAULT_OS = "deb"  # Pis set up before `lab pi setup arch` existed


def from_os_release(text: str) -> PiOs:
    """The profile for `ID ID_LIKE` as printed from /etc/os-release (Arch Linux ARM says
    ID=archarm); anything else is treated as Raspberry Pi OS / Debian."""
    ids = text.replace('"', "").split()
    return ARCH if {"arch", "archarm"} & set(ids) else DEB
