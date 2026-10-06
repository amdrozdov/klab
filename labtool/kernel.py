"""Kernel sources (fetch, patched variants) and builds."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import subprocess
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import common
from .common import (
    BUILDS,
    CONFIGS,
    TREES,
    LabError,
    check_disk,
    dry_or_raise,
    have,
    info,
    load_state,
    output,
    remove_tree,
    run,
    save_state,
    show,
    warn,
)

RELEASES_URL = "https://www.kernel.org/releases.json"
MAINLINE_GIT = "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"
STABLE_GIT = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"

# Applied on top of `make defconfig kvm_guest.config`, in this order.
BASE_FRAGMENT = "base"

# Merged on top of a --base-config full config (unless --raw) so the resulting
# kernel still boots the lab's QEMU VM: virtio devices, ext4 root, 9p shares.
LAB_VM_FRAGMENT = "lab-vm"

# Also merged for --base-config (unless --raw): clears distro-only signing/trusted-key
# paths that break a vanilla kernel.org build at the certs step.
DISTRO_FIXUP_FRAGMENT = "no-distro-keys"

# Merged last for `--no-debug-info`: disables DWARF/BTF to shrink the build.
NO_DEBUGINFO_FRAGMENT = "no-debuginfo"

# Downloaded / decompressed base configs are cached here (gitignored).
CONFIG_CACHE = CONFIGS / ".cache"

# Merged for `--arch pi` builds: pins what a Pi 4 needs to boot without an initramfs.
PI_FRAGMENT = "pi"


@dataclass(frozen=True)
class Arch:
    """A build target: how to invoke make and where the kernel image ends up."""

    name: str
    make_arch: str  # ARCH=
    cross: str  # CROSS_COMPILE= prefix ("" = native)
    image: str  # kernel image, relative to the build dir
    vm: bool  # can it boot in the lab's QEMU VM?


ARCHES: dict[str, Arch] = {
    "x86": Arch("x86", "x86_64", "", "arch/x86/boot/bzImage", vm=True),
    "pi": Arch("pi", "arm64", "aarch64-linux-gnu-", "arch/arm64/boot/Image.gz", vm=False),
}

# Public full-config sources for `--base-config <name>`: name -> (version, arch)
# -> URL of a single complete .config. Ubuntu is intentionally absent — it has no
# single downloadable per-version config; use `--base-config host` for it.
KNOWN_CONFIGS: dict[str, Callable[[str, str], str]] = {
    # Arch Linux ships one full x86_64 config tracking its latest packaged kernel;
    # `make olddefconfig` then adapts it to the target tree.
    "arch": lambda version, arch: (
        "https://gitlab.archlinux.org/archlinux/packaging/packages/linux/"
        "-/raw/main/config"
    ),
}


# --------------------------------------------------------------------------- fetch


def resolve_version(spec: str) -> str:
    """Turn 'stable' / 'mainline' / 'longterm' / '6.12.3' / 'v6.12'
    into a version string."""
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


def is_branch(spec: str) -> bool:
    """Branch names as listed by `lab rtree --branches` (linux-6.12.y, master, ...)."""
    return spec == "master" or spec.startswith("linux-")


def fetch(spec: str, name: str | None = None) -> str:
    if is_branch(spec):
        # A branch snapshot: the clone already has a local branch of that name.
        ref, url = spec, (MAINLINE_GIT if spec == "master" else STABLE_GIT)
        name = name or spec
    else:
        version = resolve_version(spec)
        ref = f"v{version}"
        name = name or ref
        # -rc tags only exist in Linus' tree; releases are in both,
        # stable has the .y updates.
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
    run(
        [
            "git",
            "-c",
            "advice.detachedHead=false",
            "clone",
            "--depth",
            "1",
            "--branch",
            ref,
            url,
            dest,
        ]
    )
    if not is_branch(spec) or name != spec:
        # A named branch so patch commits / variants have something to hang on.
        run(["git", "-C", dest, "switch", "-q", "-c", name])
    save_state(tree=name)
    info(f"done: trees/{name}  (next: lab build {name})")
    return name


# ------------------------------------------------------------------------- remote listing

TAG_RE = re.compile(r"^v(\d+(?:\.\d+)+)(?:-rc(\d+))?$")


def _series_of(nums: tuple[int, ...]) -> str:
    """'6.12.3' -> '6.12'; the 2.6 era used a third component for the series."""
    return ".".join(map(str, nums[:3] if nums[:2] == (2, 6) else nums[:2]))


def _version_key(v: dict[str, Any]) -> tuple[tuple[int, ...], int]:
    return (v["nums"] + (0,) * (4 - len(v["nums"])), v["rc"] or 10**6)


def remote_versions() -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
    """Releases, -rc tags and branches on kernel.org (stable repo mirrors mainline tags),
    annotated with each series' status from releases.json."""
    refs = output(["git", "ls-remote", "--refs", STABLE_GIT])
    if not refs:
        raise LabError(f"could not list refs of {STABLE_GIT} (network?)")
    tags: list[dict[str, Any]] = []
    branches: list[str] = []
    for line in refs.splitlines():
        ref = line.split("\t", 1)[1]
        if ref.startswith("refs/heads/"):
            branches.append(ref[len("refs/heads/") :])
        elif ref.startswith("refs/tags/"):
            m = TAG_RE.match(ref[len("refs/tags/") :])
            if m:
                nums = tuple(int(x) for x in m.group(1).split("."))
                rc = int(m.group(2)) if m.group(2) else None
                tags.append(
                    {
                        "version": ref[len("refs/tags/v") :],
                        "nums": nums,
                        "rc": rc,
                        "series": _series_of(nums),
                    }
                )
    tags.sort(key=_version_key, reverse=True)

    status: dict[str, str] = {}
    try:
        with urllib.request.urlopen(RELEASES_URL, timeout=20) as r:
            for rel in json.load(r)["releases"]:
                if rel["moniker"] == "linux-next":
                    continue
                nums = tuple(
                    int(x) for x in re.findall(r"\d+", rel["version"].split("-")[0])
                )
                status[_series_of(nums)] = "EOL" if rel.get("iseol") else rel["moniker"]
    except (OSError, ValueError) as e:
        warn(f"could not read {RELEASES_URL}: {e}")
    return tags, branches, status


