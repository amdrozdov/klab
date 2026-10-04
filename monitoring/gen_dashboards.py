#!/usr/bin/env python3
"""Generate the Grafana dashboards in monitoring/grafana/dashboards/.

    python3 monitoring/gen_dashboards.py            # (re)write the JSON files
    python3 monitoring/gen_dashboards.py --verify   # list live eBPF metrics without a panel

EBPF_CONFIGS mirrors rootfs/overlay/etc/klab/ebpf-configs: every metric of every
enabled ebpf_exporter config gets a panel. Grafana reloads the files automatically.
"""

import json
import sys
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent / "grafana" / "dashboards"
CONFIG_LIST = Path(__file__).resolve().parent.parent / "rootfs/overlay/etc/klab/ebpf-configs"
DS = {"type": "prometheus", "uid": "victoriametrics"}
GUEST = 'job="guest-node", build=~"$build", run=~"$run"'
EBPF = 'job="guest-ebpf", build=~"$build", run=~"$run"'
RATE = "$__rate_interval"

# config -> [(metric, kind, labels to group by, unit, description)]
# kind: counter (shown as rate), gauge (raw value), histogram (heatmap + quantiles + rate)
EBPF_CONFIGS = {
    "syscalls": [
        ("syscalls_total", "counter", ["syscall"], "ops", "Syscalls per second by name (raw_syscalls tracepoints)"),
        ("syscall_errors_total", "counter", ["errno"], "ops", "Failed syscalls per second by errno"),
    ],
    "timers": [
        ("timer_starts_total", "counter", ["function"], "ops", "Kernel timers started per second, by callback function"),
    ],
    "percpu-softirq": [
        ("softirqs_total", "counter", ["vec", "cpu"], "ops", "Softirq handler calls per second by vector and CPU"),
    ],
    "softirq-latency": [
        ("softirq_entry_latency_seconds", "histogram", ["kind"], "s", "Time from softirq raise to handler entry"),
        ("softirq_service_latency_seconds", "histogram", ["kind"], "s", "Time spent servicing a softirq (entry to exit)"),
        ("softirq_raised_total", "counter", ["kind"], "ops", "Softirqs raised per second"),
        ("softirq_serviced_total", "counter", ["kind"], "ops", "Softirqs serviced per second"),
    ],
    "llcstat": [
        ("llc_references_total", "counter", ["cpu"], "ops", "Last-level cache references per second (sampled PMU events)"),
        ("llc_misses_total", "counter", ["cpu"], "ops", "Last-level cache misses per second (sampled PMU events)"),
    ],
    "biolatency": [
        ("bio_latency_seconds", "histogram", ["device", "operation"], "s", "Block I/O latency"),
    ],
    "ext4dist": [
        ("ext4_latency_seconds", "histogram", ["operation"], "s", "ext4 operation latency (read/write/open/fsync)"),
    ],
    "cachestat": [
        ("page_cache_ops_total", "counter", ["operation"], "ops", "Page cache operations per second (hits, misses, dirtied)"),
    ],
    "shrinklat": [
        ("shrink_node_latency_seconds", "histogram", [], "s", "Memory reclaim: shrink_node() latency"),
    ],
    "oomkill": [
        ("oom_kills_total", "counter", ["cgroup_path"], "ops", "OOM kills per second by cgroup"),
    ],
    "bpf-jit": [
        ("bpf_jit_pages_currently_allocated", "gauge", [], "none", "Pages currently allocated for BPF JIT images"),
    ],
    "cfs-throttling": [
        ("cfs_throttling_seconds", "histogram", ["cgroup"], "s", "CFS bandwidth throttling duration by cgroup"),
    ],
    "cgroup-rstat-flushing": [
        ("cgroup_rstat_flush_total", "counter", ["level"], "ops", "cgroup rstat flushes per second by cgroup level"),
        ("cgroup_rstat_locked_total", "counter", ["contended", "yield", "level"], "ops", "rstat lock acquisitions per second"),
        ("cgroup_rstat_map_errors_total", "counter", ["type"], "ops", "BPF map errors in the rstat program"),
        ("cgroup_rstat_lock_wait_seconds", "histogram", [], "s", "Time waiting for the rstat lock"),
        ("cgroup_rstat_lock_hold_seconds", "histogram", [], "s", "Time holding the rstat lock"),
        ("cgroup_rstat_flush_latency_seconds", "histogram", ["level"], "s", "rstat flush latency"),
    ],
    "unix-socket-backlog": [
        ("unix_socket_backlog", "histogram", ["addr"], "none", "Unix socket receive backlog length (hackbench uses unix sockets)"),
    ],
    "accept-latency": [
        ("accept_latency_seconds", "histogram", ["port"], "s", "Time sockets wait in the accept queue"),
    ],
    "tcp-syn-backlog": [
        ("tcp_syn_backlog", "histogram", [], "none", "TCP SYN backlog size"),
    ],
    "tcp-retransmit": [
        ("tcp_retransmit_ipv4_packets_total", "counter", ["type", "main_port"], "ops", "IPv4 TCP retransmits per second"),
        ("tcp_retransmit_ipv6_packets_total", "counter", ["type", "main_port"], "ops", "IPv6 TCP retransmits per second"),
    ],
    "tcp-window-clamps": [
        ("tcp_window_clamps_total", "counter", [], "ops", "TCP window clamped to a low value, per second"),
    ],
    "udp-drops": [
        ("udp_fail_queue_rcv_skbs_total", "counter", ["local_port"], "ops", "UDP packets dropped (receive buffer full)"),
    ],
    "kfree_skb": [
        ("kfree_skb_total", "counter", ["reason", "ip_proto"], "ops", "Packets freed per second by drop reason"),
    ],
    "inet-frags": [
        ("inet_frags_total", "counter", ["interface", "ip_version"], "ops", "IP fragments per second"),
    ],
    "icmp-ip": [
        ("icmp4_received_packets_total", "counter", ["source_addr"], "ops", "ICMPv4 packets received per second"),
        ("icmp6_received_packets_total", "counter", ["source_addr"], "ops", "ICMPv6 packets received per second"),
    ],
}


