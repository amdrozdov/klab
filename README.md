<p align="center">
  <img src="assets/logo.png" alt="klab logo" width="220">
</p>

<p align="center"><b>Linux kernel lab toolset</b></p>

<p align="center">
  <a href="https://github.com/amdrozdov/klab/actions/workflows/tests.yml">
    <img src="https://github.com/amdrozdov/klab/actions/workflows/tests.yml/badge.svg?branch=main" alt="Tests: passing or failing">
  </a>
  <a href="https://github.com/amdrozdov/klab/actions/workflows/ci.yml">
    <img src="https://github.com/amdrozdov/klab/actions/workflows/ci.yml/badge.svg?branch=main" alt="Lint and types: passing or failing">
  </a>
</p>

Fetch, build, boot and benchmark Linux kernels in QEMU, and compare builds.

## eBPF tracing

`make observe` turns on Grafana telemetry for every boot and benchmark — host and
guest metrics plus live eBPF probes on the running kernel (syscalls, scheduler,
block/ext4 latency, page cache). See [Observing](#observing-grafana-telemetry).

<p align="center">
  <img src="assets/sc2.png" alt="klab overview dashboard" width="800">
  <br><sub>Overview dashboard</sub>
</p>

<p align="center">
  <img src="assets/sc1.png" alt="context switches traced with eBPF" width="800">
  <br><sub>Context switches, traced with eBPF</sub>
</p>

The CLI is a uv project (stdlib only, Python >= 3.10). `./lab` is a shim for
`uv run lab` that works from any directory; `uv sync` creates `.venv` on first use.

```
uv sync                         # (once) set up .venv; or just run ./lab
./lab doctor                    # check host dependencies
make deps                       # (once) apt install what's missing
./lab rootfs                    # (once) Debian guest image via docker -> images/rootfs.ext4

make latest                     # = ./lab fetch stable && ./lab build
./lab boot                      # boot the last build; Ctrl-A X to quit
./lab bench run syscall hackbench

make observe                    # optional: Grafana telemetry for every run (see Observing)
```

Add `--dry` to any command (`./lab boot --dry`, `./lab --dry build -p debug`,
`make boot DRY=1`) to print the exact git / make / docker / qemu commands as a shell
script without running or changing anything.

## Layout

| Path | What |
|---|---|
| `lab`, `labtool/`, `pyproject.toml` | the CLI (uv project, stdlib only) |
| `configs/*.config` | kconfig fragments: `base` (always, incl. BTF), profiles `perf` / `debug` |
| `rootfs/` | Dockerfile + overlay for the guest image (autologin root, 9p mounts, job runner) |
| `benchmarks/<name>/bench.sh` | benchmarks that run inside the guest |
| `trees/` | kernel sources: shallow clones + git worktree variants |
| `builds/<name>/` | out-of-tree build dirs (`make O=`), with `lab-build.json` metadata |
| `runs/<id>/result.json` | benchmark results + metadata (commit, config hash, host, VM size) |
| `shared/` | mounted at `/mnt/lab` in interactive boots — drop files/modules here |
| `monitoring/` | telemetry stack: docker compose, scrape config, Grafana dashboards + generator |

`trees/ builds/ images/ runs/` are gitignored.

## Kernel versions and patches

```
./lab rtree                       # what's on kernel.org: series, latest, status, branch, local
./lab rtree 6.12                  # every release of one series (--rc adds release candidates)
./lab rtree 6                     # only 6.x series
./lab rtree --branches            # linux-X.Y.y, linux-rolling-*, master

./lab fetch                       # latest stable (kernel.org releases.json)
./lab fetch mainline              # latest -rc
./lab fetch 6.12.3                # specific version -> trees/v6.12.3
./lab fetch linux-6.12.y          # head of a branch -> trees/linux-6.12.y

./lab tree new myexp --from v6.12.3 --patch fix.patch   # variant as git worktree
./lab tree patch more.patch --tree myexp                # add patches later
./lab tree show myexp
```

Cleaning up (check names and sizes with `./lab ls` first; add `--dry` to preview):

```
./lab rm build v6.12.3-perf            # deletes builds/v6.12.3-perf
./lab rm tree myexp                    # variant: git worktree + its branch are removed
./lab rm tree v6.12.3                  # base clone: refused while variants still use it
./lab rm run 20261003-032052           # a benchmark/boottrace run (id, unique part, latest:<build>)
```

Start over completely with `./lab clean` (or `make clean`): it lists every lab-data path with
its size and asks `[y/N]` before deleting trees, builds, the rootfs image, runs, `.lab/`,
`shared/*` and monitoring targets (but keeps `.lab/pi.json`, your Raspberry Pi settings).
It only deletes what `.gitignore` marks as ignored (so
uncommitted sources are safe), never touches `experiments/` or `configs/`, keeps `.venv`,
refuses while a lab VM is running, and needs `--yes` when there is no terminal.
The observe stack's stored metrics live in docker volumes: `./lab observe down --wipe`.

Removing a tree does not remove its builds (it warns which builds still reference it).
Removing the current default (`*` in `lab ls`) clears it; pass names explicitly or run
`lab build <tree>` to set a new one.

Variants are ordinary git branches/worktrees: you can also just edit
`trees/myexp/` and commit. Patches can be `git format-patch` output (`git am`) or plain diffs.

## Builds

```
./lab build v6.12.3                    # -> builds/v6.12.3-perf
./lab build myexp -p debug             # -> builds/myexp-debug (DWARF, KASAN, lockdep, gdb scripts)
./lab build myexp --menuconfig
./lab diffconfig v6.12.3-perf myexp-perf
```

The three builds along the path from lab VM to real hardware:

```
# 1. Quick lab build: minimal defconfig+kvm_guest config, fast to build, boots the VM.
./lab build v6.8.12 -p perf

# 2. Full VM build from your host (distro) config, trimmed to fit and still VM-bootable:
#    olddefconfig-adapted host config + lab-vm overlay + certs fix, debug info off.
./lab build v6.8.12 --base-config host --no-debug-info

# 3. Full build for real hardware: the exact host config (--raw = no lab-vm overlay),
#    debug info off. This is the artifact you package; it may not boot the lab VM.
./lab build v6.8.12 --base-config host --no-debug-info --raw

# 4. A kernel for the Raspberry Pi 4 (arm64, cross-built; needs gcc-aarch64-linux-gnu):
#    see "Custom kernels on the Pi" below. -n sets the build name, which also shows
#    up in `uname -r` on the Pi.
./lab build v6.18.55 --arch pi -n pi618
```

Config = `defconfig` + `kvm_guest.config` + `configs/base.config` + profile + `-f` fragments.
`base` includes BTF (needed by eBPF tools and the telemetry), so builds need `pahole`
(`sudo apt install dwarves`); debug info only affects the build, not the running kernel.
Options you asked for that kconfig dropped (unmet dependencies) are reported as warnings.
Modules are installed into `builds/<b>/modroot` and appear at `/lib/modules` in the guest.
`compile_commands.json` is generated and linked into the tree for clangd.

### Full configs (`--base-config`)

To build the config you'd actually run on real hardware (the Stage-2 "full config"
gate before a laptop/Pi), start from a complete `.config` instead of `defconfig`:

