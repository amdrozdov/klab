"""`lab` command line interface."""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any

from . import bench, boottrace, clean, kernel, observe, pi, pistress, vm
from .common import (
    BUILDS,
    ROOTFS_IMAGE,
    TREES,
    LabError,
    du,
    free_gb,
    have,
    human,
    load_state,
    output,
    set_dry,
    table,
)


def cmd_doctor(_: argparse.Namespace) -> int:
    ok = True

    def check(cond: object, what: str, hint: str = "") -> None:
        nonlocal ok
        mark = "\033[32mok\033[0m  " if cond else "\033[31mMISSING\033[0m"
        print(f"  {mark} {what}" + ("" if cond else f"   -> {hint}"))
        ok &= bool(cond)

    print("build tools:")
    for t in ("git", "make", "gcc", "flex", "bison", "bc", "perl"):
        check(have(t), t, "sudo apt install build-essential flex bison bc")
    check(
        os.path.exists("/usr/include/libelf.h"),
        "libelf-dev (objtool)",
        "sudo apt install libelf-dev",
    )
    check(
        os.path.exists("/usr/include/openssl/ssl.h"),
        "libssl-dev",
        "sudo apt install libssl-dev",
    )
    check(have("pahole"), "pahole (BTF for eBPF)", "sudo apt install dwarves")
    print("optional:")
    for t, why in (
        ("ccache", "much faster rebuilds"),
        ("aarch64-linux-gnu-gcc", "build for the Pi: apt install gcc-aarch64-linux-gnu"),
        ("mcopy", "lab pi setup: apt install mtools"),
        ("fakeroot", "lab pi setup arch: apt install fakeroot"),
        ("mkfs.vfat", "lab pi setup arch: apt install dosfstools"),
        ("openssl", "lab pi setup: password hashing"),
        ("nmap", "find the Pi on your network: apt install nmap"),
        (
            "virt-customize",
            "boot a build against a distro image: apt install libguestfs-tools",
        ),
    ):
        mark = "\033[32mok\033[0m  " if have(t) else "\033[33mmissing\033[0m"
        print(f"  {mark} {t}  ({why})")
    print("vm:")
    check(
        have("qemu-system-x86_64"),
        "qemu-system-x86_64",
        "sudo apt install qemu-system-x86",
    )
    check(
        os.access("/dev/kvm", os.R_OK | os.W_OK),
        "/dev/kvm access",
        "sudo usermod -aG kvm $USER",
    )
    check(
        have("docker") and output(["docker", "info", "--format", "{{.ServerVersion}}"]),
        "docker (rootfs build)",
        "install docker and add yourself to the docker group",
    )
    check(ROOTFS_IMAGE.exists(), "rootfs image", "./lab rootfs")
    free = free_gb()
    print(
        f"disk: {free:.1f} GB free"
        + (
            "  \033[33m(tight: a debug build alone can use 5+ GB)\033[0m"
            if free < 15
            else ""
        )
    )
    gov = output(["cat", "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"])
    if gov:
        print(
            f"cpu governor: {gov}"
            + (
                ""
                if gov == "performance"
                else "  (for stable benchmarks: "
                "sudo cpupower frequency-set -g performance)"
            )
        )
    return 0 if ok else 1


def cmd_ls(_: argparse.Namespace) -> None:
    state = load_state()
    trees = kernel.list_trees()
    print("trees:")
    if trees:
        rows: list[list[Any]] = []
        for t in trees:
            ti = kernel.tree_info(t)
            kind = "variant" if ti["worktree"] else "clone"
            cur = "*" if state.get("tree") == t else ""
            rows.append(
                [cur + t, ti["describe"], kind, len(ti["patches"]), human(du(TREES / t))]
            )
        print(table(rows, ["name", "describe", "kind", "commits", "size"]))
    else:
        print("  (none; `lab fetch`)")
    print("\nbuilds:")
    builds = kernel.list_builds()
    if builds:
        rows = []
        for b in builds:
            m = kernel.build_meta(b)
            cur = "*" if b in (state.get("build"), state.get("pi_build")) else ""
            img = kernel.build_image(b).exists()
            rows.append(
                [
                    cur + b,
                    m.get("tree", "?"),
                    m.get("profile", "?")
                    + ("" if kernel.build_arch(b) == "x86" else " [pi]"),
                    m.get("kernelrelease", "-") if img else "(not built)",
                    m.get("config_hash", ""),
                    human(du(BUILDS / b)),
                ]
            )
        print(table(rows, ["name", "tree", "profile", "release", "config", "size"]))
    else:
        print("  (none; `lab build`)")
    print(
        f"\nrootfs: "
        f"{'images/rootfs.ext4' if ROOTFS_IMAGE.exists() else '(none; `lab rootfs`)'}"
    )
    print(
        f"runs: {len(bench.list_runs())}   disk free: {free_gb():.1f} GB   "
        f"(* = current default)"
    )


