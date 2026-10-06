"""Root filesystem image and QEMU launching."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import common, observe
from .common import (
    IMAGES,
    ROOTFS_IMAGE,
    ROOTFS_SRC,
    SHARED,
    LabError,
    check_disk,
    dry_or_raise,
    have,
    info,
    run,
    show,
    warn,
)
from .kernel import build_dir, require_x86

ROOTFS_DOCKER_TAG = "klab-rootfs"
DEFAULT_CPUS = 4
DEFAULT_MEM = "4G"


# --------------------------------------------------------------------------- rootfs


def rootfs_rev() -> str:
    """Content hash of rootfs/ (Dockerfile + overlay); baked into the image."""
    h = hashlib.sha256()
    for p in sorted(ROOTFS_SRC.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(ROOTFS_SRC)).encode() + b"\0" + p.read_bytes())
    return h.hexdigest()[:16]


def rootfs_state() -> str:
    """'missing', 'outdated' (rootfs/ changed since the image was built) or 'current'."""
    if not ROOTFS_IMAGE.exists():
        return "missing"
    try:
        baked = subprocess.run(
            ["debugfs", "-R", "cat /etc/klab-rootfs", ROOTFS_IMAGE],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "current"  # can't tell; don't force a rebuild
    return "current" if baked == rootfs_rev() else "outdated"


def rootfs(size: str = "4G", force: bool = False, keep_image: bool = False) -> None:
    """Build a Debian ext4 rootfs with Docker (no root on the host needed)."""
    if ROOTFS_IMAGE.exists() and not force:
        info(f"{ROOTFS_IMAGE} exists (use --force to rebuild)")
        return
    if not have("docker"):
        raise LabError("docker is required to build the rootfs")
    check_disk(3, "the rootfs image")
    if common.DRY:
        _rootfs_dry(size, keep_image)
        return
    IMAGES.mkdir(exist_ok=True)
    info("building rootfs docker image")
    run(
        [
            "docker",
            "build",
            "--build-arg",
            f"LAB_ROOTFS_REV={rootfs_rev()}",
            "-t",
            ROOTFS_DOCKER_TAG,
            ROOTFS_SRC,
        ]
    )

    info(f"exporting container filesystem into {ROOTFS_IMAGE.name} ({size}, sparse)")
    cid = run(["docker", "create", ROOTFS_DOCKER_TAG], capture=True).stdout.strip()
    tmp = ROOTFS_IMAGE.with_suffix(".tmp")
    try:
        # Export as a tar stream and unpack it as root inside a second container,
        # so file ownership is preserved in the ext4 image.
        export = subprocess.Popen(["docker", "export", cid], stdout=subprocess.PIPE)
        assert export.stdout is not None
        run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "-v",
                f"{IMAGES}:/out",
                ROOTFS_DOCKER_TAG,
                "/usr/local/sbin/lab-mkrootfs",
                f"/out/{tmp.name}",
                size,
                str(os.getuid()),
                str(os.getgid()),
            ],
            stdin=export.stdout,
        )
        export.stdout.close()
        if export.wait() != 0:
            raise LabError("docker export failed")
    finally:
        run(["docker", "rm", "-f", cid], capture=True, quiet=True, check=False)
    tmp.rename(ROOTFS_IMAGE)
    if not keep_image:
        # The ext4 image is all we need; don't keep ~1 GB of docker layers around.
        run(["docker", "image", "rm", ROOTFS_DOCKER_TAG], capture=True, check=False)
    info(f"rootfs ready: {ROOTFS_IMAGE}")


def _rootfs_dry(size: str, keep_image: bool) -> None:
    """Print the rootfs pipeline as one shell script."""
    tmp = ROOTFS_IMAGE.with_suffix(".tmp")
    mkfs = shlex.join(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            "-v",
            f"{IMAGES}:/out",
            ROOTFS_DOCKER_TAG,
            "/usr/local/sbin/lab-mkrootfs",
            f"/out/{tmp.name}",
            size,
            str(os.getuid()),
            str(os.getgid()),
        ]
    )
    show(["mkdir", "-p", IMAGES])
    show(
        [
            "docker",
            "build",
            "--build-arg",
            f"LAB_ROOTFS_REV={rootfs_rev()}",
            "-t",
            ROOTFS_DOCKER_TAG,
            ROOTFS_SRC,
        ]
    )
    print(f"cid=$(docker create {ROOTFS_DOCKER_TAG})")
    print(f'docker export "$cid" | {mkfs}')
    print('docker rm -f "$cid"')
    show(["mv", tmp, ROOTFS_IMAGE])
    if not keep_image:
        show(["docker", "image", "rm", ROOTFS_DOCKER_TAG])


# --------------------------------------------------------------------------- qemu


def qemu_cmd(
    build: str,
    share_dir: Path,
    cpus: int = DEFAULT_CPUS,
    mem: str = DEFAULT_MEM,
    gdb: bool = False,
    persist: bool = False,
    append: str = "",
    job: bool = False,
    pin: str | None = None,
    net: bool = True,
    telemetry: observe.Session | None = None,
) -> list[str]:
    require_x86(build)
    if not ROOTFS_IMAGE.exists():
        dry_or_raise("no rootfs image yet (run `lab rootfs`)")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        dry_or_raise("/dev/kvm is not accessible (add yourself to the 'kvm' group)")
    kernel = build_dir(build) / "arch/x86/boot/bzImage"
    if not kernel.exists():
        dry_or_raise(f"build '{build}' has no bzImage yet (run `lab build`)")
    modules = build_dir(build) / "modroot/lib/modules"

    cmdline: list[str] = [
        "console=ttyS0",
        "root=/dev/vda",
        "rw",
        "rootfstype=ext4",
        "panic=-1",
        "net.ifnames=0",
    ]
    if gdb:
        cmdline.append("nokaslr")  # stable addresses for breakpoints
    if job:
        cmdline += ["lab.job=1", "quiet"]
    if telemetry:
        cmdline.append(telemetry.cmdline)
    if append:
        cmdline += shlex.split(append)

    drive = f"file={ROOTFS_IMAGE},if=virtio,format=raw"
    if not persist:
        drive += ",snapshot=on"  # guest writes are discarded at exit

    cmd: list[Any] = []
    if pin:
        cmd += ["taskset", "-c", pin]
    cmd += [
        "qemu-system-x86_64",
        # debug-threads names vCPU threads "CPU n/KVM" (per-vCPU host metrics).
        "-name",
        f"guest=lab-{build},debug-threads=on",
        "-machine",
        "q35,accel=kvm",
        "-cpu",
        "host",
        "-smp",
        str(cpus),
        "-m",
        str(mem),
        "-kernel",
        kernel,
        "-append",
        " ".join(cmdline),
        "-drive",
        drive,
        "-virtfs",
        f"local,path={share_dir},mount_tag=lab,security_model=none,id=lab",
        "-device",
        "virtio-rng-pci",
        "-no-reboot",
    ]
    if modules.exists():
        cmd += [
            "-virtfs",
            f"local,path={modules},mount_tag=mods,"
            f"security_model=none,readonly=on,id=mods",
        ]
    if net:
        cmd += [
            "-nic",
            ",".join(
                ["user,model=virtio-net-pci", *(telemetry.hostfwd if telemetry else [])]
            ),
        ]
    if gdb:
        cmd += ["-s", "-S"]  # gdbstub on :1234, wait for debugger
    if job:
        cmd += ["-display", "none", "-serial", "stdio", "-monitor", "none"]
    else:
        cmd += ["-nographic"]
    return [str(c) for c in cmd]


def boot(build: str, no_telemetry: bool = False, **kw: Any) -> None:
    """Interactive boot on the serial console (Ctrl-A X quits)."""
    session: observe.Session | None = None
    if observe.wanted(no_telemetry):
        observe.preflight(build)
        session = observe.Session(build, time.strftime("boot-%Y%m%d-%H%M%S"), "boot")
    cmd = qemu_cmd(build, SHARED, telemetry=session, **kw)
    info(f"booting {build}  (host ./shared <-> guest /mnt/lab, quit with Ctrl-A X)")
    if kw.get("gdb"):
        info(f"gdb stub on :1234, VM paused. In another terminal: ./lab gdb {build}")
    if session:
        info(f"telemetry on: {observe.GRAFANA_URL}  (opt out: --no-telemetry)")
    show(cmd)
    if common.DRY:
        return
    SHARED.mkdir(exist_ok=True)
    if not session:
        os.execvp(cmd[0], cmd)
    with session:  # stay around to unregister the scrape targets
        subprocess.run(cmd)


def run_job(
    build: str,
    workdir: Path,
    log_path: Path | None,
    timeout: float,
    verbose: bool = False,
    on_line: Callable[[str], None] | None = None,
    **kw: Any,
) -> float:
    """Boot non-interactively; the guest runs workdir/job.sh and powers off.
    on_line(line) is called for every console line (telemetry markers)."""
    cmd = qemu_cmd(build, workdir, job=True, **kw)
    show(cmd)
    if common.DRY:
        return 0.0
    assert log_path is not None
    start = time.monotonic()
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        assert proc.stdout is not None
        # Kill a hung guest even if it stops producing output.
        watchdog = threading.Timer(timeout, proc.kill)
        watchdog.start()
        try:
            for line in proc.stdout:
                log.write(line)
                if on_line:
                    on_line(line)
                if verbose:
                    print(f"  | {line}", end="", flush=True)
            proc.wait()
        finally:
            watchdog.cancel()
    elapsed = time.monotonic() - start
    if elapsed >= timeout:
        raise LabError(f"VM timed out after {timeout}s (console log: {log_path})")
    if proc.returncode != 0:
        warn(f"qemu exited with {proc.returncode}")
    return elapsed


def gdb(build: str) -> None:
    """Attach gdb to a VM started with `lab boot --gdb`."""
    require_x86(build)
    out = build_dir(build)
    vmlinux = out / "vmlinux"
    if not vmlinux.exists():
        dry_or_raise(f"{vmlinux} missing")
    elif "CONFIG_DEBUG_INFO_NONE=y" in (out / ".config").read_text():
        warn(
            "this build has no debug info; use a build with "
            "--profile debug for source-level gdb"
        )
    cmd = [
        "gdb",
        "-q",
        "-iex",
        f"add-auto-load-safe-path {out}",
        "-ex",
        f"file {vmlinux}",
        "-ex",
        "target remote :1234",
    ]
    if (out / "vmlinux-gdb.py").exists():
        cmd += ["-ex", "lx-version"]
    show(cmd, cwd=out)
    if common.DRY:
        return
    os.chdir(out)
    os.execvp("gdb", cmd)
