"""`lab pi stress`: run stress-ng on the Pi and save the result under runs/.

The result is a normal benchmark run (runs/<id>/result.json), so `lab runs`, `lab show`
and `lab compare` work on it: run it on the stock kernel and on your own build, then
`lab compare latest:stock latest:<build>`. The raw stress-ng YAML of every run is kept
in runs/<id>/raw/.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Any

from . import bench, common, kernel, pi
from .common import BENCHMARKS, RUNS, LabError, info, show

PROFILES = BENCHMARKS / "pi"
DEFAULT_PROFILE = "quick"
GROUP = "stress-ng"  # the benchmark name in result.json
UNIT = "bogo-ops/s"
METRIC_KEY = "bogo-ops-per-second-real-time"
# How long the Pi lets a stressor run past its own --timeout before killing it.
GRACE = 60

STRESS_NG_PRESENT = "command -v stress-ng >/dev/null && echo yes || echo no"
# What the run records about the Pi before and after (read-only).
FACTS = r"""echo "kernel=$(uname -r)"
echo "hostname=$(uname -n)"
echo "os=$(. /etc/os-release; echo $ID)"
echo "model=$(tr -d '\0' </proc/device-tree/model 2>/dev/null)"
echo "cpus=$(nproc)"
echo "mem_mb=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)"
echo "governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
echo "temp_mc=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)"
echo "load=$(cut -d' ' -f1-3 /proc/loadavg)"
echo "stress_ng=$(stress-ng --version 2>&1 | head -1)"
cat /boot/firmware/klab/BUILD /boot/klab/BUILD 2>/dev/null || true
"""
END_FACTS = r"""echo "temp_mc=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)"
echo "load=$(cut -d' ' -f1-3 /proc/loadavg)"
"""


@dataclass
class Stressor:
    name: str  # stress-ng option name: cpu, switch, vm, ...
    workers: int | None = None  # None = one per CPU of the Pi
    options: list[str] = field(default_factory=list)


def parse_profile(text: str) -> list[Stressor]:
    """'name[:workers] [stress-ng options]' per line; '#' starts a comment."""
    out: list[Stressor] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        head, *options = shlex.split(line)
        name, _, count = head.partition(":")
        if not name.isidentifier() or (count and not count.isdigit()):
            raise LabError(f"bad stressor '{head}' (expected name or name:workers)")
        if any(s.name == name for s in out):
            raise LabError(f"stressor '{name}' listed twice; results are keyed by name")
        out.append(Stressor(name, int(count) if count else None, options))
    if not out:
        raise LabError("the profile lists no stressors")
    return out


def list_profiles() -> dict[str, str]:
    """profile name -> first comment line (its description)."""
    found: dict[str, str] = {}
    for p in sorted(PROFILES.glob("*.stress")):
        first = p.read_text().splitlines()[0] if p.stat().st_size else ""
        found[p.stem] = first.lstrip("# ").strip()
    return found


def load_profile(name: str) -> list[Stressor]:
    path = PROFILES / f"{name}.stress"
    if not path.is_file():
        raise LabError(
            f"unknown profile '{name}' "
            f"(available: {', '.join(list_profiles()) or 'none'})"
        )
    return parse_profile(path.read_text())


def stressor_command(s: Stressor, workers: int | str, timeout: int) -> str:
    """The shell command that runs one stressor on the Pi and prints its YAML."""
    args = [
        "stress-ng",
        f"--{s.name}",
        str(s.workers or workers),
        *s.options,
        "--timeout",
        f"{timeout}s",
        "--oom-avoid",  # never let a memory stressor start the OOM killer
        "--metrics-brief",
        "--yaml",
    ]
    cmd = " ".join(shlex.quote(a) for a in args)
    return (
        f'f=$(mktemp) || exit 1; timeout {timeout + GRACE} {cmd} "$f" >/dev/null '
        '2>"$f.err"; rc=$?; cat "$f"; [ "$rc" -eq 0 ] || cat "$f.err" >&2; '
        'rm -f "$f" "$f.err"; exit "$rc"'
    )


def parse_metrics(yaml_text: str) -> dict[str, dict[str, float]]:
    """stressor -> {metric: value} from the `metrics:` list of stress-ng's YAML."""
    metrics: dict[str, dict[str, float]] = {}
    current: dict[str, float] | None = None
    in_metrics = False
    for line in yaml_text.splitlines():
        if not line.strip():
            continue
        if not line.startswith((" ", "-")):
            in_metrics = line.strip() == "metrics:"
            continue
        if not in_metrics:
            continue
        item = line.strip().removeprefix("- ")
        key, _, value = item.partition(":")
        if key == "stressor":
            current = metrics.setdefault(value.strip(), {})
        elif current is not None:
            with contextlib.suppress(ValueError):  # skip non-numeric entries
                current[key] = float(value)
    return metrics


def metric_of(yaml_text: str, stressor: str) -> float:
    found = parse_metrics(yaml_text)
    if stressor not in found or METRIC_KEY not in found[stressor]:
        raise LabError(f"stress-ng printed no '{METRIC_KEY}' for '{stressor}'")
    return found[stressor][METRIC_KEY]


def stress_ng_version(text: str) -> str:
    """'0.19.02' from `stress-ng, version 0.19.02 (gcc ..., Linux 6.18.55-klab-x)`."""
    m = re.search(r"version (\S+)", text)
    return m.group(1) if m else ""