```
./lab build v6.12.3 --base-config host        # /boot/config-$(uname -r) (your distro)
./lab build v6.12.3 --base-config arch         # Arch's public config (fetched, cached)
./lab build v6.12.3 --base-config path/to/.config      # a local file (.gz ok)
./lab build v6.12.3 --base-config https://…/config     # any URL (.gz ok)
```

The config is copied in, adapted to the target tree with `make olddefconfig`, then the
profile and any `-f` fragments are merged on top. The source, its sha256 and whether
`--raw` was used are recorded in `builds/<b>/lab-build.json` (provenance). Downloads are
cached under `configs/.cache/`. Ubuntu has no single downloadable per-version config, so
use `--base-config host` on an Ubuntu machine (a remote Ubuntu-by-version source is a
planned follow-up).

A full distro config with DWARF + BTF is a 20–30 GB build. For a config/boot validation
you rarely need debug info, so `--no-debug-info` disables DWARF and BTF (roughly halving
build size and disk use); you lose source-level gdb and BTF-based eBPF, but the running
kernel is unaffected. Distro configs also point module-signing at packaging files a
vanilla tree lacks (`debian/canonical-certs.pem`), which would fail the build at the
certs step — the tool clears those automatically (unless `--raw`).

> **⚠️ virtio / 9p must be built-in, or the lab VM won't boot.**
> A distro config ships `virtio`, `9p` and friends as **modules**. But the lab boots the
> kernel directly (`-kernel`) and mounts the module tree *over 9p* — so the 9p and virtio
> drivers needed to reach `/lib/modules` aren't loaded yet. To avoid that chicken-and-egg,
> `--base-config` merges [`configs/lab-vm.config`](configs/lab-vm.config) on top, forcing
> those few symbols (`VIRTIO*`, `EXT4_FS`, `NET_9P*`, `9P_FS`, `SERIAL_8250_CONSOLE`)
> **built-in**. Pass **`--raw`** to skip this overlay and build the *exact* ship config —
> correct for packaging to real hardware, but such a build may not boot the lab VM (Gate 1)
> until you add those options yourself.

