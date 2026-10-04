"""Kernel sources (fetch, patched variants) and builds."""

import hashlib
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

from . import common
from .common import (BUILDS, CONFIGS, TREES, LabError, check_disk, dry_or_raise, have, info,
                     load_state, output, remove_tree, run, save_state, show, warn)

RELEASES_URL = "https://www.kernel.org/releases.json"
MAINLINE_GIT = "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"
STABLE_GIT = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"

# Applied on top of `make defconfig kvm_guest.config`, in this order.
BASE_FRAGMENT = "base"


# --------------------------------------------------------------------------- fetch

def resolve_version(spec):
    """Turn 'stable' / 'mainline' / 'longterm' / '6.12.3' / 'v6.12' into a version string."""
    if spec in ("stable", "latest", "mainline", "longterm"):
        with urllib.request.urlopen(RELEASES_URL, timeout=20) as r:
            data = json.load(r)
        if spec in ("stable", "latest"):
            return data["latest_stable"]["version"]
        for rel in data["releases"]:
            if rel["moniker"] == spec:
                return rel["version"]
        raise LabError(f"no '{spec}' release listed on kernel.org")
    return spec[1:] if spec.startswith("v") else spec


def is_branch(spec):
    """Branch names as listed by `lab rtree --branches` (linux-6.12.y, master, ...)."""
    return spec == "master" or spec.startswith("linux-")


def fetch(spec, name=None):
    if is_branch(spec):
        # A branch snapshot: the clone already has a local branch of that name.
        ref, url = spec, (MAINLINE_GIT if spec == "master" else STABLE_GIT)
        name = name or spec
    else:
        version = resolve_version(spec)
        ref = f"v{version}"
        name = name or ref
        # -rc tags only exist in Linus' tree; releases are in both, stable has the .y updates.
        url = MAINLINE_GIT if "-rc" in version else STABLE_GIT
    dest = TREES / name
    if dest.exists():
        info(f"tree '{name}' already exists at {dest}")
        save_state(tree=name)
        return name

    check_disk(3, "a shallow kernel clone")
    info(f"fetching {ref} (shallow clone) into trees/{name}")
    if not common.DRY:
        TREES.mkdir(exist_ok=True)
    run(["git", "-c", "advice.detachedHead=false", "clone", "--depth", "1",
         "--branch", ref, url, dest])
    if not is_branch(spec) or name != spec:
        # A named branch so patch commits / variants have something to hang on.
        run(["git", "-C", dest, "switch", "-q", "-c", name])
    save_state(tree=name)
    info(f"done: trees/{name}  (next: lab build {name})")
    return name


# --------------------------------------------------------------------------- remote listing

TAG_RE = re.compile(r"^v(\d+(?:\.\d+)+)(?:-rc(\d+))?$")


def _series_of(nums):
    """'6.12.3' -> '6.12'; the 2.6 era used a third component for the series."""
    return ".".join(map(str, nums[:3] if nums[:2] == (2, 6) else nums[:2]))


def _version_key(v):
    return (v["nums"] + (0,) * (4 - len(v["nums"])), v["rc"] or 10**6)


def remote_versions():
    """Releases, -rc tags and branches on kernel.org (stable repo mirrors mainline tags),
    annotated with each series' status from releases.json."""
    refs = output(["git", "ls-remote", "--refs", STABLE_GIT])
    if not refs:
        raise LabError(f"could not list refs of {STABLE_GIT} (network?)")
    tags, branches = [], []
    for line in refs.splitlines():
        ref = line.split("\t", 1)[1]
        if ref.startswith("refs/heads/"):
            branches.append(ref[len("refs/heads/"):])
        elif ref.startswith("refs/tags/"):
            m = TAG_RE.match(ref[len("refs/tags/"):])
            if m:
                nums = tuple(int(x) for x in m.group(1).split("."))
                rc = int(m.group(2)) if m.group(2) else None
                tags.append({"version": ref[len("refs/tags/v"):], "nums": nums, "rc": rc,
                             "series": _series_of(nums)})
    tags.sort(key=_version_key, reverse=True)

    status = {}
    try:
        with urllib.request.urlopen(RELEASES_URL, timeout=20) as r:
            for rel in json.load(r)["releases"]:
                if rel["moniker"] == "linux-next":
                    continue
                nums = tuple(int(x) for x in re.findall(r"\d+", rel["version"].split("-")[0]))
                status[_series_of(nums)] = "EOL" if rel.get("iseol") else rel["moniker"]
    except (OSError, ValueError) as e:
        warn(f"could not read {RELEASES_URL}: {e}")
    return tags, branches, status


