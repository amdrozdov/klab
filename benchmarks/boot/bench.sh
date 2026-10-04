# description: boot time split into kernel and userspace (systemd-analyze)
# iterations: 1
# Each run_one sees the same boot, so this runs once per VM boot; use --boots N.
systemd-analyze time | python3 -c '
import re, sys
line = sys.stdin.read()
def secs(tok):
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)(min|ms|us|s)", tok):
        total += float(num) * {"min": 60, "s": 1, "ms": 1e-3, "us": 1e-6}[unit]
    return total
for part in ("kernel", "userspace"):
    m = re.search(r"([\d.a-z ]+?) \(" + part + r"\)", line)
    if m:
        print(f"METRIC {part} {secs(m.group(1)) * 1000:.1f} ms lower")
'