## Booting

```
./lab boot [build]                 # rootfs changes are discarded on exit
./lab boot --persist               # keep changes (e.g. apt install something)
./lab boot --cpus 8 --mem 8G --append "mitigations=off"
./lab boot myexp-debug --gdb       # paused; then in another terminal:
./lab gdb myexp-debug              #   gdb with vmlinux, lx-* helpers, target remote :1234
```

Leave the guest with `poweroff`, `halt` or `reboot` (all make qemu exit), or Ctrl-A X.
`halt` is mapped to power-off in the guest, since a real halt leaves qemu running.

The guest has perf, bpftrace, trace-cmd, strace, stress-ng, fio, sysbench, hackbench.
Network uses QEMU user mode (outbound only).

## Benchmarks

```
./lab bench list
./lab bench run syscall -b v6.12.3-perf -r 10 --boots 3 --pin 4-11
./lab runs
./lab show latest
./lab compare latest:v6.12.3-perf latest:myexp-perf
```

`compare` uses the first run as the baseline and marks a change only when it exceeds
2x the combined coefficient of variation (or `--threshold`).

Adding a benchmark: create `benchmarks/<name>/bench.sh` that prints
`METRIC <name> <value> <unit> <higher|lower>` lines. `*.c` files next to it are compiled
statically on the host. Header comments: `# description: ...`, `# iterations: 1`.

### Reducing noise

- Benchmark `perf` builds only, never `debug` (KASAN/lockdep).
- `sudo cpupower frequency-set -g performance`, disable turbo, keep the host idle.
- `--pin` qemu to dedicated host CPUs; same `--cpus/--mem` for every run you compare.
- Use more `--repeat` and `--boots`; look at `cv`.

## Observing (Grafana telemetry)

```
make observe                  # = ./lab observe up
./lab bench run hackbench     # unchanged; now also streams telemetry
open http://127.0.0.1:3000    # dashboards: folder "klab"
make observe-down             # stop (data kept); ./lab observe down --wipe deletes it
./lab observe status
```

`make observe` (re)builds the guest rootfs if it lacks the telemetry agents, then starts
the stack from `monitoring/docker-compose.yml`. All ports bind to 127.0.0.1 only.