# --------------------------------------------------------------------------- panel helpers

class Layout:
    """Places panels left-to-right on a 24-column grid, wrapping rows."""

    def __init__(self):
        self.panels, self.x, self.y, self.row_h, self.next_id = [], 0, 0, 0, 1

    def add(self, panel, w=12, h=8):
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        panel["id"] = self.next_id
        panel["gridPos"] = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.next_id += 1
        self.panels.append(panel)
        self.x += w
        self.row_h = max(self.row_h, h)

    def row(self, title, collapsed=False):
        self.x, self.y = 0, self.y + self.row_h
        self.row_h = 0
        self.add({"type": "row", "title": title, "collapsed": collapsed, "panels": []}, w=24, h=1)


def target(expr, legend="", ref="A", **kw):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref, **kw}


def timeseries(title, targets, unit="short", desc="", stack=False, points=False, legend_calcs=None):
    return {
        "type": "timeseries", "title": title, "description": desc, "datasource": DS,
        "targets": targets if isinstance(targets, list) else [targets],
        "fieldConfig": {"defaults": {
            "unit": unit,
            "custom": {"drawStyle": "points" if points else "line", "lineWidth": 1,
                       "pointSize": 6, "fillOpacity": 20 if stack else 5,
                       "showPoints": "always" if points else "never", "spanNulls": False,
                       "stacking": {"mode": "normal" if stack else "none"}}},
            "overrides": []},
        "options": {"legend": {"displayMode": "table", "placement": "right",
                               "calcs": legend_calcs or ["mean", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def heatmap(title, expr, unit="s", desc=""):
    return {
        "type": "heatmap", "title": title, "description": desc, "datasource": DS,
        "targets": [target(expr, "{{le}}", format="heatmap")],
        "options": {"calculate": False, "cellGap": 1, "yAxis": {"unit": unit, "axisPlacement": "left"},
                    "rowsFrame": {"layout": "auto"},
                    "color": {"mode": "scheme", "scheme": "Spectral", "steps": 64,
                              "exponent": 0.5, "scale": "exponential", "reverse": True},
                    "filterValues": {"le": 1e-9}, "legend": {"show": True},
                    "tooltip": {"mode": "single", "yHistogram": True, "showColorScale": True}},
    }


def table(title, expr, hide=(), desc="", value_name="Value", mappings=None, unit="short"):
    exclude = {k: True for k in ("Time", "__name__", "job", "instance", "kind", *hide)}
    return {
        "type": "table", "title": title, "description": desc, "datasource": DS,
        "targets": [target(expr, format="table", instant=True, range=False)],
        "transformations": [{"id": "organize", "options": {
            "excludeByName": exclude, "renameByName": {"Value": value_name}}}],
        "fieldConfig": {"defaults": {"unit": unit, "mappings": mappings or []}, "overrides": []},
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
    }


def text(content, title=""):
    return {"type": "text", "title": title, "options": {"mode": "markdown", "content": content}}


TEMPO = {"type": "tempo", "uid": "tempo"}


def query_var(name, label, query, multi=True, include_all=True, hide=0, sort=1):
    v = {"name": name, "label": label, "type": "query", "datasource": DS,
         "query": {"query": query, "refId": name}, "definition": query,
         "multi": multi, "includeAll": include_all, "refresh": 2, "sort": sort, "hide": hide}
    if include_all:
        v.update(allValue=".*", current={"text": "All", "value": "$__all"})
    return v


def dashboard(uid, title, layout, desc, templating=None, annotations=None):
    return {
        "uid": uid, "title": title, "description": desc, "tags": ["klab"],
        "editable": True, "schemaVersion": 41, "version": 1,
        "time": {"from": "now-30m", "to": "now"}, "refresh": "5s",
        "timepicker": {"refresh_intervals": ["1s", "5s", "10s", "30s", "1m"]},
        "links": [{"type": "dashboards", "tags": ["klab"], "asDropdown": False,
                   "title": "klab", "includeVars": True, "keepTime": True}],
        "templating": {"list": templating if templating is not None else [
            {"name": "build", "label": "Build", "type": "query", "datasource": DS,
             "query": {"query": 'label_values(up{job=~"guest-.*"}, build)', "refId": "build"},
             "definition": 'label_values(up{job=~"guest-.*"}, build)',
             "multi": True, "includeAll": True, "allValue": ".*", "refresh": 2,
             "current": {"text": "All", "value": "$__all"}, "sort": 1},
            {"name": "run", "label": "Run", "type": "query", "datasource": DS,
             "query": {"query": 'label_values(up{job=~"guest-.*", build=~"$build"}, run)',
                       "refId": "run"},
             "definition": 'label_values(up{job=~"guest-.*", build=~"$build"}, run)',
             "multi": True, "includeAll": True, "allValue": ".*", "refresh": 2,
             "current": {"text": "All", "value": "$__all"}, "sort": 2},
        ]},
        "annotations": {"list": annotations if annotations is not None else [
            {"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
             "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
             "name": "Annotations & Alerts", "type": "dashboard"},
            {"name": "Benchmark iterations", "enable": True, "iconColor": "#F2CC0C",
             "datasource": {"type": "grafana", "uid": "-- Grafana --"},
             "target": {"type": "tags", "tags": ["lab-iter"], "matchAny": False, "limit": 2000}},
            {"name": "VM sessions", "enable": True, "iconColor": "#8AB8FF",
             "datasource": {"type": "grafana", "uid": "-- Grafana --"},
             "target": {"type": "tags", "tags": ["lab-session"], "matchAny": False, "limit": 500}},
        ]},
        "panels": layout.panels,
    }


# --------------------------------------------------------------------------- dashboards

def overview():
    L = Layout()
    L.add(text(
        "Telemetry from `lab bench run` / `lab boot` while the observe stack is up. "
        "**Yellow regions** = benchmark iterations, **blue** = VM sessions (toggle at the top). "
        "Pick builds/runs above; guest panels are the kernel under test, *Host & QEMU* shows "
        "whether the host added noise. eBPF metrics: see **klab — eBPF**."), w=24, h=3)

    L.row("Runs & results")
    L.add(table("Guest kernels in range",
                f'last_over_time(node_uname_info{{{GUEST}}}[$__range])',
                hide=("domainname", "machine", "sysname", "nodename"), value_name="up"), w=10, h=7)
    L.add(table("Benchmark medians (runs in range)",
                'last_over_time(lab_bench_median{build=~"$build", run=~"$run"}[$__range])',
                value_name="median"), w=14, h=7)
    L.add(timeseries("Per-iteration results",
                     target('lab_bench_value{build=~"$build", run=~"$run"}',
                            "{{bench}}/{{metric}} [{{unit}}] {{build}}"),
                     points=True, desc="One point per benchmark iteration, at the time it finished",
                     legend_calcs=["lastNotNull"]), w=24, h=8)

    L.row("Guest CPU & scheduler")
    L.add(timeseries("CPU time by mode (cores)",
                     target(f'sum by (mode) (rate(node_cpu_seconds_total{{{GUEST}, mode!="idle"}}[{RATE}]))',
                            "{{mode}}"), stack=True, desc="user/system/softirq/irq/steal ... summed over vCPUs"))
    L.add(timeseries("Busy per vCPU",
                     target(f'1 - rate(node_cpu_seconds_total{{{GUEST}, mode="idle"}}[{RATE}])',
                            "cpu{{cpu}}"), unit="percentunit"))
    L.add(timeseries("Context switches / s",
                     target(f'sum by (run) (rate(node_context_switches_total{{{GUEST}}}[{RATE}]))', "{{run}}")), w=8)
    L.add(timeseries("Forks / s",
                     target(f'sum by (run) (rate(node_forks_total{{{GUEST}}}[{RATE}]))', "{{run}}")), w=8)
    L.add(timeseries("Running / blocked tasks",
                     [target(f'node_procs_running{{{GUEST}}}', "running"),
                      target(f'node_procs_blocked{{{GUEST}}}', "blocked", "B")]), w=8)
    L.add(timeseries("Run-queue wait (schedstat)",
                     target(f'sum by (cpu) (rate(node_schedstat_waiting_seconds_total{{{GUEST}}}[{RATE}]))',
                            "cpu{{cpu}}"), unit="s",
                     desc="Seconds per second tasks spent runnable but waiting for a CPU"))
    L.add(timeseries("Timeslices / s (schedstat)",
                     target(f'sum by (cpu) (rate(node_schedstat_timeslices_total{{{GUEST}}}[{RATE}]))',
                            "cpu{{cpu}}")))
    L.add(timeseries("Load average",
                     [target(f'node_load1{{{GUEST}}}', "1m"), target(f'node_load5{{{GUEST}}}', "5m", "B")]), w=8)
    L.add(timeseries("CPU pressure (PSI)",
                     target(f'rate(node_pressure_cpu_waiting_seconds_total{{{GUEST}}}[{RATE}])', "some"),
                     unit="percentunit", desc="Empty if the kernel has no CONFIG_PSI"), w=8)
    L.add(timeseries("Threads / processes",
                     [target(f'node_processes_threads{{{GUEST}}}', "threads"),
                      target(f'sum(node_processes_state{{{GUEST}}})', "processes", "B")]), w=8)

    L.row("Guest interrupts & softirqs")
    L.add(timeseries("Interrupts / s",
                     target(f'sum by (run) (rate(node_intr_total{{{GUEST}}}[{RATE}]))', "{{run}}")), w=8)
    L.add(timeseries("Interrupts / s by source (top 10)",
                     target(f'topk(10, sum by (type, info) (rate(node_interrupts_total{{{GUEST}}}[{RATE}])))',
                            "{{type}} {{info}}")), w=16)
    L.add(timeseries("Softirqs / s by type",
                     target(f'sum by (type) (rate(node_softirqs_functions_total{{{GUEST}}}[{RATE}]))',
                            "{{type}}"), stack=True), w=24)

    L.row("Guest memory")
    L.add(timeseries("Memory",
                     [target(f'node_memory_MemAvailable_bytes{{{GUEST}}}', "available"),
                      target(f'node_memory_Cached_bytes{{{GUEST}}}', "page cache", "B"),
                      target(f'node_memory_AnonPages_bytes{{{GUEST}}}', "anon", "C"),
                      target(f'node_memory_Slab_bytes{{{GUEST}}}', "slab", "D")], unit="bytes"))
    L.add(timeseries("Page faults / s",
                     [target(f'rate(node_vmstat_pgfault{{{GUEST}}}[{RATE}])', "minor+major"),
                      target(f'rate(node_vmstat_pgmajfault{{{GUEST}}}[{RATE}])', "major", "B")]))

    L.row("Guest disk & network")
    L.add(timeseries("Disk throughput",
                     [target(f'rate(node_disk_read_bytes_total{{{GUEST}}}[{RATE}])', "read {{device}}"),
                      target(f'rate(node_disk_written_bytes_total{{{GUEST}}}[{RATE}])', "write {{device}}", "B")],
                     unit="Bps"))
    L.add(timeseries("Network throughput",
                     [target(f'rate(node_network_receive_bytes_total{{{GUEST}, device!="lo"}}[{RATE}])', "rx {{device}}"),
                      target(f'rate(node_network_transmit_bytes_total{{{GUEST}, device!="lo"}}[{RATE}])', "tx {{device}}", "B")],
                     unit="Bps"))

    L.row("Host & QEMU (noise sources)")
    vcpu = 'groupname=~"lab-.*", threadname=~"CPU .*"'
    L.add(timeseries("vCPU threads: host CPU (cores)",
                     target(f'sum by (groupname, threadname) (rate(namedprocess_namegroup_thread_cpu_seconds_total{{{vcpu}}}[{RATE}]))',
                            "{{groupname}} {{threadname}}"),
                     desc="How busy each vCPU thread is on the host"))
    L.add(timeseries("vCPU threads: involuntary context switches / s",
                     target(f'sum by (groupname, threadname) (rate(namedprocess_namegroup_thread_context_switches_total{{{vcpu}, ctxswitchtype="nonvoluntary"}}[{RATE}]))',
                            "{{groupname}} {{threadname}}"),
                     desc="Host preempting vCPUs: high values mean the host stole time from the guest"))
    L.add(timeseries("QEMU non-vCPU threads (cores)",
                     target(f'sum by (groupname, threadname) (rate(namedprocess_namegroup_thread_cpu_seconds_total{{groupname=~"lab-.*", threadname!~"CPU .*"}}[{RATE}]))',
                            "{{groupname}} {{threadname}}"), desc="I/O, 9p and main-loop threads"), w=8)
    L.add(timeseries("Host CPU busy",
                     target(f'1 - avg(rate(node_cpu_seconds_total{{job="host-node", mode="idle"}}[{RATE}]))', "busy"),
                     unit="percentunit"), w=8)
    L.add(timeseries("Host CPU frequency",
                     [target('avg(node_cpu_scaling_frequency_hertz{job="host-node"})', "avg"),
                      target('min(node_cpu_scaling_frequency_hertz{job="host-node"})', "min", "B"),
                      target('max(node_cpu_scaling_frequency_hertz{job="host-node"})', "max", "C")],
                     unit="hertz", desc="Frequency swings (powersave governor, turbo, thermals) skew results"), w=8)
    L.add(timeseries("Host load & temperature",
                     [target('node_load1{job="host-node"}', "load1"),
                      target('max(node_hwmon_temp_celsius{job="host-node"})', "max temp °C", "B")]), w=12)
    L.add(timeseries("QEMU resident memory",
                     target('sum by (groupname) (namedprocess_namegroup_memory_bytes{groupname=~"lab-.*", memtype="resident"})',
                            "{{groupname}}"), unit="bytes"), w=12)
    return dashboard("lab-overview", "klab — Overview", L,
                     "Guest kernel, host and QEMU telemetry for klab runs")


def ebpf():
    L = Layout()
    L.add(text(
        "Metrics from **ebpf_exporter** in the guest (CO-RE programs, needs a kernel with BTF). "
        "Each config is checked at boot and only loaded if it attaches on the running kernel — "
        "the status table shows what's available for the selected runs. Histograms: heatmap of "
        "the distribution + p50/p90/p99 + event rate. Yellow regions = benchmark iterations."),
        w=24, h=3)
    L.row("Status")
    loaded = [{"type": "value", "options": {
        "0": {"text": "not attached", "color": "red"}, "1": {"text": "attached", "color": "green"}}}]
    L.add(table("eBPF configs on the guest kernel",
                f'max by (config, reason) (last_over_time(lab_ebpf_config_loaded{{{GUEST}}}[$__range]))',
                value_name="status", mappings=loaded), w=12, h=10)
    L.add(table("BTF available (per run)",
                f'max by (run, build) (last_over_time(lab_ebpf_btf_available{{{GUEST}}}[$__range]))',
                value_name="BTF", mappings=[{"type": "value", "options": {
                    "0": {"text": "no BTF", "color": "red"}, "1": {"text": "yes", "color": "green"}}}]),
          w=6, h=10)
    L.add(timeseries("ebpf_exporter scrape duration",
                     target(f'scrape_duration_seconds{{{EBPF}}}', "{{run}}"), unit="s",
                     desc="Cost of reading all BPF maps each second"), w=6, h=10)

    for config, metrics in EBPF_CONFIGS.items():
        L.row(f"{config}")
        for name, kind, labels, unit, desc in metrics:
            full = f"ebpf_exporter_{name}"
            by = ", ".join(labels)
            legend = " ".join(f"{{{{{l}}}}}" for l in labels) or name
            if kind == "counter":
                expr = f'sum by ({by}) (rate({full}{{{EBPF}}}[{RATE}]))' if labels else \
                    f'sum(rate({full}{{{EBPF}}}[{RATE}]))'
                if len(labels) and name in ("syscalls_total", "timer_starts_total", "kfree_skb_total",
                                            "softirqs_total"):
                    expr = f"topk(15, {expr})"
                L.add(timeseries(f"{name} (rate)", target(expr, legend), unit=unit, desc=desc))
            elif kind == "gauge":
                L.add(timeseries(name, target(f'sum({full}{{{EBPF}}})', name), unit=unit, desc=desc))
            else:
                L.add(heatmap(f"{name} distribution",
                              f'sum by (le) (rate({full}_bucket{{{EBPF}}}[{RATE}]))', unit=unit,
                              desc=desc), w=8)
                q = [target(f'histogram_quantile({p}, sum by (le{", " + by if by else ""}) '
                            f'(rate({full}_bucket{{{EBPF}}}[{RATE}])))',
                            f"p{int(p * 100)} {legend if labels else ''}".strip(), ref)
                     for p, ref in ((0.5, "A"), (0.9, "B"), (0.99, "C"))]
                L.add(timeseries(f"{name} quantiles", q, unit=unit, desc=desc), w=8)
                L.add(timeseries(f"{name} events / s",
                                 target(f'sum by ({by}) (rate({full}_count{{{EBPF}}}[{RATE}]))' if labels
                                        else f'sum(rate({full}_count{{{EBPF}}}[{RATE}]))', legend),
                                 unit="ops", desc=desc), w=8)
    return dashboard("lab-ebpf", "klab — eBPF", L,
                     "Every metric exported by the guest's ebpf_exporter configs")


def compare():
    L = Layout()
    L.add(text("Results exported by `lab bench run` while the observe stack is up "
               "(`lab compare` in the terminal does the statistics). Choose builds/runs above."),
          w=24, h=2)
    sel = 'build=~"$build", run=~"$run"'
    L.add(table("Median per run",
                f'last_over_time(lab_bench_median{{{sel}}}[$__range])', value_name="median"),
          w=24, h=10)
    L.add(table("Spread per run (stdev)",
                f'last_over_time(lab_bench_stdev{{{sel}}}[$__range])', value_name="stdev"),
          w=24, h=8)
    L.add(timeseries("Iterations over time",
                     target(f'lab_bench_value{{{sel}}}', "{{bench}}/{{metric}} [{{unit}}] {{build}}"),
                     points=True, legend_calcs=["lastNotNull", "min", "max"]), w=24, h=10)
    return dashboard("lab-compare", "klab — Bench results", L,
                     "Benchmark results exported by lab")


def boot():
    L = Layout()
    run = 'run="$boot"'
    L.add(text(
        "Every step of a boot, recorded by `lab boottrace` (initcall tracepoints, kernel log "
        "milestones, systemd unit timestamps). Pick a **boot** above for the waterfall: "
        "click a span → *Span attributes* → **link.vscode** opens the function in your local "
        "tree, **link.elixir** on elixir.bootlin.com (systemd units: **link.docs**). "
        "The *kernel* span's events are kernel-log milestones. "
        "Compare builds at the bottom. Phases: pre-kernel (QEMU + firmware + decompression, "
        "host-measured, approx.) → kernel (`start_kernel` → initcall levels → root mount) "
        "→ userspace (systemd units)."), w=24, h=3)

    L.row("Selected boot")
    L.add({"type": "stat", "title": "Phases", "datasource": DS,
           "targets": [target(f'last_over_time(lab_boot_seconds{{{run}}}[$__range])', "{{phase}}",
                              instant=True, range=False)],
           "fieldConfig": {"defaults": {"unit": "s", "decimals": 3}, "overrides": []},
           "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "textMode": "value_and_name",
                       "colorMode": "none", "graphMode": "none", "orientation": "vertical",
                       "text": {"titleSize": 14, "valueSize": 28}}},
          w=18, h=4)
    L.add({"type": "stat", "title": "Initcalls", "datasource": DS,
           "targets": [target(f'count(last_over_time(lab_boot_initcall_seconds{{{run}}}[$__range]))',
                              "initcalls", instant=True, range=False)],
           "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "none",
                       "graphMode": "none"}}, w=6, h=4)
    L.add({"type": "traces", "title": "Boot waterfall", "datasource": TEMPO,
           # queryType must be "traceql": the browser plugin turns a trace-ID query into a
           # lookup itself; "traceId" is backend-only and leaves the panel loading forever.
           "targets": [{"datasource": TEMPO, "queryType": "traceql", "query": "${trace_id}",
                        "refId": "A", "limit": 20}]}, w=24, h=24)
    slowest = table("Slowest initcalls",
                    f'topk(30, last_over_time(lab_boot_initcall_seconds{{{run}}}[$__range]))',
                    hide=("run", "build"), value_name="time", unit="s")
    slowest["options"]["sortBy"] = [{"displayName": "time", "desc": True}]
    slowest["fieldConfig"]["overrides"] = [{"matcher": {"id": "byName", "options": "time"},
                                            "properties": [{"id": "custom.cellOptions", "value": {
                                                "type": "gauge", "mode": "gradient"}}]}]
    L.add(slowest, w=12, h=12)
    L.add({"type": "barchart", "title": "Time per initcall level", "datasource": DS,
           "targets": [target(f'last_over_time(lab_boot_level_seconds{{{run}}}[$__range])',
                              format="table", instant=True, range=False)],
           "transformations": [
               {"id": "organize", "options": {"excludeByName": {
                   "Time": True, "__name__": True, "run": True, "build": True}}},
               {"id": "sortBy", "options": {"sort": [{"field": "level", "desc": False}]}}],
           "fieldConfig": {"defaults": {"unit": "s"}, "overrides": []},
           "options": {"xField": "level", "orientation": "horizontal", "showValue": "auto",
                       "legend": {"showLegend": False}}}, w=12, h=12)
    units = table("Slowest systemd units",
                  f'topk(25, last_over_time(lab_boot_unit_seconds{{{run}}}[$__range]))',
                  hide=("run", "build"), value_name="time", unit="s")
    units["options"]["sortBy"] = [{"displayName": "time", "desc": True}]
    L.add(units, w=24, h=10)

    L.row("Compare builds (all boots of the selected builds in range)")
    sel = 'build=~"$build"'
    L.add(timeseries("Boot phases per boot",
                     target(f'lab_boot_seconds{{{sel}, phase!="total"}}', "{{phase}} {{build}}"),
                     unit="s", points=True, legend_calcs=["lastNotNull", "min", "max"]), w=24, h=8)

    def matrix(title, expr, row_field, h=10):
        t = table(title, expr, value_name="Value", unit="s")
        t["transformations"] = [{"id": "groupingToMatrix", "options": {
            "columnField": "build", "rowField": row_field, "valueField": "Value"}}]
        L.add(t, w=12 if h <= 10 else 24, h=h)

    matrix("Phase averages by build",
           f'avg by (build, phase) (last_over_time(lab_boot_seconds{{{sel}}}[$__range]))', "phase")
    matrix("Initcall level averages by build",
           f'avg by (build, level) (last_over_time(lab_boot_level_seconds{{{sel}}}[$__range]))',
           "level")
    matrix("Initcalls ≥ 0.5 ms on any build (averages)",
           f'avg by (build, fn) (last_over_time(lab_boot_initcall_seconds{{{sel}}}[$__range])) '
           f'and on (fn) (max by (fn) (avg by (build, fn) '
           f'(last_over_time(lab_boot_initcall_seconds{{{sel}}}[$__range]))) > 0.0005)', "fn", h=16)

    templating = [
        query_var("build", "Build", "label_values(lab_boot_info, build)"),
        query_var("boot", "Boot", 'label_values(lab_boot_info{build=~"$build"}, run)',
                  multi=False, include_all=False, sort=2),
        query_var("trace_id", "trace", 'label_values(lab_boot_info{run="$boot"}, trace_id)',
                  multi=False, include_all=False, hide=2),
    ]
    annotations = [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                    "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                    "name": "Annotations & Alerts", "type": "dashboard"}]
    d = dashboard("lab-boot", "klab — Boot", L,
                  "Boot process traces from lab boottrace", templating, annotations)
    d["time"] = {"from": "now-7d", "to": "now"}
    d["refresh"] = ""
    return d


# --------------------------------------------------------------------------- main

def check_config_list():
    listed = [l.strip() for l in CONFIG_LIST.read_text().splitlines()
              if l.strip() and not l.startswith("#")]
    missing = set(listed) ^ set(EBPF_CONFIGS)
    if missing:
        sys.exit(f"EBPF_CONFIGS and {CONFIG_LIST} differ: {sorted(missing)}")


def verify(url="http://127.0.0.1:8428"):
    """Report ebpf_exporter metrics present in VictoriaMetrics but missing a panel."""
    with urllib.request.urlopen(f"{url}/api/v1/label/__name__/values", timeout=5) as r:
        names = json.load(r)["data"]
    covered = {f"ebpf_exporter_{m[0]}" for ms in EBPF_CONFIGS.values() for m in ms}
    live = {n.removesuffix("_bucket").removesuffix("_sum").removesuffix("_count")
            for n in names if n.startswith("ebpf_exporter_")}
    internal = {n for n in live if n.startswith(("ebpf_exporter_ebpf_", "ebpf_exporter_enabled",
                                                 "ebpf_exporter_build", "ebpf_exporter_decoder",
                                                 "ebpf_exporter_attach"))}
    gaps = sorted(live - covered - internal)
    print(f"live ebpf metrics: {len(live - internal)}, with panels: {len((live - internal) & covered)}")
    for g in gaps:
        print(f"  no panel: {g}")
    return not gaps


def main():
    check_config_list()
    if "--verify" in sys.argv:
        sys.exit(0 if verify() else 1)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, d in (("lab-overview", overview()), ("lab-ebpf", ebpf()), ("lab-compare", compare()),
                    ("lab-boot", boot())):
        (OUT / f"{name}.json").write_text(json.dumps(d, indent=2) + "\n")
        print(f"wrote {OUT / name}.json ({len(d['panels'])} panels)")


if __name__ == "__main__":
    main()