def local_versions() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """series -> local tree names, and version -> local tree names."""
    by_series: dict[str, list[str]] = {}
    by_version: dict[str, list[str]] = {}
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


def tree_path(name: str) -> Path:
    p = TREES / name
    if not (p / "Makefile").exists():
        raise LabError(f"no kernel tree '{name}' (see `lab ls`)")
    return p


def default_tree(name: str | None) -> str:
    name = name or load_state().get("tree")
    if not name:
        raise LabError(
            "no tree given and no current default (pass a tree name; `lab ls` lists them)"
        )
    return name


def _git_identity() -> list[str]:
    """Commits need an identity; fall back to a lab one if the user
    has none configured."""
    if output(["git", "config", "user.email"]):
        return []
    return ["-c", "user.name=klab", "-c", "user.email=klab@localhost"]


def apply_patches(tree: str, patches: Iterable[str], src: Path | None = None) -> None:
    """Apply patch files (git format-patch mbox or plain diff) as commits on the tree."""
    src = src or tree_path(tree)
    ident = _git_identity()
    for patch in patches:
        path = Path(patch).resolve()
        if not path.is_file():
            raise LabError(f"patch not found: {path}")
        head = path.read_text(errors="replace")[:200]
        if head.startswith("From "):
            run(["git", *ident, "-C", src, "am", "--3way", path])
        else:
            run(["git", "-C", src, "apply", "--index", path])
            run(
                [
                    "git",
                    *ident,
                    "-C",
                    src,
                    "commit",
                    "-q",
                    "-m",
                    f"lab: apply {path.name}",
                ]
            )


def tree_new(name: str, base: str | None, patches: Sequence[str]) -> None:
    """Create a variant of a tree as a git worktree (shares objects),
    optionally patched."""
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


def tree_info(name: str) -> dict[str, Any]:
    src = TREES / name
    describe = output(["git", "-C", src, "describe", "--tags", "--always", "--dirty"])
    commit = output(["git", "-C", src, "rev-parse", "HEAD"])
    base_tag = output(["git", "-C", src, "describe", "--tags", "--abbrev=0"])
    patches = (
        output(["git", "-C", src, "log", "--oneline", f"{base_tag}..HEAD"])
        if base_tag
        else ""
    )
    return {
        "name": name,
        "describe": describe,
        "commit": commit,
        "base": base_tag,
        "patches": patches.splitlines() if patches else [],
        "dirty": describe.endswith("-dirty"),
        "worktree": (src / ".git").is_file(),
    }