| Service | Where | What |
|---|---|---|
| Grafana | http://127.0.0.1:3000 | dashboards, no login (local only) |
| VictoriaMetrics | http://127.0.0.1:8428/vmui | Prometheus-compatible storage + scraper, 90 days |
| Tempo | 127.0.0.1:3200 / OTLP 4318 | trace storage for `lab boottrace` waterfalls, 90 days |
| code-links | http://127.0.0.1:3001 | redirects trace source links to `vscode://file/...` |
| node-exporter | host | host CPU frequency, load, temperature |
| process-exporter | host | QEMU per-thread stats (vCPU threads named `CPU n/KVM`) |
| node-exporter | guest | guest kernel: CPU, schedstat, interrupts, softirqs, memory, PSI, ... |
| ebpf_exporter | guest | eBPF programs: syscalls, timers, softirq latency, block/ext4 latency, page cache, ... |

**While the stack is up, telemetry is on automatically** for `lab bench run` and `lab boot`:
lab forwards the guest's exporter ports, registers them as scrape targets (labels
`build`, `run`), marks every benchmark iteration as a Grafana annotation, and exports the
results. When the stack is down nothing changes: the guest agents only start when the
kernel is booted with `lab.telemetry=1`.

Dashboards (folder *klab*, pick builds/runs at the top):

- **Overview**: guest kernels and benchmark medians in range; guest CPU by mode,
  context switches, run-queue wait (schedstat), PSI, interrupts, softirqs, memory, page
  faults, disk/net; host CPU frequency/load and QEMU vCPU threads (host preemption = noise).
- **eBPF**: which ebpf_exporter configs attached on the selected kernel (and why not),
  then every metric: counters as rates, histograms as heatmap + p50/p90/p99 + events/s.
- **Bench results**: medians/stdev per run and every iteration over time.
- **Boot**: `lab boottrace` waterfalls and boot comparisons (see below).

Yellow regions are benchmark iterations, blue ones are VM sessions.

Notes:

- **Overhead.** eBPF probes cost time on every event they trace (e.g. `getpid` rises from
  ~68 ns to ~110 ns with the syscall counter attached). Use it to *understand* behaviour;
  compare final numbers with `--no-telemetry`. `lab compare` warns when mixing the two.
- **What attaches depends on the kernel.** The guest checks each config from
  `rootfs/overlay/etc/klab/ebpf-configs` and loads only the ones that work; the eBPF
  dashboard lists the others with the libbpf error (on 7.2: `oomkill`, `cfs-throttling`,
  `cgroup-rstat-flushing`, `unix-socket-backlog`). Network counters appear after the first
  event (e.g. only under network load).
- **No PMU on hybrid Intel hosts.** KVM gives guests no hardware perf counters on P/E-core
  CPUs (`/sys/module/kvm/parameters/enable_pmu` = N), so `llcstat` stays empty there.
- **BTF needed.** A build without BTF still boots with node metrics, but eBPF panels stay
  empty (lab warns); rebuild with `lab build`.
- **Changing dashboards.** Edit `monitoring/gen_dashboards.py` and run it (Grafana reloads
  the JSON). To add an eBPF config, list it in `rootfs/overlay/etc/klab/ebpf-configs`
  *and* `EBPF_CONFIGS`, then `make observe` (rebuilds the rootfs).
  `python3 monitoring/gen_dashboards.py --verify` lists live eBPF metrics without a panel.

## Boot process (`lab boottrace`)

```
make boottrace                       # = ./lab boottrace [build]
./lab boottrace v7.2.8-perf --boots 5 -l baseline
./lab compare latest:v7.2.8-perf ...  # boottrace runs are compare-able (pre-kernel/kernel/userspace)
./lab boottrace --resend             # re-upload saved traces Tempo is missing
```

Every trace is also saved as `runs/<id>/boot*/trace.otlp.json`. If the waterfall says *No data
found in response*, Tempo doesn't have that trace (e.g. it was down or still starting during
the boot): `lab boottrace --resend` uploads the missing ones; `make observe` does this
automatically and waits until Tempo is ready.

