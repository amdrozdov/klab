# Thin shortcuts over ./lab (run `./lab --help` for everything).
#   make latest                    fetch latest stable + build it (perf profile)
#   make rtree [SERIES=6.12]       list versions/branches available on kernel.org
#   make fetch VERSION=mainline    stable | mainline | longterm | 6.12.3 | linux-6.12.y
#   make build TREE=v6.12.3 PROFILE=debug
#   make boot [BUILD=...]          boot last (or given) build
#   make bench [BENCH="syscall hackbench"] [BUILD=...]
#   make observe                   start Grafana+VictoriaMetrics; benches/boots then report telemetry
#   make observe-down              stop it (data kept; ./lab observe down --wipe deletes it)
#   make boottrace [BUILD=...]     trace every boot step; waterfall in Grafana (Boot dashboard)
#   make clean                     delete all lab data (trees, builds, rootfs, runs); asks [y/N]
#   make lint / make typecheck     run ruff / mypy over labtool (dev tools: uv sync)
#   any target + DRY=1             only print the commands (e.g. make boot DRY=1)

LAB      := ./lab $(if $(DRY),--dry)
VERSION  ?= stable
PROFILE  ?= perf
TREE     ?=
SERIES   ?=
BUILD    ?=
BENCH    ?=
REPEAT   ?= 5

.PHONY: help clean deps doctor observe observe-down boottrace rtree latest fetch build rootfs boot gdb bench runs ls lint typecheck

help:
	@sed -n '1,14p' Makefile

deps:
	sudo apt install -y build-essential flex bison bc libelf-dev libssl-dev \
		ccache dwarves qemu-system-x86 gdb

lint:
	uv run ruff check labtool

typecheck:
	uv run mypy

doctor:
	@$(LAB) doctor

clean:
	$(LAB) clean

observe:
	$(LAB) observe up

observe-down:
	$(LAB) observe down

boottrace:
	$(LAB) boottrace $(BUILD)

rtree:
	$(LAB) rtree $(SERIES)

latest: fetch build

fetch:
	$(LAB) fetch $(VERSION)

build:
	$(LAB) build $(TREE) --profile $(PROFILE)

rootfs:
	$(LAB) rootfs

boot:
	$(LAB) boot $(BUILD)

gdb:
	$(LAB) gdb $(BUILD)

bench:
	$(LAB) bench run $(BENCH) $(if $(BUILD),--build $(BUILD)) --repeat $(REPEAT)

runs:
	$(LAB) runs

ls:
	$(LAB) ls