def list_trees() -> list[str]:
    if not TREES.exists():
        return []
    return sorted(p.name for p in TREES.iterdir() if (p / "Makefile").exists())


def _forget_default(tree: str | None = None, build: str | None = None) -> None:
    """Clear the current default tree/build if it is the one being removed."""
    state = load_state()
    clear = {
        k: None
        for k, v in (("tree", tree), ("build", build), ("pi_build", build))
        if v and state.get(k) == v
    }
    if clear:
        save_state(**clear)
        info(f"current default {', '.join(clear)} cleared (it was {tree or build})")


def tree_rm(name: str) -> None:
    src = tree_path(name)
    users = [b for b in list_builds() if build_meta(b).get("tree") == name]
    if users:
        warn(f"builds still reference this tree: {', '.join(users)}")
    if (src / ".git").is_file():
        main = output(
            ["git", "-C", src, "rev-parse", "--path-format=absolute", "--git-common-dir"]
        )
        run(["git", "-C", Path(main).parent, "worktree", "remove", "--force", src])
        run(["git", "-C", Path(main).parent, "branch", "-q", "-D", name], check=False)
    else:
        wts = output(["git", "-C", src, "worktree", "list", "--porcelain"]).count(
            "worktree "
        )
        if wts > 1:
            raise LabError(
                f"'{name}' has variant worktrees depending on it; remove those first"
            )
        remove_tree(src)
    _forget_default(tree=name)
    info(f"{'would remove' if common.DRY else 'removed'} tree {name}")


# --------------------------------------------------------------------------- builds


def build_dir(name: str) -> Path:
    return BUILDS / name


def build_meta(name: str) -> dict[str, Any]:
    try:
        return json.loads((build_dir(name) / "lab-build.json").read_text())
    except (OSError, ValueError):
        return {}


def build_arch(name: str) -> str:
    """'x86' or 'pi' (builds made before --arch existed are x86)."""
    return str(build_meta(name).get("arch") or "x86")


def build_image(name: str) -> Path:
    return build_dir(name) / ARCHES[build_arch(name)].image


def require_x86(name: str) -> None:
    """The lab VM is x86: refuse builds made for other targets, with a pointer."""
    if build_arch(name) != "x86":
        raise LabError(
            f"build '{name}' is for the Raspberry Pi (arm64), not the lab VM; "
            f"install it on the Pi with `lab pi kernel install {name}`"
        )


def list_builds() -> list[str]:
    if not BUILDS.exists():
        return []
    return sorted(p.name for p in BUILDS.iterdir() if (p / "lab-build.json").exists())


def default_build(name: str | None) -> str:
    name = name or load_state().get("build")
    if not name:
        raise LabError(
            "no build given and no current default "
            "(pass a build name; `lab ls` lists them)"
        )
    if not (build_dir(name) / "lab-build.json").exists():
        raise LabError(f"no build '{name}' (see `lab ls`)")
    return name


def fragment_path(frag: str) -> Path:
    p = Path(frag)
    if p.suffix == ".config" and p.exists():
        return p.resolve()
    p = CONFIGS / f"{frag}.config"
    if not p.exists():
        avail = ", ".join(sorted(f.stem for f in CONFIGS.glob("*.config")))
        raise LabError(f"unknown config fragment '{frag}' (available: {avail})")
    return p


def parse_fragment(path: Path) -> dict[str, str]:
    """Return {CONFIG_X: value} where value is 'n' for '# CONFIG_X is not set'."""
    opts: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        m = re.match(r"# (CONFIG_\w+) is not set", line)
        if m:
            opts[m.group(1)] = "n"
        elif line.startswith("CONFIG_") and "=" in line:
            k, v = line.split("=", 1)
            opts[k] = v
    return opts


def verify_config(out: Path, fragments: Iterable[Path]) -> bool:
    """Warn about options requested by fragments that kconfig silently dropped."""
    final = parse_fragment(out / ".config")
    wanted: dict[str, str] = {}
    for f in fragments:
        wanted.update(parse_fragment(f))
    bad = [(k, v, final.get(k, "n")) for k, v in wanted.items() if final.get(k, "n") != v]
    for k, want, got in bad:
        warn(f"{k}: requested {want}, got {got} (missing dependency or removed symbol?)")
    return not bad


