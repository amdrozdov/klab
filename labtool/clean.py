"""`lab clean`: delete all lab data (trees, builds, rootfs image, runs, ...).

Only paths that .gitignore marks as ignored are removed (git clean -X semantics), so
source files are safe even when they are not committed yet. experiments/ and configs/
are never touched, and .venv (the environment running this command) is kept.
"""

import shutil
import subprocess
import sys

from . import common
from .common import ROOT, LabError, du, human, info, show, table, warn

KEEP = ("experiments/", "configs/", ".venv/")
# Used when ROOT is not a git repository: the lab data locations from .gitignore.
FALLBACK = ["trees/", "builds/", "images/", "runs/", ".lab/", "monitoring/targets/"]


def _git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True)


def _is_repo():
    return _git("rev-parse", "--show-toplevel").stdout.strip() == str(ROOT)


def targets():
    """Paths (relative, dirs end with '/') that clean would delete."""
    if _is_repo():
        # -X: only ignored files; -ff: also nested repositories (the kernel trees).
        out = _git("clean", "-n", "-d", "-X", "-ff").stdout
        paths = [l[len("Would remove "):] for l in out.splitlines() if l.startswith("Would remove ")]
    else:
        paths = [p for p in FALLBACK if (ROOT / p).exists()]
        paths += [f"shared/{p.name}" for p in (ROOT / "shared").glob("*") if p.name != ".keep"]
    keep = [p for p in paths if p.startswith(KEEP)]
    return [p for p in paths if p not in keep]


def untracked_not_ignored():
    if not _is_repo():
        return []
    out = _git("ls-files", "--others", "--exclude-standard", "--directory").stdout
    return [p for p in out.splitlines() if not p.startswith(KEEP)]


def running_vms():
    out = subprocess.run(["pgrep", "-af", "qemu-system"], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "guest=lab-" in l]


def clean(yes=False):
    if running_vms():
        raise LabError("a lab VM is running (its rootfs/kernel would be deleted); stop it first")
    paths = targets()
    if not paths:
        info("nothing to clean")
        return

    sizes = {p: du(ROOT / p) for p in paths}
    total = sum(sizes.values())
    rows = [[p, human(sizes[p])] for p in sorted(paths, key=sizes.get, reverse=True)]
    print(table(rows, ["will delete", "size"]))
    print(f"\ntotal: {human(total)} in {len(paths)} paths; kept: tracked files, "
          f"{', '.join(KEEP)}")

    other = untracked_not_ignored()
    tracked = _git("ls-files").stdout.strip() if _is_repo() else "n/a"
    if _is_repo() and not tracked:
        warn("nothing is committed in this repository; your sources are untracked. They are "
             "NOT deleted (only .gitignore'd lab data is), but consider: git add -A && git commit")
    elif other:
        warn("untracked files that are kept (not lab data): " + ", ".join(other[:10]))
    print("not touched: the observe stack's data (docker volumes); use `lab observe down --wipe`")

    if common.DRY:
        for p in paths:
            show(["rm", "-rf", ROOT / p])
        return
    if not yes:
        if not sys.stdin.isatty():
            raise LabError("refusing to clean without confirmation (no terminal); pass --yes")
        answer = input(f"\nDelete {len(paths)} paths ({human(total)})? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            info("aborted, nothing deleted")
            return

    for p in paths:
        target = ROOT / p
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists() or target.is_symlink():
            target.unlink()
    # Keep the placeholder files the layout relies on.
    for keep in ("shared/.keep", "monitoring/targets/.keep"):
        (ROOT / keep).parent.mkdir(parents=True, exist_ok=True)
        (ROOT / keep).touch()
    left = [p for p in paths if (ROOT / p).exists()]
    if left:
        warn(f"could not remove: {', '.join(left)}")
    info(f"cleaned {human(total)}; start again with `lab fetch` / `lab rootfs` (or make observe)")
