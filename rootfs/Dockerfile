# syntax=docker/dockerfile:1.7
# Guest root filesystem. Built by `lab rootfs` and converted to images/rootfs.ext4.
FROM debian:trixie

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        systemd systemd-sysv udev kmod dbus \
        iproute2 iputils-ping procps psmisc util-linux e2fsprogs ca-certificates \
        less vim-tiny strace python3 \
        stress-ng rt-tests fio sysbench \
        linux-perf trace-cmd bpftrace \
        prometheus-node-exporter pci.ids \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ebpf_exporter + its precompiled CO-RE examples (telemetry, see monitoring/).
ARG EBPF_EXPORTER_VERSION=2.5.1
ADD --checksum=sha256:b8748fed477a3cc36e3b7465c5b8aa55d8e10d087ea4f7d6490b2a3759f9aad2 \
    https://github.com/cloudflare/ebpf_exporter/releases/download/v${EBPF_EXPORTER_VERSION}/ebpf_exporter_with_examples.x86_64.tar.gz \
    /tmp/ebpf_exporter.tar.gz
RUN mkdir -p /opt/ebpf_exporter \
    && tar -C /opt/ebpf_exporter --strip-components=1 -xzf /tmp/ebpf_exporter.tar.gz \
    && rm /tmp/ebpf_exporter.tar.gz \
    && /opt/ebpf_exporter/ebpf_exporter --version

COPY overlay/ /

# Telemetry agents only start when booted with lab.telemetry=1 (see their units).
RUN passwd -d root \
    && chmod +x /usr/local/sbin/* \
    && mkdir -p /mnt/lab /var/lib/prometheus/node-exporter \
    && prometheus-node-exporter --help 2>&1 | grep -q collector.softirqs \
    && systemctl enable systemd-networkd lab-runner.service lab-ebpf-exporter.service \
        prometheus-node-exporter.service \
    && systemctl mask systemd-networkd-wait-online.service \
    && ln -s /usr/lib/systemd/system/poweroff.target /etc/systemd/system/halt.target

# Content hash of rootfs/ (set by `lab rootfs`), lets lab detect a stale image.
ARG LAB_ROOTFS_REV=unknown
RUN echo "$LAB_ROOTFS_REV" > /etc/klab-rootfs
