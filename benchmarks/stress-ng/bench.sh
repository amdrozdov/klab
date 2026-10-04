# description: stress-ng throughput: context switch, futex, page faults, pipe, fork
set -e
for s in switch futex fault pipe fork; do
    stress-ng --$s 0 --timeout 5s --metrics-brief --yaml /tmp/sng.yaml >/dev/null 2>&1
    v=$(awk '/bogo-ops-per-second-real-time:/ {print $2; exit}' /tmp/sng.yaml)
    echo "METRIC $s ${v} ops/s higher"
done