Boots the build with the kernel's initcall tracepoints on (`trace_event=initcall:*`,
near-zero cost), then collects the ftrace buffer, `dmesg` and systemd unit timestamps and
prints phases, kernel-log milestones, time per initcall level and the slowest initcalls and
units. With `make observe` running it also sends the boot to Grafana — **klab — Boot**:

```
boot <build>
├─ pre-kernel             QEMU start, firmware, kernel load + decompression (host-measured, approx.)
├─ kernel                 events on this span = kernel-log milestones
│  ├─ start_kernel        setup_arch, mm, sched, IRQs, timers (+ console initcalls)
│  ├─ initcalls: early    pre-SMP
│  ├─ SMP bring-up + driver core
│  ├─ initcalls: pure → core → postcore → arch → subsys → fs → device → late
│  │     └─ one span per initcall (µs exact)
│  └─ after initcalls     wait for async device probes → mount root → free init memory → exec init
└─ userspace (systemd)    one span per unit, activating → active
```

**Code walkthrough:** click any span → *Span attributes*: `link.vscode` opens the function at
its definition in your local tree (VS Code; via the `code-links` redirect on :3001, so
patched variants open their own source), `link.elixir` opens it on elixir.bootlin.com at the
tree's base tag, plus `code.filepath`/`code.lineno`. Initcalls are resolved from the
build's `vmlinux` debug info (`nm` + `addr2line`, cached in `builds/<b>/lab-sources.json`);
phase spans link to the function implementing them (`start_kernel`, `do_initcall_level`,
`wait_for_device_probe`, `prepare_namespace`, ...); systemd units get `link.docs` (man page).
Other editor: change the scheme in `monitoring/code-links/nginx.conf` (e.g. `cursor://file`).

The dashboard has the waterfall (Tempo) for the selected boot, phase times, slowest
initcalls, time per level, slowest units, and build comparisons (phases, levels and
every initcall ≥ 0.5 ms side by side).

Things to try:

- Read the code behind a span: `init/main.c` (`start_kernel`, `kernel_init`,
  `do_initcalls`), `include/linux/init.h` (initcall levels), `init/do_mounts.c` (root mount).
- On the default q35 machine ~0.4 s of the kernel phase is *waiting* in
  `wait_for_device_probe()` for async probes (AHCI scanning six empty SATA ports, the
  emulated DVD drive, PS/2 mouse). Disable `CONFIG_SATA_AHCI`/`CONFIG_ATA` in a variant
  (`lab build myexp --menuconfig`), boottrace both and compare.
- Initcall costs per subsystem: `pci_subsys_init` (PCI enumeration), `acpi_init` (ACPI
  namespace) dominate `subsys`; compare after trimming the config or with `--cpus 1`.
- Userspace: `systemd-binfmt` and `systemd-networkd` each take ~1 s in this rootfs.

## Raspberry Pi (`lab pi`)

Put Raspberry Pi OS on a USB stick or SD card with your user, Wi-Fi and ssh already
configured, boot the Pi, and log in with one command.

```
./lab pi setup deb            # find the stick, ask once, write Raspberry Pi OS Lite (64-bit)
                              # -> put it in the Pi, power on, wait 2-3 minutes
./lab pi shell                # first time: finds <hostname>.local, or explains how to find the Pi
./lab pi host 192.168.1.50    # save the Pi's address (and --port) once you know it
./lab pi shell                # ssh with the saved host, port, user and password
./lab pi shell -- uname -a    # run one command instead of a shell
./lab pi show                 # saved settings (passwords hidden; --show-secrets)
```

`setup` autodetects removable disks (USB sticks, SD cards, card readers; a reader with no
card shows as 0 B and is skipped) and never offers a disk that holds a system mount. It
asks for username, password, Wi-Fi name and password (empty = no Wi-Fi) and the Wi-Fi
country, then **asks you to type the device name before erasing it**. Everything you answer
is saved, so the next `setup` asks nothing (`--ask` to be asked again; every answer is also
a flag, e.g. `--user`, `--wifi-ssid`, `--device /dev/sdb`).