def config_hash(out: Path) -> str:
    try:
        return hashlib.sha256((out / ".config").read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


# Host headers the kernel build needs: (header, apt package, why).
REQUIRED_HEADERS = [
    ("gelf.h", "libelf-dev", "objtool, which every x86_64 build runs"),
    ("openssl/ssl.h", "libssl-dev", "module signing / certificate tools"),
]


def check_build_deps(out: Path | None = None) -> None:
    """Fail early with an actionable message instead of a deep C compile error."""
    missing: list[tuple[str, str, str]] = []
    btf = out is not None and "CONFIG_DEBUG_INFO_BTF=y" in _read(out / ".config")
    if btf and not have("pahole"):
        missing.append(
            ("pahole", "dwarves", "BTF generation (CONFIG_DEBUG_INFO_BTF, for eBPF)")
        )
    for header, pkg, why in REQUIRED_HEADERS:
        probe = subprocess.run(
            ["gcc", "-E", "-x", "c", "-", "-o", "/dev/null"],
            input=f"#include <{header}>\n",
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode != 0:
            missing.append((header, pkg, why))
    if missing:
        lines = [
            f"  {h if h == 'pahole' else f'<{h}>'} from {p}: needed by {w}"
            for h, p, w in missing
        ]
        pkgs = " ".join(p for _, p, _ in missing)
        raise LabError(
            "missing build dependencies:\n"
            + "\n".join(lines)
            + f"\ninstall with: sudo apt install {pkgs}   (or: make deps)"
        )


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _safe_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


# --------------------------------------------------------------------------- base configs


def _is_gzip(path: Path) -> bool:
    if path.suffix == ".gz":
        return True
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"\x1f\x8b"
    except OSError:
        return False


def _gunzip(src: Path, dest: Path) -> Path:
    if common.DRY:
        print(f"gunzip -c {src} > {dest}", flush=True)
        return dest
    dest.write_bytes(gzip.decompress(src.read_bytes()))
    return dest


def _fetch(url: str, dest: Path) -> Path:
    """Download a .config (gunzipping if the server sent gzip) into the cache."""
    show(["curl", "-fsSL", "-o", dest, url])
    if common.DRY:
        return dest
    info(f"downloading kernel config from {url}")
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = r.read()
    except OSError as e:
        raise LabError(f"could not download base config from {url}: {e}") from e
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    dest.write_bytes(data)
    return dest


def resolve_base_config(source: str, version: str, arch: str) -> Path:
    """Resolve a --base-config SOURCE to a local full .config.

    SOURCE is 'host' (the running kernel's config), 'pi' (the config of the kernel
    running on the saved Raspberry Pi), a known distro name (see KNOWN_CONFIGS), a URL,
    or a local path (optionally gzipped)."""
    if not common.DRY:
        CONFIG_CACHE.mkdir(parents=True, exist_ok=True)

    if source == "host":
        plain = Path(f"/boot/config-{os.uname().release}")
        if plain.exists():
            return plain
        gz = Path("/proc/config.gz")
        if gz.exists():
            return _gunzip(gz, CONFIG_CACHE / "host.config")
        dry_or_raise(
            f"no host config: neither {plain} nor /proc/config.gz exists "
            "(pass a path or URL to --base-config instead)"
        )
        return plain

    if source == "pi":
        from . import pi  # local import: pi.py is built on top of the kernel helpers

        return pi.fetch_kernel_config()

    if source in KNOWN_CONFIGS:
        url = KNOWN_CONFIGS[source](version, arch)
        return _fetch(url, CONFIG_CACHE / f"{source}-{version}.config")

    if source.startswith(("http://", "https://")):
        name = (source.rsplit("/", 1)[-1] or "download").removesuffix(".gz")
        if not name.endswith(".config"):
            name += ".config"
        return _fetch(source, CONFIG_CACHE / name)

    p = Path(source)
    if not p.exists():
        dry_or_raise(f"base config not found: {source}")
        return p
    if _is_gzip(p):
        return _gunzip(p, CONFIG_CACHE / f"{p.stem}.config")
    return p.resolve()


LOCALVERSION_MAX = 64  # the kernel release string (uname -r) is limited to 64 bytes


def localversion(name: str) -> str:
    """The CONFIG_LOCALVERSION that makes `uname -r` on the target name the build."""
    return "-klab-" + re.sub(r"[^A-Za-z0-9._+-]", "-", name)


def localversion_fragment(out: Path, name: str, version: str) -> Path:
    """Write out/lab-localversion.config (CONFIG_LOCALVERSION="-klab-<build>")."""
    lv = localversion(name)
    if len(version) + len(lv) > LOCALVERSION_MAX - 2:
        raise LabError(
            f"build name '{name}' is too long: the kernel release '{version}{lv}' "
            f"must stay under {LOCALVERSION_MAX} characters (use -n to shorten it)"
        )
    path = out / "lab-localversion.config"
    if not common.DRY:
        path.write_text(f'CONFIG_LOCALVERSION="{lv}"\n')
    return path


def build(
    tree: str | None = None,
    profile: str = "perf",
    fragments: Sequence[str] = (),
    name: str | None = None,
    jobs: int | None = None,
    reconfig: bool = False,
    menuconfig: bool = False,
    olddefconfig_only: bool = False,
    base_config: str | None = None,
    raw: bool = False,
    no_debug_info: bool = False,
    arch: str = "x86",
) -> str:
    tree = default_tree(tree)
    src = tree_path(tree)
    spec = ARCHES[arch]
    pi = arch == "pi"
    if base_config is None:
        base_config = (
            "pi" if pi else "defconfig"
        )  # a Pi build starts from the Pi's config
    if pi:
        if raw:
            raise LabError(
                "--raw is for the x86 lab VM overlay; it has no meaning for --arch pi"
            )
        if base_config == "host" or base_config in KNOWN_CONFIGS:
            raise LabError(
                f"--base-config {base_config} is an x86 config; for --arch pi use 'pi' "
                "(the config of the kernel running on your Pi), 'defconfig', a path "
                "or a URL"
            )
        if spec.cross and not have(f"{spec.cross}gcc"):
            dry_or_raise(
                f"{spec.cross}gcc not found (sudo apt install gcc-aarch64-linux-gnu)"
            )
    full = base_config != "defconfig"
    if name is None:
        if pi:
            name = f"{tree}-pi"
        elif not full:
            name = f"{tree}-{profile}"
        elif base_config == "host" or base_config in KNOWN_CONFIGS:
            name = f"{tree}-{base_config}"
        else:
            name = f"{tree}-custom"
    out = build_dir(name)
    jobs = jobs or os.cpu_count()

    # Validate profile/fragments first, so a typo doesn't leave an empty build dir.
    # defconfig: build up from defconfig + kvm_guest + base fragment + profile.
    # full base config: start from it, overlay the VM-boot essentials (unless --raw)
    # and the profile; the kvm_guest/base fragments are NOT forced on.
    base_cfg = resolve_base_config(base_config, tree, spec.make_arch) if full else None
    if not common.DRY:
        out.mkdir(parents=True, exist_ok=True)
    if pi:
        # The stock Pi config (or arm64 defconfig) + pins for booting without an
        # initramfs + the profile + a LOCALVERSION that names this build.
        version = output(["make", "-s", "-C", src, "kernelversion"]) or "?"
        frag_paths = [
            fragment_path(PI_FRAGMENT),
            fragment_path(profile),
            localversion_fragment(out, name, version),
        ]
    elif not full:
        frag_paths = [fragment_path(BASE_FRAGMENT), fragment_path(profile)]
    elif raw:
        frag_paths = []
    else:
        frag_paths = [
            fragment_path(LAB_VM_FRAGMENT),
            fragment_path(DISTRO_FIXUP_FRAGMENT),
            fragment_path(profile),
        ]
    frag_paths += [fragment_path(f) for f in fragments]
    # Applied last so it overrides debug-info settings from the base config / profile.
    if no_debug_info:
        frag_paths.append(fragment_path(NO_DEBUGINFO_FRAGMENT))
    if not common.DRY:
        out.mkdir(parents=True, exist_ok=True)
    hash_srcs = ([base_cfg] if base_cfg else []) + frag_paths
    inputs_hash = hashlib.sha256(b"".join(_safe_bytes(p) for p in hash_srcs)).hexdigest()[
        :12
    ]

    meta = build_meta(name)
    if meta and meta.get("tree") != tree:
        raise LabError(
            f"build '{name}' belongs to tree '{meta.get('tree')}', not '{tree}'"
        )

    make = ["make", "-C", src, f"O={out}", f"ARCH={spec.make_arch}"]
    if spec.cross:
        make.append(f"CROSS_COMPILE={spec.cross}")
    if have("ccache"):
        make.append(f"CC=ccache {spec.cross}gcc")

    if (
        reconfig
        or not (out / ".config").exists()
        or meta.get("fragments_hash") != inputs_hash
    ):
        overlay = ", ".join(p.stem for p in frag_paths)
        if base_cfg is None:
            info(
                f"configuring {name}: defconfig"
                + ("" if pi else " + kvm_guest")
                + f" + {overlay}"
            )
            run([*make, "defconfig"])
            if not pi:
                run([*make, "kvm_guest.config"])
        else:
            info(
                f"configuring {name}: base config '{base_config}'"
                + (f" + {overlay}" if overlay else " (raw)")
            )
            run(["cp", base_cfg, out / ".config"])
            # Adapt the (possibly different-version) config to this tree first.
            run([*make, "olddefconfig"])
        if frag_paths:
            run(
                [
                    src / "scripts/kconfig/merge_config.sh",
                    "-m",
                    "-O",
                    out,
                    out / ".config",
                    *frag_paths,
                ],
                cwd=src,
            )
        run([*make, "olddefconfig"])
        if not common.DRY:
            verify_config(out, frag_paths)

    meta = {
        "name": name,
        "tree": tree,
        "profile": profile,
        "fragments": [
            str(p.relative_to(CONFIGS)) if p.is_relative_to(CONFIGS) else str(p)
            for p in frag_paths
        ],
        "fragments_hash": inputs_hash,
        "arch": arch,
        "image": spec.image,
    }
    if pi:
        meta["localversion"] = localversion(name)
    if base_cfg is not None:
        meta["base_config"] = {
            "source": base_config,
            "path": str(base_cfg),
            "sha256": hashlib.sha256(_safe_bytes(base_cfg)).hexdigest(),
            "raw": raw,
        }
    if not common.DRY:
        (out / "lab-build.json").write_text(
            json.dumps({**build_meta(name), **meta}, indent=2) + "\n"
        )
    if pi:  # keep `lab boot` / `lab bench` pointing at the last x86 build
        save_state(tree=tree, pi_build=name)
    else:
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
        # A full distro config builds thousands of modules; much bigger than defconfig.
        # Debug info (DWARF/BTF) roughly doubles the on-disk footprint again.
        heavy, light = (25, 12) if full else (6, 2)
        check_disk(heavy if debug_info else light, "this build")
    info(f"building {name} with -j{jobs}")
    if pi:
        # Image.gz is what the Pi firmware loads; dtbs are not part of `make all` here.
        run([*make, f"-j{jobs}", "Image.gz", "modules"])
        run([*make, f"-j{jobs}", "dtbs"])
    else:
        run([*make, f"-j{jobs}"])
    # Install modules into the build dir; the VM mounts them at /lib/modules.
    modroot = out / "modroot"
    remove_tree(modroot)
    run(
        [*make, f"INSTALL_MOD_PATH={modroot}", "INSTALL_MOD_STRIP=1", "modules_install"],
        quiet=False,
    )
    if pi:
        dtbroot = out / "dtbroot"
        remove_tree(dtbroot)
        run([*make, f"INSTALL_DTBS_PATH={dtbroot}", "dtbs_install"])
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
    meta.update(
        kernelrelease=release,
        commit=info_meta["commit"],
        describe=info_meta["describe"],
        config_hash=config_hash(out),
    )
    (out / "lab-build.json").write_text(json.dumps(meta, indent=2) + "\n")
    nxt = f"lab pi kernel install {name}" if pi else f"lab boot {name}"
    info(f"built {name}: {release}  (next: {nxt})")
    return name


def build_rm(name: str) -> None:
    out = build_dir(name)
    if not out.exists():
        raise LabError(f"no build '{name}'")
    remove_tree(out)
    _forget_default(build=name)
    info(f"{'would remove' if common.DRY else 'removed'} build {name}")


def diffconfig(a: str, b: str) -> None:
    a, b = default_build(a), default_build(b)
    tree = build_meta(b).get("tree") or build_meta(a).get("tree")
    if not tree:
        raise LabError(f"neither build '{a}' nor '{b}' records its tree")
    script = tree_path(tree) / "scripts/diffconfig"
    run(
        ["python3", script, build_dir(a) / ".config", build_dir(b) / ".config"],
        quiet=True,
    )