def local_versions():
    """series -> local tree names, and version -> local tree names."""
    by_series, by_version = {}, {}
    for t in list_trees():
        base = output(["git", "-C", TREES / t, "describe", "--tags", "--abbrev=0"]) or t
        m = TAG_RE.match(base)
        if not m:
            continue
        nums = tuple(int(x) for x in m.group(1).split("."))
        by_series.setdefault(_series_of(nums), []).append(t)
        by_version.setdefault(base[1:], []).append(t)
    return by_series, by_version


# --------------------------------------------------------------------------- trees

def tree_path(name):
    p = TREES / name
    if not (p / "Makefile").exists():
        raise LabError(f"no kernel tree '{name}' (see `lab ls`)")
    return p


def default_tree(name):
    name = name or load_state().get("tree")
    if not name:
        raise LabError("no tree given and no current default (pass a tree name; `lab ls` lists them)")
    return name


def _git_identity():
    """Commits need an identity; fall back to a lab one if the user has none configured."""
    if output(["git", "config", "user.email"]):
        return []
    return ["-c", "user.name=klab", "-c", "user.email=klab@localhost"]


def apply_patches(tree, patches, src=None):
    """Apply patch files (git format-patch mbox or plain diff) as commits on the tree."""
    src = src or tree_path(tree)
    ident = _git_identity()
    for patch in patches:
        patch = Path(patch).resolve()
        if not patch.is_file():
            raise LabError(f"patch not found: {patch}")
        head = patch.read_text(errors="replace")[:200]
        if head.startswith("From "):
            run(["git", *ident, "-C", src, "am", "--3way", patch])
        else:
            run(["git", "-C", src, "apply", "--index", patch])
            run(["git", *ident, "-C", src, "commit", "-q", "-m", f"lab: apply {patch.name}"])


def tree_new(name, base, patches):
    """Create a variant of a tree as a git worktree (shares objects), optionally patched."""
    base = default_tree(base)
    base_src = tree_path(base)
    dest = TREES / name
    if dest.exists():
        raise LabError(f"tree '{name}' already exists")
    check_disk(2, "a kernel worktree")
    run(["git", "-C", base_src, "worktree", "add", "-q", "-b", name, dest, "HEAD"])
    if patches:
        apply_patches(name, patches, src=dest)
    save_state(tree=name)
    info(f"created trees/{name} from {base}  (next: lab build {name})")


def tree_info(name):
    src = TREES / name
    describe = output(["git", "-C", src, "describe", "--tags", "--always", "--dirty"])
    commit = output(["git", "-C", src, "rev-parse", "HEAD"])
    base_tag = output(["git", "-C", src, "describe", "--tags", "--abbrev=0"])
    patches = output(["git", "-C", src, "log", "--oneline", f"{base_tag}..HEAD"]) if base_tag else ""
    return {
        "name": name,
        "describe": describe,
        "commit": commit,
        "base": base_tag,
        "patches": patches.splitlines() if patches else [],
        "dirty": describe.endswith("-dirty"),
        "worktree": (src / ".git").is_file(),
    }


def list_trees():
    if not TREES.exists():
        return []
    return sorted(p.name for p in TREES.iterdir() if (p / "Makefile").exists())


def _forget_default(tree=None, build=None):
    """Clear the current default tree/build if it is the one being removed."""
    state = load_state()
    clear = {k: None for k, v in (("tree", tree), ("build", build)) if v and state.get(k) == v}
    if clear:
        save_state(**clear)
        info(f"current default {', '.join(clear)} cleared (it was {tree or build})")