def cmd_rtree(a: argparse.Namespace) -> None:
    tags, branches, status = kernel.remote_versions()
    local_series, local_version = kernel.local_versions()
    branch_set = set(branches)

    if a.branches:
        rows: list[list[Any]] = []
        for b in sorted(
            branches,
            key=lambda b: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", b)],
            reverse=True,
        ):
            series = b[len("linux-") : -len(".y")] if b.endswith(".y") else ""
            rows.append(
                [
                    b,
                    status.get(series, "EOL" if series else "-"),
                    ", ".join(local_version.get(b, []))
                    or (b if b in kernel.list_trees() else ""),
                ]
            )
        print(table(rows if a.all else rows[: a.limit], ["branch", "status", "local"]))
        _rtree_footer(
            a, len(rows), "lab fetch linux-6.12.y   (shallow snapshot of the branch head)"
        )
        return

    by_series: dict[str, list[dict[str, Any]]] = {}
    for t in tags:
        by_series.setdefault(t["series"], []).append(t)

    if a.series and a.series in by_series:
        # One series: every version in it, newest first.
        rows = []
        hide_rc = not a.rc and any(not x["rc"] for x in by_series[a.series])
        for t in by_series[a.series]:
            if t["rc"] and hide_rc:
                continue
            rows.append(
                [
                    t["version"],
                    "rc" if t["rc"] else "release",
                    ", ".join(local_version.get(t["version"], [])),
                ]
            )
        st = status.get(a.series, "EOL")
        br = f"linux-{a.series}.y" if f"linux-{a.series}.y" in branch_set else "-"
        print(f"series {a.series}: {st}, branch {br}\n")
        print(table(rows if a.all else rows[: a.limit], ["version", "kind", "local"]))
        _rtree_footer(
            a,
            len(rows),
            f"lab fetch {rows[0][0]}"
            + ("   (--rc to include release candidates)" if hide_rc else ""),
        )
        return

    # Overview: one line per series, newest first, optionally filtered
    # by prefix (`lab rtree 6`).
    rows = []
    for series, vs in by_series.items():
        if a.series and not (series + ".").startswith(a.series.rstrip(".") + "."):
            continue
        releases = [v for v in vs if not v["rc"]]
        latest = releases[0]["version"] if releases else vs[0]["version"]
        rows.append(
            [
                series,
                latest,
                len(releases),
                sum(1 for v in vs if v["rc"]),
                status.get(series, "EOL"),
                f"linux-{series}.y" if f"linux-{series}.y" in branch_set else "-",
                ", ".join(local_series.get(series, [])),
            ]
        )
    if not rows:
        raise LabError(f"no kernel series matching '{a.series}'")
    total = len(rows)
    if not a.all:
        # Always keep every supported series (mainline/stable/longterm), then fill
        # up to --limit with the most recent EOL ones.
        rows = [r for i, r in enumerate(rows) if r[4] != "EOL" or i < a.limit]
    print(
        table(rows, ["series", "latest", "releases", "rcs", "status", "branch", "local"])
    )
    more = (
        f"  (showing {len(rows)} of {total}; --all for everything)"
        if len(rows) < total
        else ""
    )
    print(f"\nnext: lab rtree {rows[0][0]}   (all versions of a series){more}")