How it works: the current image is looked up in the Raspberry Pi Imager OS list, downloaded
into `images/pi/` and verified against its published SHA-256 (cached for next time). A copy
gets `user-data` (user with a password hash, hostname, ssh password login), `network-config`
(Wi-Fi) and an empty `ssh` file written into its boot partition with `mtools`, and only the
final `dd` needs `sudo`. On first boot the Pi applies them with cloud-init, joins the
Wi-Fi and starts sshd. `--no-write` only prepares `images/pi/custom.img`; `--dry` prints
every command. Only current cloud-init images are supported (not the legacy ones).

`lab pi shell` finds the Pi at `<hostname>.local` (mDNS). If that fails it prints how to
look for it on your network, with your actual subnet (`sudo nmap -sn 192.168.1.0/24`), and
you save the address with `lab pi host <ip>`. Password login uses `SSH_ASKPASS` (OpenSSH
8.4+), so `sshpass` is not needed; the Pi's host key is kept in `.lab/pi_known_hosts`
and reset on every reflash.

Settings live in `.lab/pi.json` (mode 0600, gitignored; `lab clean` keeps it). The passwords
are stored in plain text there, as is the Wi-Fi password in `network-config` on the stick.
Needs `mtools` and `openssl` (`lab doctor` checks; `nmap` is optional).

### Arch Linux ARM instead of Raspberry Pi OS

```
./lab pi setup arch           # same questions and answers as for deb, same card
```

The card can be reused: run `setup deb` or `setup arch` again to switch distros, each
build is from scratch. Arch Linux ARM is only published as a root filesystem tarball
(850 MB, MD5), so `setup arch` assembles the whole image itself without root: a
partition table, a 512 MiB FAT32 `/boot`, an ext4 root filled from the tarball under
`fakeroot`. The default user `alarm` is renamed to yours and root is locked. Wi-Fi goes
through `wpa_supplicant`, and only the hashed PSK is stored on the card, not your
passphrase. Arch has no `sudo`, so the package is downloaded, SHA-256 checked and
installed offline on first boot. That first boot also grows the root partition and
initialises the pacman keyring; once online, `htop`, `vim` and `stress-ng` are installed
(retried until they succeed, allow a few minutes).

`lab pi kernel install/status/stock` and `lab pi stress` detect the OS over ssh
(`/etc/os-release`) and adapt: Arch keeps its boot files in `/boot` (U-Boot, no
`cmdline.txt`), so install writes `/boot/klab/cmdline.txt` for the root PARTUUID and
selects it with `os_prefix=klab/`, and the stock kernel stays the fallback. The kernel
config for `lab build --arch pi` is read from the embedded config (IKCONFIG) of the
Pi's stock `/boot/Image.gz`, cached as `arch-<version>.config`. `lab pi stress` records
the OS and the stress-ng version, and `lab compare` warns when they differ. Extra
tools for building the image: `fakeroot`, `dosfstools`, `mtools` (`make deps-pi`).
The image boot itself (U-Boot, `klab/kernel8.img`) is untested on real hardware as of
writing, so report what you see on the first Arch boot.

### Custom kernels on the Pi

Build your own kernel and boot the Pi with it. The Pi must already be set up and
reachable (`lab pi shell` works), and the host needs the cross-compiler
(`make deps-pi`, or `sudo apt install gcc-aarch64-linux-gnu`).

```
./lab fetch 6.18.55                              # a kernel.org tree (6.18.y matches Raspberry Pi OS)
./lab build v6.18.55 --arch pi -n pi618 --no-debug-info        # -> builds/pi618   (-n NAME is optional)
./lab pi kernel install pi618 --reboot           # copy it to the Pi, boot it from now on
./lab pi kernel status                           # uname -r on the Pi: 6.18.55-klab-pi618

# in case of issues only
./lab pi kernel stock --reboot                   # (optional)back to the stock Raspberry Pi OS kernel
```