def tree_rm(name):
    src = tree_path(name)
    users = [b for b in list_builds() if build_meta(b).get("tree") == name]
    if users:
        warn(f"builds still reference this tree: {', '.join(users)}")
    if (src / ".git").is_file():
        main = output(["git", "-C", src, "rev-parse", "--path-format=absolute", "--git-common-dir"])
        run(["git", "-C", Path(main).parent, "worktree", "remove", "--force", src])
        run(["git", "-C", Path(main).parent, "branch", "-q", "-D", name], check=False)
    else:
        wts = output(["git", "-C", src, "worktree", "list", "--porcelain"]).count("worktree ")
        if wts > 1:
            raise LabError(f"'{name}' has variant worktrees depending on it; remove those first")
        remove_tree(src)
    _forget_default(tree=name)
    info(f"{'would remove' if common.DRY else 'removed'} tree {name}")


# --------------------------------------------------------------------------- builds

def build_dir(name):
    return BUILDS / name


def build_meta(name):
    try:
        return json.loads((build_dir(name) / "lab-build.json").read_text())
    except (OSError, ValueError):
        return {}


def list_builds():
    if not BUILDS.exists():
        return []
    return sorted(p.name for p in BUILDS.iterdir() if (p / "lab-build.json").exists())


def default_build(name):
    name = name or load_state().get("build")
    if not name:
        raise LabError("no build given and no current default (pass a build name; `lab ls` lists them)")
    if not (build_dir(name) / "lab-build.json").exists():
        raise LabError(f"no build '{name}' (see `lab ls`)")
    return name


def fragment_path(frag):
    p = Path(frag)
    if p.suffix == ".config" and p.exists():
        return p.resolve()
    p = CONFIGS / f"{frag}.config"
    if not p.exists():
        avail = ", ".join(sorted(f.stem for f in CONFIGS.glob("*.config")))
        raise LabError(f"unknown config fragment '{frag}' (available: {avail})")
    return p


def parse_fragment(path):
    """Return {CONFIG_X: value} where value is 'n' for '# CONFIG_X is not set'."""
    opts = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        m = re.match(r"# (CONFIG_\w+) is not set", line)
        if m:
            opts[m.group(1)] = "n"
        elif line.startswith("CONFIG_") and "=" in line:
            k, v = line.split("=", 1)
            opts[k] = v
    return opts


def verify_config(out, fragments):
    """Warn about options requested by fragments that kconfig silently dropped."""
    final = parse_fragment(out / ".config")
    wanted = {}
    for f in fragments:
        wanted.update(parse_fragment(f))
    bad = [(k, v, final.get(k, "n")) for k, v in wanted.items() if final.get(k, "n") != v]
    for k, want, got in bad:
        warn(f"{k}: requested {want}, got {got} (missing dependency or removed symbol?)")
    return not bad