def _rtree_footer(a: argparse.Namespace, total: int, hint: str) -> None:
    shown = total if a.all else min(total, a.limit)
    more = (
        f"  (showing {shown} of {total}; --all for everything)" if shown < total else ""
    )
    print(f"\nnext: {hint}{more}")


def cmd_fetch(a: argparse.Namespace) -> None:
    kernel.fetch(a.version, a.name)


def cmd_tree_new(a: argparse.Namespace) -> None:
    kernel.tree_new(a.name, a.base, a.patch)


def cmd_tree_patch(a: argparse.Namespace) -> None:
    kernel.apply_patches(kernel.default_tree(a.tree), a.patches)


def cmd_tree_show(a: argparse.Namespace) -> None:
    ti = kernel.tree_info(kernel.default_tree(a.tree))
    print(
        f"{ti['name']}: {ti['describe']} (base {ti['base']}, commit {ti['commit'][:12]})"
    )
    for p in ti["patches"]:
        print(f"  {p}")


def cmd_build(a: argparse.Namespace) -> None:
    kernel.build(
        a.tree,
        a.profile,
        a.fragment,
        a.name,
        a.jobs,
        a.reconfig,
        a.menuconfig,
        a.config_only,
        base_config=a.base_config,
        raw=a.raw,
        no_debug_info=a.no_debug_info,
        arch=a.arch,
    )


def cmd_diffconfig(a: argparse.Namespace) -> None:
    kernel.diffconfig(a.a, a.b)


def cmd_rootfs(a: argparse.Namespace) -> None:
    vm.rootfs(a.size, a.force, a.keep_image)


def vm_kwargs(a: argparse.Namespace) -> dict[str, Any]:
    return {"cpus": a.cpus, "mem": a.mem, "pin": a.pin, "append": a.append}


def cmd_boot(a: argparse.Namespace) -> None:
    vm.boot(
        kernel.default_build(a.build),
        no_telemetry=a.no_telemetry,
        gdb=a.gdb,
        persist=a.persist,
        **vm_kwargs(a),
    )


def cmd_gdb(a: argparse.Namespace) -> None:
    vm.gdb(kernel.default_build(a.build))


def cmd_bench_list(_: argparse.Namespace) -> None:
    rows = [
        [b, bench.bench_header(b).get("description", "")] for b in bench.list_benchmarks()
    ]
    print(table(rows, ["benchmark", "description"]))


def cmd_bench_run(a: argparse.Namespace) -> None:
    bench.run_bench(
        a.build,
        a.benchmarks,
        a.repeat,
        a.warmup,
        a.boots,
        timeout=a.timeout,
        label=a.label,
        verbose=a.verbose,
        no_telemetry=a.no_telemetry,
        **vm_kwargs(a),
    )


def cmd_boottrace(a: argparse.Namespace) -> None:
    if a.resend:
        boottrace.resend()
        return
    boottrace.boottrace(
        a.build,
        a.boots,
        timeout=a.timeout,
        label=a.label,
        no_telemetry=a.no_telemetry,
        top=a.top,
        **vm_kwargs(a),
    )


def cmd_observe(a: argparse.Namespace) -> None:
    {"up": observe.up, "status": observe.status, "down": lambda: observe.down(a.wipe)}[
        a.action
    ]()


def cmd_runs(_: argparse.Namespace) -> None:
    rows = []
    for r in bench.list_runs():
        d = bench.load_run(r)
        rows.append(
            [
                r,
                d["describe"],
                ",".join(d["results"]),
                d["params"]["repeat"],
                d["params"]["boots"],
            ]
        )
    print(
        table(rows, ["run", "describe", "benchmarks", "repeat", "boots"])
        if rows
        else "(no runs)"
    )


def cmd_show(a: argparse.Namespace) -> None:
    bench.show_run(a.run)


def cmd_compare(a: argparse.Namespace) -> None:
    bench.compare(a.runs, a.threshold)


def cmd_clean(a: argparse.Namespace) -> None:
    clean.clean(yes=a.yes)


def cmd_rm(a: argparse.Namespace) -> None:
    for name in a.names:
        {"tree": kernel.tree_rm, "build": kernel.build_rm, "run": bench.rm_run}[a.kind](
            name
        )


