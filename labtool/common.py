"""Shared paths, state and small helpers."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TREES = ROOT / "trees"        # kernel source trees (git clones / worktrees)
BUILDS = ROOT / "builds"      # out-of-tree build dirs (make O=...)
IMAGES = ROOT / "images"      # rootfs images
RUNS = ROOT / "runs"          # benchmark runs and results
SHARED = ROOT / "shared"      # mounted at /mnt/lab during interactive boots
CONFIGS = ROOT / "configs"    # kconfig fragments
BENCHMARKS = ROOT / "benchmarks"
ROOTFS_SRC = ROOT / "rootfs"
STATE_FILE = ROOT / ".lab" / "state.json"

ROOTFS_IMAGE = IMAGES / "rootfs.ext4"


class LabError(Exception):
    pass


# --dry: print the commands that would run (as a shell script) and change nothing.
# Read-only queries (git describe, kernel.org version lookup) still run.
DRY = False


def set_dry(value):
    global DRY
    DRY = bool(value)


def info(msg):
    if DRY:
        print(f"# {msg}", flush=True)
    else:
        print(f"\033[1;34m==>\033[0m {msg}", flush=True)


def show(cmd, cwd=None):
    """Echo a command: dimmed '+ cmd' normally, plain copy-pasteable shell in --dry."""
    line = shlex.join(str(c) for c in cmd)
    if cwd:
        line = f"(cd {shlex.quote(str(cwd))} && {line})"
    if DRY:
        print(line, flush=True)
    else:
        print(f"\033[2m+ {line}\033[0m", flush=True)


def warn(msg):
    print(f"\033[1;33mwarning:\033[0m {msg}", file=sys.stderr, flush=True)


def run(cmd, cwd=None, env=None, check=True, capture=False, quiet=False, **kw):
    """Run a command, echoing it first. Returns CompletedProcess.
    In --dry mode only prints it and returns a successful, empty result."""
    cmd = [str(c) for c in cmd]
    if DRY:
        show(cmd, cwd)
        return subprocess.CompletedProcess(cmd, 0, stdout="" if capture else None)
    if not quiet:
        show(cmd, cwd)
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    try:
        return subprocess.run(
            cmd, cwd=cwd, env=full_env, check=check, text=True,
            stdout=subprocess.PIPE if capture else None, **kw)
    except subprocess.CalledProcessError as e:
        raise LabError(f"command failed (exit {e.returncode}): {shlex.join(cmd)}")


def output(cmd, cwd=None):
    """Run quietly and return stripped stdout ('' on failure)."""
    try:
        return subprocess.run([str(c) for c in cmd], cwd=cwd, text=True, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def have(tool):
    return shutil.which(tool) is not None


def free_gb(path=ROOT):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1e9


def check_disk(need_gb, what):
    if DRY:
        return
    free = free_gb()
    if free < need_gb:
        warn(f"only {free:.1f} GB free; {what} typically needs ~{need_gb} GB. "
             "Use `lab rm ...` to clean up old trees/builds.")


def du(path):
    """Actual disk usage of a path in bytes (sparse-aware)."""
    out = output(["du", "-s", "--block-size=1", path])
    return int(out.split()[0]) if out else 0


def human(n):
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(**updates):
    if DRY:
        return
    state = load_state()
    state.update(updates)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def table(rows, headers):
    """Render a simple left-aligned text table."""
    rows = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, r)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines += [fmt.format(*r) for r in rows]
    return "\n".join(lines)


def dry_or_raise(msg):
    """A precondition failed: fatal normally, just a warning in --dry."""
    if DRY:
        warn(msg)
    else:
        raise LabError(msg)


def remove_tree(path):
    """rm -rf, or just print it in --dry."""
    if DRY:
        show(["rm", "-rf", path])
    else:
        shutil.rmtree(path, ignore_errors=True)