def config_hash(out):
    try:
        return hashlib.sha256((out / ".config").read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


# Host headers the kernel build needs: (header, apt package, why).
REQUIRED_HEADERS = [
    ("gelf.h", "libelf-dev", "objtool, which every x86_64 build runs"),
    ("openssl/ssl.h", "libssl-dev", "module signing / certificate tools"),
]


def check_build_deps(out=None):
    """Fail early with an actionable message instead of a deep C compile error."""
    missing = []
    btf = out is not None and "CONFIG_DEBUG_INFO_BTF=y" in _read(out / ".config")
    if btf and not have("pahole"):
        missing.append(("pahole", "dwarves", "BTF generation (CONFIG_DEBUG_INFO_BTF, for eBPF)"))
    for header, pkg, why in REQUIRED_HEADERS:
        probe = subprocess.run(["gcc", "-E", "-x", "c", "-", "-o", "/dev/null"],
                               input=f"#include <{header}>\n", text=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode != 0:
            missing.append((header, pkg, why))
    if missing:
        lines = [f"  {h if h == 'pahole' else f'<{h}>'} from {p}: needed by {w}"
                 for h, p, w in missing]
        pkgs = " ".join(p for _, p, _ in missing)
        raise LabError("missing build dependencies:\n" + "\n".join(lines) +
                       f"\ninstall with: sudo apt install {pkgs}   (or: make deps)")


def _read(path):
    try:
        return path.read_text()
    except OSError:
        return ""


def build(tree=None, profile="perf", fragments=(), name=None, jobs=None,
          reconfig=False, menuconfig=False, olddefconfig_only=False):
    tree = default_tree(tree)
    src = tree_path(tree)
    name = name or f"{tree}-{profile}"
    out = build_dir(name)
    jobs = jobs or os.cpu_count()

    # Validate profile/fragments first, so a typo doesn't leave an empty build dir.
    frag_paths = [fragment_path(BASE_FRAGMENT), fragment_path(profile)]
    frag_paths += [fragment_path(f) for f in fragments]
    if not common.DRY:
        out.mkdir(parents=True, exist_ok=True)
    inputs_hash = hashlib.sha256(b"".join(p.read_bytes() for p in frag_paths)).hexdigest()[:12]

    meta = build_meta(name)
    if meta and meta.get("tree") != tree:
        raise LabError(f"build '{name}' belongs to tree '{meta.get('tree')}', not '{tree}'")

    make = ["make", "-C", src, f"O={out}", "ARCH=x86_64"]
    if have("ccache"):
        make.append("CC=ccache gcc")

    if reconfig or not (out / ".config").exists() or meta.get("fragments_hash") != inputs_hash:
        info(f"configuring {name}: defconfig + kvm_guest + {', '.join(p.stem for p in frag_paths)}")
        run([*make, "defconfig"])
        run([*make, "kvm_guest.config"])
        run([src / "scripts/kconfig/merge_config.sh", "-m", "-O", out, out / ".config", *frag_paths],
            cwd=src)
        run([*make, "olddefconfig"])
        if not common.DRY:
            verify_config(out, frag_paths)

    meta = {
        "name": name,
        "tree": tree,
        "profile": profile,
        "fragments": [str(p.relative_to(CONFIGS)) if p.is_relative_to(CONFIGS) else str(p)
                      for p in frag_paths],
        "fragments_hash": inputs_hash,
    }
    if not common.DRY:
        (out / "lab-build.json").write_text(
            json.dumps({**build_meta(name), **meta}, indent=2) + "\n")
    save_state(tree=tree, build=name)

    if menuconfig:
        run([*make, "menuconfig"])
    if olddefconfig_only:
        return name

    try:
        check_build_deps(out)
    except LabError as e:
        dry_or_raise(str(e))
    if not common.DRY:
        debug_info = "CONFIG_DEBUG_INFO_NONE=y" not in (out / ".config").read_text()
        check_disk(6 if debug_info else 2, "this build")
    info(f"building {name} with -j{jobs}")
    run([*make, f"-j{jobs}"])
    # Install modules into the build dir; the VM mounts them at /lib/modules.
    modroot = out / "modroot"
    remove_tree(modroot)
    run([*make, f"INSTALL_MOD_PATH={modroot}", "INSTALL_MOD_STRIP=1", "modules_install"], quiet=False)
    # compile_commands.json for clangd/VS Code navigation of this exact config.
    run([*make, "compile_commands.json"], check=False)
    cc = out / "compile_commands.json"
    link = src / "compile_commands.json"
    if common.DRY:
        show(["ln", "-sfn", cc, link])
        return name
    if cc.exists() and not link.exists():
        link.symlink_to(cc)

    release = output(["make", "-s", "-C", src, f"O={out}", "kernelrelease"])
    info_meta = tree_info(tree)
    meta.update(kernelrelease=release, commit=info_meta["commit"], describe=info_meta["describe"],
                config_hash=config_hash(out))
    (out / "lab-build.json").write_text(json.dumps(meta, indent=2) + "\n")
    info(f"built {name}: {release}  (next: lab boot {name})")
    return name


def build_rm(name):
    out = build_dir(name)
    if not out.exists():
        raise LabError(f"no build '{name}'")
    remove_tree(out)
    _forget_default(build=name)
    info(f"{'would remove' if common.DRY else 'removed'} build {name}")


def diffconfig(a, b):
    a, b = default_build(a), default_build(b)
    tree = build_meta(b).get("tree") or build_meta(a).get("tree")
    script = tree_path(tree) / "scripts/diffconfig"
    run(["python3", script, build_dir(a) / ".config", build_dir(b) / ".config"], quiet=True)