def cmd_pi_setup(a: argparse.Namespace) -> None:
    pi.setup(
        a.os,
        {
            "user": a.user,
            "password": a.password,
            "hostname": a.hostname,
            "wifi_ssid": a.wifi_ssid,
            "wifi_psk": a.wifi_psk,
            "wifi_country": a.wifi_country,
        },
        device=a.device,
        ask=a.ask,
        yes=a.yes,
        no_write=a.no_write,
        keep_image=a.keep_image,
    )


def cmd_pi_shell(a: argparse.Namespace) -> int:
    return pi.shell(a.remote)


def cmd_pi_host(a: argparse.Namespace) -> None:
    pi.host(a.addr, a.port)


def cmd_pi_show(a: argparse.Namespace) -> None:
    pi.show_config(a.show_secrets)


def cmd_pi_stress(a: argparse.Namespace) -> None:
    if a.list:
        for name, what in pistress.list_profiles().items():
            print(f"{name:10} {what}")
        return
    pistress.stress(a.profile, a.repeat, a.timeout, a.label, a.workers)


def cmd_pi_kernel_install(a: argparse.Namespace) -> None:
    pi.kernel_install(a.build, reboot=a.reboot)


def cmd_pi_kernel_status(_: argparse.Namespace) -> None:
    pi.kernel_status()


def cmd_pi_kernel_stock(a: argparse.Namespace) -> None:
    pi.kernel_stock(reboot=a.reboot)


def add_vm_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--cpus", type=int, default=vm.DEFAULT_CPUS, help="vCPUs (default %(default)s)"
    )
    p.add_argument(
        "--mem", default=vm.DEFAULT_MEM, help="guest RAM (default %(default)s)"
    )
    p.add_argument("--pin", metavar="CPULIST", help="pin qemu to host CPUs, e.g. 4-7")
    p.add_argument("--append", default="", help="extra kernel command line")
    p.add_argument(
        "--no-telemetry",
        action="store_true",
        help="don't export telemetry even if the observe stack is running",
    )


DRY_HELP = (
    "only print the commands that would run (build, qemu, git, docker); change nothing"
)