Note: on arch by default you may have perf issues, add this to your boot/config.txt
```
temp_limit=80
# arm_freq=1200 cap freq to 1200 ghZ in case of overheats
```

The **build name** is what you pass to `lab pi kernel install` and what identifies the
kernel on the Pi: `uname -r` there is `<kernel version>-klab-<build name>`, so with
`-n pi618` it reads `6.18.55-klab-pi618`. Without `-n` the name is `<tree>-pi`
(`v6.18.55-pi`, release `6.18.55-klab-v6.18.55-pi`). Use a different name for each variant
you want to tell apart, for example `-n pi618-nopreempt`; the whole release must stay under
64 characters. `lab pi kernel install` without a name installs the last Pi build you made,
and `lab ls` lists them (marked `[pi]`).

`--arch pi` cross-compiles for arm64 and starts from the **stock Raspberry Pi OS kernel
config of your Pi** (read over ssh from `/boot/config-*-rpi-v8`, cached in
`configs/.cache/`; it stays the stock one even while your own kernel runs, and the cached
copy is used when the Pi is off). That config already has everything a Pi 4 needs to boot
built in, so no initramfs is required; options that exist only in Raspberry Pi's own
kernel fork simply drop out on a kernel.org tree.
Other bases: `--base-config defconfig` (generic arm64) or a path / URL. `configs/pi.config`
pins the boot-critical drivers whatever the base. A Pi build never replaces your default x86
build, and it cannot boot in the lab VM (`lab boot` refuses it).

`lab pi kernel install` puts the kernel **next to** the stock one, it replaces nothing:

- the kernel (`kernel8.img`), the Pi 4 device trees and a copy of the live `cmdline.txt`
  go to `/boot/firmware/klab/`, the modules to `/lib/modules/<release>`;
- a block at the end of `config.txt` (between `# klab begin` and `# klab end`) sets
  `os_prefix=klab/`, which makes the firmware load the kernel, device tree and
  `cmdline.txt` from that directory, and `auto_initramfs=0`;
- the change is permanent until `lab pi kernel stock` removes the block.

If the kernel does not boot: the firmware falls back to the stock kernel by itself when
`klab/` has no kernel or device tree; for any other failure put the card in a PC and delete
the `# klab begin` ... `# klab end` block from `config.txt` on the `bootfs` partition.
Only the Pi 4 is supported for now.

### Stress-testing the Pi (`lab pi stress`)

Run stress-ng on the Pi and keep the result as a normal run, to compare kernels on real
hardware:

```
./lab pi stress                  # profile "quick": cpu, switch, pipe, futex, vm, memcpy
./lab pi stress kernel -r 5      # kernel-heavy profile, 5 repeats
./lab pi stress --list           # profiles (benchmarks/pi/*.stress)

./lab pi kernel stock --reboot   # then: ./lab pi stress
./lab pi kernel install pi618 --reboot   # then: ./lab pi stress
./lab compare latest:stock latest:pi618
```

It installs `stress-ng` on the Pi if it is missing (`apt`), runs every stressor on its own
for `-t` seconds (default 20) and `-r` times (default 3), and saves
`runs/<id>/result.json` (the metric is bogo-ops per second, real time) next to the raw
stress-ng YAML in `runs/<id>/raw/`. The run is labelled with the build the Pi is running
(`stock` for Raspberry Pi OS's own kernel), so `lab runs`, `lab show` and `lab compare` work
on it like on any benchmark run. A stressor line in a profile is
`name[:workers] [stress-ng options]`; the default is one worker per CPU, and `vm` is sized
as a share of free memory so it never swaps. Stop whatever else runs on the Pi first: the
command does not check, and a busy or hot Pi gives noisy numbers (the result records the
governor and the temperature before and after).

## License

GPL-3.0, see [LICENSE](LICENSE). The logo is an original drawing of Tux, the Linux mascot
created by Larry Ewing (lewing@isc.tamu.edu) with The GIMP.