def running_build(facts: dict[str, str]) -> str:
    """The klab build the Pi is running, or 'stock' for any other kernel."""
    if facts.get("build") and facts.get("release") == facts.get("kernel"):
        return facts["build"]
    return "stock"


def _celsius(millidegrees: str | None) -> float | None:
    return round(int(millidegrees) / 1000, 1) if millidegrees else None


def build_result(
    run_id: str,
    label: str | None,
    profile: str,
    repeat: int,
    timeout: int,
    workers: int,
    facts: dict[str, str],
    end: dict[str, str],
    values: dict[str, list[float]],
) -> dict[str, Any]:
    """A result.json that `lab runs`, `lab show` and `lab compare` understand."""
    build = running_build(facts)
    release = facts.get("kernel", "?")
    meta = kernel.build_meta(build) if build != "stock" else {}
    if meta:
        describe, prof = meta.get("describe"), meta.get("profile")
        config_hash = meta.get("config_hash")
    else:
        describe, prof = f"rpi-os {release}", "stock"
        cached = kernel.CONFIG_CACHE / f"pi-{release}.config"
        data = cached.read_bytes() if cached.is_file() else b""
        config_hash = hashlib.sha256(data).hexdigest()[:12] if data else ""
    cpus = int(facts.get("cpus") or workers)
    return {
        "id": run_id,
        "label": label,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "target": "pi",
        "build": build,
        "tree": meta.get("tree"),
        "profile": prof,
        "commit": meta.get("commit"),
        "describe": describe,
        "config_hash": config_hash,
        "guest": {"kernel_release": release, "nproc": str(cpus)},
        "vm": {
            "cpus": cpus,
            "mem": f"{facts.get('mem_mb', '?')}M",
            "pin": None,
            "append": "",
        },
        "params": {
            "repeat": repeat,
            "warmup": 0,
            "boots": 1,
            "profile": profile,
            "timeout": timeout,
            "workers": workers,
        },
        "telemetry": False,
        "os": "arch" if facts.get("os") in ("arch", "archarm") else "deb",
        "stress_ng": facts.get("stress_ng", ""),
        "stress_ng_version": stress_ng_version(facts.get("stress_ng", "")),
        "host": {
            "hostname": facts.get("hostname", ""),
            "cpu": f"{facts.get('model', 'Raspberry Pi')} ({cpus} cores)",
            "host_kernel": release,
            "governor": facts.get("governor", ""),
            "no_turbo": "",
            "smt": "",
            "loadavg": facts.get("load", ""),
            "temp_start_c": _celsius(facts.get("temp_mc")),
            "temp_end_c": _celsius(end.get("temp_mc")),
        },
        "results": {
            GROUP: {
                name: {"unit": UNIT, "better": "higher", "values": vals}
                for name, vals in values.items()
            }
        },
    }


def stress(
    profile: str = DEFAULT_PROFILE,
    repeat: int = 3,
    timeout: int = 20,
    label: str | None = None,
    workers: int | None = None,
) -> str | None:
    """Run a stress-ng profile on the saved Pi and save the result in runs/."""
    stressors = load_profile(profile)
    if repeat < 1 or timeout < 1 or (workers is not None and workers < 1):
        raise LabError("--repeat, --timeout and --workers must be at least 1")
    conn = pi.connection()
    os_ = pi.detect_os(conn)
    if common.DRY:
        info(f"{profile}: {len(stressors)} stressors x{repeat}, {timeout}s each")
        show(conn.argv([STRESS_NG_PRESENT]))
        show(conn.argv([os_.pkg_install]))
        for s in stressors:
            show(conn.argv([stressor_command(s, workers or "<cpus>", timeout)]))
        return None

    if pi.remote(conn, STRESS_NG_PRESENT).stdout.strip() != "yes":
        info(f"installing stress-ng on the Pi ({os_.title})")
        pi.remote(conn, os_.pkg_install)
    facts = pi.parse_kv(pi.remote(conn, FACTS).stdout)
    nworkers = workers or int(facts.get("cpus") or 1)
    build = running_build(facts)
    run_id = (
        time.strftime("%Y%m%d-%H%M%S") + f"-pi-{build}" + (f"-{label}" if label else "")
    )
    rundir = RUNS / run_id
    (rundir / "raw").mkdir(parents=True)
    total = repeat * len(stressors)
    info(
        f"run {run_id}: {profile} x{repeat}, {timeout}s per stressor, "
        f"{nworkers} workers, kernel {facts.get('kernel')}"
    )

    values: dict[str, list[float]] = {s.name: [] for s in stressors}
    done = 0
    try:
        for rep in range(repeat):  # all stressors per repetition: drift spreads evenly
            for s in stressors:
                done += 1
                info(f"[{done}/{total}] {s.name}  (repeat {rep + 1}/{repeat})")
                out = pi.remote(conn, stressor_command(s, nworkers, timeout)).stdout
                (rundir / "raw" / f"{rep}-{s.name}.yaml").write_text(out)
                values[s.name].append(metric_of(out, s.name))
    except LabError as e:
        raise LabError(f"{e}\n(partial output kept in runs/{run_id}/raw)") from e

    end = pi.parse_kv(pi.remote(conn, END_FACTS).stdout)
    result = build_result(
        run_id, label, profile, repeat, timeout, nworkers, facts, end, values
    )
    (rundir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print()
    print(bench.summary_table(result))
    info(
        f"saved runs/{run_id}/result.json  "
        f"(compare: lab compare latest:stock latest:{build})"
    )
    bench._warn_noise(result["host"])
    return run_id