def parser() -> argparse.ArgumentParser:
    # --dry works both before and after the subcommand:
    # `lab --dry boot` / `lab boot --dry`.
    # SUPPRESS keeps a subcommand from resetting a top-level --dry back to False.
    dry = argparse.ArgumentParser(add_help=False)
    dry.add_argument(
        "--dry",
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help=DRY_HELP,
    )

    p = argparse.ArgumentParser(
        prog="lab", description="Kernel build / boot / benchmark lab."
    )
    p.add_argument(
        "--dry", "--dry-run", action="store_true", default=False, help=DRY_HELP
    )
    # Not required: `lab` with no command prints full help (see main()).
    sub = p.add_subparsers(dest="cmd", required=False, metavar="COMMAND")

    s = sub.add_parser("doctor", parents=[dry], help="check host dependencies")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("ls", parents=[dry], help="list trees, builds, rootfs, runs")
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser(
        "rtree",
        parents=[dry],
        help="list kernel versions/branches available on kernel.org",
    )
    s.add_argument(
        "series",
        nargs="?",
        help="6.12 = all versions of that series; 6 = only 6.x series",
    )
    s.add_argument(
        "-b", "--branches", action="store_true", help="list git branches instead"
    )
    s.add_argument(
        "--rc", action="store_true", help="include -rc tags in a series listing"
    )
    s.add_argument(
        "-n", "--limit", type=int, default=15, help="rows to show (default 15)"
    )
    s.add_argument("-a", "--all", action="store_true", help="show all rows")
    s.set_defaults(func=cmd_rtree)

    s = sub.add_parser(
        "fetch", parents=[dry], help="shallow-clone a kernel version or branch"
    )
    s.add_argument(
        "version",
        nargs="?",
        default="stable",
        help="stable (default) | mainline | longterm | 6.12.3 | v6.13-rc2 | "
        "linux-6.12.y | master  (see `lab rtree`)",
    )
    s.add_argument("--name", help="tree name (default: v<version>)")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("tree", parents=[dry], help="manage source variants and patches")
    tsub = s.add_subparsers(dest="tcmd", required=True, metavar="SUBCOMMAND")
    t = tsub.add_parser(
        "new", parents=[dry], help="create a variant (git worktree) of a tree"
    )
    t.add_argument("name")
    t.add_argument("--from", dest="base", help="base tree (default: current)")
    t.add_argument(
        "--patch", action="append", default=[], help="patch file to apply (repeatable)"
    )
    t.set_defaults(func=cmd_tree_new)
    t = tsub.add_parser(
        "patch", parents=[dry], help="apply patch files as commits to a tree"
    )
    t.add_argument("patches", nargs="+")
    t.add_argument("--tree", help="tree (default: current)")
    t.set_defaults(func=cmd_tree_patch)
    t = tsub.add_parser(
        "show", parents=[dry], help="show a tree's base and commits on top"
    )
    t.add_argument("tree", nargs="?")
    t.set_defaults(func=cmd_tree_show)

    s = sub.add_parser("build", parents=[dry], help="configure and build a tree")
    s.add_argument("tree", nargs="?", help="tree (default: current)")
    s.add_argument(
        "-p", "--profile", default="perf", help="perf (default) | debug | configs/<x>"
    )
    s.add_argument(
        "-f",
        "--fragment",
        action="append",
        default=[],
        help="extra config fragment from configs/ (repeatable)",
    )
    s.add_argument(
        "-n",
        "--name",
        help="build name (default: <tree>-<profile>, <tree>-pi for --arch pi); "
        "for --arch pi it becomes part of `uname -r` on the Pi",
    )
    s.add_argument("-j", "--jobs", type=int)
    s.add_argument(
        "-a",
        "--arch",
        choices=["x86", "pi"],
        default="x86",
        help="x86 = lab VM kernel (default) | pi = Raspberry Pi 4 (arm64, cross-built; "
        "install with `lab pi kernel install`)",
    )
    s.add_argument(
        "-c",
        "--base-config",
        default=None,
        metavar="SOURCE",
        help="start from a full config instead of defconfig+kvm_guest: "
        "host | arch | <path> | <url>  (host = /boot/config-$(uname -r)); "
        "for --arch pi the default is 'pi' = the config of the kernel on your Pi",
    )
    s.add_argument(
        "--raw",
        action="store_true",
        help="with --base-config: don't add the lab VM-boot overlay "
        "(exact ship config; may not boot the lab VM)",
    )
    s.add_argument(
        "--no-debug-info",
        action="store_true",
        help="disable DWARF/BTF debug info (~halves build size; drops "
        "source-level gdb and BTF-based eBPF)",
    )
    s.add_argument(
        "--reconfig", action="store_true", help="regenerate .config from scratch"
    )
    s.add_argument(
        "--menuconfig", action="store_true", help="run menuconfig before building"
    )
    s.add_argument("--config-only", action="store_true", help="only generate .config")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("diffconfig", parents=[dry], help="diff the .config of two builds")
    s.add_argument("a")
    s.add_argument("b")
    s.set_defaults(func=cmd_diffconfig)

    s = sub.add_parser(
        "rootfs", parents=[dry], help="build the Debian rootfs image (via docker)"
    )
    s.add_argument("--size", default="4G")
    s.add_argument("--force", action="store_true")
    s.add_argument(
        "--keep-image", action="store_true", help="keep the docker image afterwards"
    )
    s.set_defaults(func=cmd_rootfs)

    s = sub.add_parser(
        "boot", parents=[dry], help="boot a build interactively (Ctrl-A X to quit)"
    )
    s.add_argument("build", nargs="?", help="build (default: last built)")
    s.add_argument(
        "--gdb", action="store_true", help="start paused with gdb stub on :1234"
    )
    s.add_argument("--persist", action="store_true", help="keep rootfs changes")
    add_vm_args(s)
    s.set_defaults(func=cmd_boot)

    s = sub.add_parser(
        "gdb", parents=[dry], help="attach gdb to a VM started with `boot --gdb`"
    )
    s.add_argument("build", nargs="?")
    s.set_defaults(func=cmd_gdb)

    s = sub.add_parser("bench", parents=[dry], help="list / run benchmarks")
    bsub = s.add_subparsers(dest="bcmd", required=True, metavar="SUBCOMMAND")
    b = bsub.add_parser("list", parents=[dry], help="list available benchmarks")
    b.set_defaults(func=cmd_bench_list)
    b = bsub.add_parser(
        "run", parents=[dry], help="boot a build and run benchmarks in it"
    )
    b.add_argument("benchmarks", nargs="*", help="benchmark names (default: all)")
    b.add_argument("-b", "--build", help="build (default: last built)")
    b.add_argument(
        "-r", "--repeat", type=int, default=5, help="iterations per boot (default 5)"
    )
    b.add_argument(
        "-w", "--warmup", type=int, default=1, help="discarded iterations (default 1)"
    )
    b.add_argument("--boots", type=int, default=1, help="fresh VM boots (default 1)")
    b.add_argument(
        "--timeout", type=int, default=900, help="seconds per boot (default 900)"
    )
    b.add_argument("-l", "--label", help="tag appended to the run id")
    b.add_argument(
        "-v", "--verbose", action="store_true", help="stream the guest console"
    )
    add_vm_args(b)
    b.set_defaults(func=cmd_bench_run)

    s = sub.add_parser(
        "boottrace",
        parents=[dry],
        help="trace every boot step (initcalls, phases, systemd units)",
    )
    s.add_argument("build", nargs="?", help="build (default: last built)")
    s.add_argument("--boots", type=int, default=1, help="boots to record (default 1)")
    s.add_argument("--top", type=int, default=15, help="rows in the slowest-N tables")
    s.add_argument(
        "--resend",
        action="store_true",
        help="only re-upload saved traces that Tempo is missing",
    )
    s.add_argument("-l", "--label", help="tag appended to the run id")
    s.add_argument("--timeout", type=int, default=300, help="seconds per boot")
    add_vm_args(s)
    s.set_defaults(func=cmd_boottrace)

    s = sub.add_parser(
        "observe",
        parents=[dry],
        help="telemetry stack: VictoriaMetrics + Grafana (docker compose)",
    )
    s.add_argument("action", nargs="?", default="up", choices=["up", "down", "status"])
    s.add_argument(
        "--wipe", action="store_true", help="with down: delete stored metrics too"
    )
    s.set_defaults(func=cmd_observe)

    s = sub.add_parser("runs", parents=[dry], help="list benchmark runs")
    s.set_defaults(func=cmd_runs)

    s = sub.add_parser("show", parents=[dry], help="show one run's results")
    s.add_argument("run", help="run id, unique substring, or latest[:build]")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser(
        "compare", parents=[dry], help="compare runs (first one is the baseline)"
    )
    s.add_argument("runs", nargs="+", help="run ids / substrings / latest:<build>")
    s.add_argument(
        "-t", "--threshold", type=float, help="min %% change to flag (default: noise)"
    )
    s.set_defaults(func=cmd_compare)

    s = sub.add_parser(
        "clean",
        parents=[dry],
        help="delete ALL lab data (trees, builds, rootfs, runs), asks first",
    )
    s.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    s.set_defaults(func=cmd_clean)

    s = sub.add_parser("rm", parents=[dry], help="delete trees, builds or runs")
    s.add_argument("kind", choices=["tree", "build", "run"])
    s.add_argument("names", nargs="+")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser(
        "pi",
        parents=[dry],
        help="set up a Raspberry Pi (OS on a USB/SD stick) and ssh in",
    )
    psub = s.add_subparsers(dest="picmd", required=True, metavar="SUBCOMMAND")
    r = psub.add_parser(
        "setup",
        parents=[dry],
        help="write Raspberry Pi OS to a USB/SD stick with user, ssh and Wi-Fi preset",
    )
    r.add_argument(
        "os",
        choices=sorted(pi.OS_CHOICES),
        help="deb = Raspberry Pi OS Lite (Debian) | arch = Arch Linux ARM",
    )
    r.add_argument(
        "-d", "--device", help="target disk, e.g. /dev/sdb (default: autodetect)"
    )
    r.add_argument("-u", "--user", help="username (asked once, then saved)")
    r.add_argument("--password", help="password (prefer the prompt: argv shows in ps)")
    r.add_argument("--hostname", help=f"hostname (default {pi.DEFAULT_HOSTNAME})")
    r.add_argument("--wifi-ssid", help="Wi-Fi network name; '' = no Wi-Fi")
    r.add_argument("--wifi-psk", help="Wi-Fi password (prefer the prompt)")
    r.add_argument("--wifi-country", help="Wi-Fi regulatory country, e.g. DE")
    r.add_argument("--ask", action="store_true", help="ask for every setting again")
    r.add_argument("-y", "--yes", action="store_true", help="don't ask before erasing")
    r.add_argument(
        "--no-write", action="store_true", help="only prepare images/pi/custom.img"
    )
    r.add_argument(
        "--keep-image", action="store_true", help="keep the customised image afterwards"
    )
    r.set_defaults(func=cmd_pi_setup)
    r = psub.add_parser("shell", parents=[dry], help="ssh into the saved Raspberry Pi")
    r.add_argument("remote", nargs="*", help="run this command instead of a shell")
    r.set_defaults(func=cmd_pi_shell)
    r = psub.add_parser("host", parents=[dry], help="show or save the Pi's address")
    r.add_argument("addr", nargs="?", metavar="HOST", help="IP address or hostname")
    r.add_argument("-p", "--port", type=int, help="ssh port (default 22)")
    r.set_defaults(func=cmd_pi_host)
    r = psub.add_parser("show", parents=[dry], help="show the saved Pi settings")
    r.add_argument("--show-secrets", action="store_true", help="print passwords too")
    r.set_defaults(func=cmd_pi_show)
    r = psub.add_parser(
        "stress",
        parents=[dry],
        help="run stress-ng on the Pi and save the result in runs/",
    )
    r.add_argument(
        "profile",
        nargs="?",
        default=pistress.DEFAULT_PROFILE,
        help="stressor set from benchmarks/pi/ (default %(default)s; --list shows all)",
    )
    r.add_argument("-r", "--repeat", type=int, default=3, help="runs per stressor (3)")
    r.add_argument(
        "-t", "--timeout", type=int, default=20, help="seconds per stressor (20)"
    )
    r.add_argument("-l", "--label", help="tag appended to the run id")
    r.add_argument(
        "--workers", type=int, help="workers per stressor (default: the Pi's CPU count)"
    )
    r.add_argument("--list", action="store_true", help="list the profiles and exit")
    r.set_defaults(func=cmd_pi_stress)
    r = psub.add_parser(
        "kernel", parents=[dry], help="install a kernel built with `lab build --arch pi`"
    )
    ksub = r.add_subparsers(dest="kcmd", required=True, metavar="ACTION")
    k = ksub.add_parser(
        "install", parents=[dry], help="copy a Pi build to the Pi and boot it from now on"
    )
    k.add_argument(
        "build", nargs="?", help="build name (default: last `--arch pi` build)"
    )
    k.add_argument("--reboot", action="store_true", help="reboot the Pi afterwards")
    k.set_defaults(func=cmd_pi_kernel_install)
    k = ksub.add_parser("status", parents=[dry], help="what the Pi runs and boots next")
    k.set_defaults(func=cmd_pi_kernel_status)
    k = ksub.add_parser(
        "stock", parents=[dry], help="boot the stock Raspberry Pi OS kernel again"
    )
    k.add_argument("--reboot", action="store_true", help="reboot the Pi afterwards")
    k.set_defaults(func=cmd_pi_kernel_stock)
    return p


def main(argv: list[str] | None = None) -> int:
    p = parser()
    args = p.parse_args(argv)
    if getattr(args, "func", None) is None:
        # No command given: show full help instead of a terse usage error.
        p.print_help()
        return 0
    set_dry(args.dry)
    try:
        return args.func(args) or 0
    except LabError as e:
        print(f"\033[1;31merror:\033[0m {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
