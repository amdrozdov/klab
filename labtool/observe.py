"""Telemetry: the monitoring stack (monitoring/docker-compose.yml), per-VM scrape
targets, Grafana annotations and export of benchmark results to VictoriaMetrics.

Telemetry is on automatically whenever the stack is running (`lab observe up`);
nothing here may break a benchmark, so all HTTP errors degrade to warnings."""

from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import common
from .common import ROOT, LabError, info, run, warn

MONITORING = ROOT / "monitoring"
COMPOSE_FILE = MONITORING / "docker-compose.yml"
TARGETS = MONITORING / "targets"
VM_URL = "http://127.0.0.1:8428"
GRAFANA_URL = "http://127.0.0.1:3000"
GUEST_NODE_PORT = 9100  # prometheus-node-exporter in the guest
GUEST_EBPF_PORT = 9435  # ebpf_exporter in the guest


# --------------------------------------------------------------------------- http


def _request(
    method: str, url: str, body: Any = None, data: bytes | None = None, timeout: float = 3
) -> Any:
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw and raw[:1] in (b"{", b"[") else raw


def available() -> bool:
    """True when VictoriaMetrics answers, i.e. the stack is up."""
    try:
        _request("GET", f"{VM_URL}/health", timeout=0.5)
        return True
    except (OSError, urllib.error.URLError):
        return False


def _wait_http(url: str, what: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _request("GET", url, timeout=2)
            return
        except (OSError, urllib.error.URLError):
            time.sleep(1)
    raise LabError(
        f"{what} did not come up at {url} (see `docker compose -f {COMPOSE_FILE} logs`)"
    )


# --------------------------------------------------------------------------- stack


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["docker", "compose", "-f", COMPOSE_FILE, *args], check=check)


def up() -> None:
    from . import vm  # local import: vm imports this module

    if not common.have("docker"):
        raise LabError("docker is required for the telemetry stack")
    state = vm.rootfs_state()
    if state != "current":
        info(f"guest rootfs is {state}; (re)building it so it has the telemetry agents")
        vm.rootfs(force=True)
    info("starting telemetry stack (VictoriaMetrics, Grafana, host exporters)")
    compose("up", "-d", "--remove-orphans")
    if common.DRY:
        return
    TARGETS.mkdir(exist_ok=True)
    cleanup_stale_targets()
    _wait_http(f"{VM_URL}/health", "VictoriaMetrics")
    _wait_http(f"{GRAFANA_URL}/api/health", "Grafana")
    from . import boottrace

    if boottrace.tempo_available(wait=90):
        boottrace.resend(quiet=True)  # traces a Tempo restart lost, or skipped earlier
    else:
        warn(
            "Tempo did not become ready; boot waterfalls unavailable "
            "(docker compose -f monitoring/docker-compose.yml logs tempo)"
        )
    info(f"Grafana:          {GRAFANA_URL}  (dashboards in folder 'klab')")
    info(f"VictoriaMetrics:  {VM_URL}/vmui")
    info(
        "telemetry is now on for `lab bench run` and `lab boot` (opt out: --no-telemetry)"
    )


def down(wipe: bool = False) -> None:
    info(
        "stopping telemetry stack" + (" and deleting all stored metrics" if wipe else "")
    )
    compose("down", *(["-v"] if wipe else []))


def status() -> None:
    compose("ps", check=False)
    print(
        f"\nVictoriaMetrics: {'up' if available() else 'down'}   Grafana: {GRAFANA_URL}"
    )
    sessions = (
        sorted(p.name for p in TARGETS.glob("*-node.json")) if TARGETS.exists() else []
    )
    print(f"active VM sessions: {len(sessions)}")
    for s in sessions:
        print(f"  {s[: -len('-node.json')]}")


def cleanup_stale_targets() -> None:
    """Remove target files of lab processes that no longer exist (e.g. killed runs)."""
    for f in TARGETS.glob("*.json"):
        try:
            pid = int(f.name.rsplit("-", 2)[-2])
        except (ValueError, IndexError):
            continue
        if not os.path.exists(f"/proc/{pid}"):
            f.unlink(missing_ok=True)


def wanted(disabled: bool = False) -> bool:
    """Decide whether this VM run gets telemetry."""
    return not disabled and available()


def preflight(build: str) -> None:
    """Warn about things that would leave dashboards empty."""
    from . import vm
    from .kernel import build_dir

    state = vm.rootfs_state()
    if state != "current":
        warn(
            f"guest rootfs is {state}: telemetry agents may be missing "
            "(run `make observe` or `lab rootfs --force`)"
        )
    try:
        config = (build_dir(build) / ".config").read_text()
    except OSError:
        config = ""
    if "CONFIG_DEBUG_INFO_BTF=y" not in config:
        warn(
            f"build '{build}' has no BTF: eBPF metrics will be empty "
            "(rebuild it with `lab build`; BTF is part of configs/base.config)"
        )


# --------------------------------------------------------------------------- session


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _esc(v: object) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _labels(d: Mapping[str, object]) -> str:
    return "{" + ",".join(f'{k}="{_esc(v)}"' for k, v in d.items()) + "}"


class Session:
    """Telemetry for one VM boot: port forwards, scrape targets, annotations, results."""

    def __init__(self, build: str, run_id: str, kind: str) -> None:
        self.build, self.run_id, self.kind = build, run_id, kind
        self.node_port, self.ebpf_port = _free_port(), _free_port()
        self.name = f"{run_id}-{os.getpid()}"
        self.files: list[Path] = []
        self.session_ann: int | None = None
        self.start_ms: int = 0
        self.iter_start: dict[tuple[str, str], int] = {}  # (bench, iter) -> ms
        self.iter_end: dict[tuple[str, str], int] = {}  # (bench, iter) -> ms
        self._warned = False

    # qemu / kernel command line pieces
    @property
    def hostfwd(self) -> list[str]:
        return [
            f"hostfwd=tcp:127.0.0.1:{self.node_port}-:{GUEST_NODE_PORT}",
            f"hostfwd=tcp:127.0.0.1:{self.ebpf_port}-:{GUEST_EBPF_PORT}",
        ]

    cmdline = "lab.telemetry=1"

    def _warn(self, what: str, err: object) -> None:
        if not self._warned:
            warn(f"telemetry: {what} failed ({err}); benchmark continues")
            self._warned = True

    def _annotate(
        self, text: str, tags: list[str], start_ms: int, end_ms: int | None = None
    ) -> int | None:
        body: dict[str, Any] = {"time": start_ms, "tags": tags, "text": text}
        if end_ms:
            body["timeEnd"] = end_ms
        try:
            return _request("POST", f"{GRAFANA_URL}/api/annotations", body=body).get("id")
        except (OSError, urllib.error.URLError, ValueError, AttributeError) as e:
            self._warn("Grafana annotation", e)
            return None

    def __enter__(self) -> Session:
        labels = {"run": self.run_id, "build": self.build, "kind": self.kind}
        for job, port in (("node", self.node_port), ("ebpf", self.ebpf_port)):
            f = TARGETS / f"{self.name}-{job}.json"
            tmp = f.with_suffix(".tmp")
            tmp.write_text(
                json.dumps([{"targets": [f"127.0.0.1:{port}"], "labels": labels}])
            )
            tmp.rename(f)  # atomic: the scraper never sees a partial file
            self.files.append(f)
        self.start_ms = int(time.time() * 1000)
        self.session_ann = self._annotate(
            f"{self.kind}: {self.build} ({self.run_id})",
            ["lab-session", f"build:{self.build}"],
            self.start_ms,
        )
        return self

    def __exit__(self, *exc: object) -> None:
        # Keep the targets a moment so the final scrape lands, then drop them.
        time.sleep(1.5)
        for f in self.files:
            f.unlink(missing_ok=True)
        end = int(time.time() * 1000)
        if self.session_ann:
            try:
                _request(
                    "PATCH",
                    f"{GRAFANA_URL}/api/annotations/{self.session_ann}",
                    body={"time": self.start_ms, "timeEnd": end},
                )
            except (OSError, urllib.error.URLError) as e:
                self._warn("Grafana annotation", e)

    def on_line(self, line: str) -> None:
        """Guest console line hook: '@@lab start <bench> <iter>' /
        '@@lab end <bench> <iter> <rc>'. Console lines carry a journal
        prefix ('[  6.2] lab-runner[483]: @@lab ...')."""
        pos = line.find("@@lab ")
        if pos < 0:
            return
        parts = line[pos:].split()
        if len(parts) < 4:
            return
        now = int(time.time() * 1000)
        key = (parts[2], parts[3])
        if parts[1] == "start":
            self.iter_start[key] = now
        elif parts[1] == "end":
            self.iter_end[key] = now
            rc = parts[4] if len(parts) > 4 else "0"
            bench, it = key
            label = "warmup" if it == "w" else f"#{it}"
            text = f"{bench} {label} on {self.build}" + (
                "" if rc == "0" else f" (FAILED rc={rc})"
            )
            self._annotate(
                text,
                ["lab-iter", f"bench:{bench}", f"build:{self.build}"],
                self.iter_start.get(key, now),
                now,
            )

    def push_iterations(self, results: Mapping[str, Mapping[str, Any]]) -> None:
        """Per-iteration values, timestamped at the end of their iteration."""
        lines: list[str] = []
        fallback = int(time.time() * 1000)
        for bench, metrics in results.items():
            for name, m in metrics.items():
                for value, it in zip(m["values"], m.get("iters", []), strict=False):
                    ts = self.iter_end.get((bench, it), fallback)
                    lab = {
                        "run": self.run_id,
                        "build": self.build,
                        "bench": bench,
                        "metric": name,
                        "unit": m["unit"],
                        "better": m["better"],
                        "iter": it,
                    }
                    lines.append(f"lab_bench_value{_labels(lab)} {value} {ts}")
        push(lines)


def push(lines: Sequence[str]) -> None:
    """Send Prometheus exposition lines (with ms timestamps) to VictoriaMetrics."""
    if not lines:
        return
    try:
        _request(
            "POST", f"{VM_URL}/api/v1/import/prometheus", data="\n".join(lines).encode()
        )
    except (OSError, urllib.error.URLError) as e:
        warn(f"telemetry: exporting results failed ({e})")


def push_summary(
    result: Mapping[str, Any], stats: Callable[[Sequence[float]], dict[str, float]]
) -> None:
    """Run-level metadata and per-metric medians for cross-build dashboards."""
    ts = int(time.time() * 1000)
    info_labels = {
        "run": result["id"],
        "build": result["build"],
        "describe": result.get("describe") or "",
        "profile": result.get("profile") or "",
        "kernel": result["guest"].get("kernel_release", ""),
        "config_hash": result.get("config_hash") or "",
    }
    lines = [f"lab_run_info{_labels(info_labels)} 1 {ts}"]
    for bench, metrics in result["results"].items():
        for name, m in metrics.items():
            if not m["values"]:
                continue
            s = stats(m["values"])
            base = {
                "run": result["id"],
                "build": result["build"],
                "bench": bench,
                "metric": name,
                "unit": m["unit"],
                "better": m["better"],
            }
            for stat in ("median", "min", "max", "stdev"):
                if not math.isnan(s[stat]):
                    lines.append(f"lab_bench_{stat}{_labels(base)} {s[stat]} {ts}")
    push(lines)
