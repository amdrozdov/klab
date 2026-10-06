"""Unit tests for `lab pi stress` (no Pi: ssh is mocked, stress-ng is a stub)."""

import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from labtool import bench, common, pi, pistress
from labtool.common import LabError

# Output captured from stress-ng 0.19.02 on the Pi (`--metrics-brief --yaml`).
YAML = """---
system-info:
      stress-ng-version: '0.19.02'
      release: '6.18.55-klab-custom-v6.18.55'
      cpus: 4

metrics:
    - stressor: cpu
      bogo-ops: 145
      bogo-ops-per-second-usr-sys-time: 24.057454
      bogo-ops-per-second-real-time: 66.407971
      wall-clock-time: 2.183473
      user-time: 6.023261
      system-time: 0.003977
      cpu-usage-per-instance: 69.009767
      max-rss: 3872

    - stressor: vm
      bogo-ops: 3405
      bogo-ops-per-second-usr-sys-time: 1226.043100
      bogo-ops-per-second-real-time: 1602.497766
      wall-clock-time: 2.124808
      user-time: 1.316286
      system-time: 1.460941
      cpu-usage-per-instance: 65.352424
      max-rss: 176916

...
"""

FACTS = (
    "kernel=6.18.55-klab-pi618\nhostname=raspberrypi\n"
    "model=Raspberry Pi 4 Model B Rev 1.5\n"
    "cpus=4\nmem_mb=3790\ngovernor=ondemand\ntemp_mc=45000\nload=0.10 0.20 0.30\n"
    "stress_ng=stress-ng, version 0.19.02\nbuild=pi618\nrelease=6.18.55-klab-pi618\n"
)


def yaml_for(stressor: str, rate: float) -> str:
    """What stress-ng would print for one stressor."""
    return (
        "---\nsystem-info:\n      cpus: 4\n\nmetrics:\n"
        f"    - stressor: {stressor}\n      bogo-ops: 10\n"
        f"      bogo-ops-per-second-real-time: {rate}\n\n...\n"
    )


STOCK_FACTS = FACTS.replace("kernel=6.18.55-klab-pi618", "kernel=6.18.50+rpt-rpi-v8")


class TestProfiles(unittest.TestCase):
    def test_parse(self) -> None:
        got = pistress.parse_profile(
            "# comment\ncpu --cpu-method matrixprod  # trailing\n\n"
            "vm:2 --vm-bytes 25%\nswitch\n"
        )
        self.assertEqual(
            got,
            [
                pistress.Stressor("cpu", None, ["--cpu-method", "matrixprod"]),
                pistress.Stressor("vm", 2, ["--vm-bytes", "25%"]),
                pistress.Stressor("switch", None, []),
            ],
        )

    def test_bad_profiles(self) -> None:
        for bad in ("", "# only a comment\n", "vm:x\n", "cpu\ncpu\n", "--cpu\n"):
            with self.assertRaises(LabError, msg=repr(bad)):
                pistress.parse_profile(bad)

    def test_shipped_profiles_load(self) -> None:
        names = pistress.list_profiles()
        self.assertIn("quick", names)
        self.assertIn("kernel", names)
        for name in names:
            self.assertTrue(pistress.load_profile(name), name)
        self.assertTrue(names["quick"])  # has a description

    def test_unknown_profile(self) -> None:
        with self.assertRaisesRegex(LabError, "available: .*quick"):
            pistress.load_profile("nope")


class TestCommand(unittest.TestCase):
    def test_shape(self) -> None:
        cmd = pistress.stressor_command(
            pistress.Stressor("vm", 2, ["--vm-bytes", "25%"]), 4, 20
        )
        self.assertIn("timeout 80 stress-ng --vm 2 --vm-bytes 25% --timeout 20s", cmd)
        for flag in ("--oom-avoid", "--metrics-brief", '--yaml "$f"'):
            self.assertIn(flag, cmd)
        default = pistress.stressor_command(pistress.Stressor("switch"), 4, 20)
        self.assertIn("stress-ng --switch 4 ", default)  # one worker per CPU

    def test_valid_shell(self) -> None:
        cmd = pistress.stressor_command(
            pistress.Stressor("cpu", None, ["--cpu-method", "x"]), 4, 5
        )
        subprocess.run(["sh", "-n", "-c", cmd], check=True)

    def run_with_stub(self, body: str) -> subprocess.CompletedProcess[str]:
        """Run the command locally with a fake stress-ng in front of PATH."""
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "stress-ng"
            stub.write_text(f"#!/bin/sh\n{body}\n")
            stub.chmod(0o755)
            env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}", "TMPDIR": d}
            cmd = pistress.stressor_command(pistress.Stressor("cpu"), 2, 3)
            result = subprocess.run(
                ["sh", "-c", cmd], env=env, capture_output=True, text=True
            )
            leftovers = [p.name for p in Path(d).iterdir() if p.name != "stress-ng"]
            self.assertEqual(leftovers, [], "temp files must be cleaned up")
            return result

    def test_prints_the_yaml_the_stressor_wrote(self) -> None:
        body = (
            'while [ $# -gt 0 ]; do [ "$1" = --yaml ] && out="$2"; shift; done\n'
            f"cat > \"$out\" <<'EOF'\n{YAML}EOF"
        )
        r = self.run_with_stub(body)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(pistress.metric_of(r.stdout, "cpu"), 66.407971)

    def test_failure_returns_the_exit_code_and_stderr(self) -> None:
        r = self.run_with_stub("echo 'stress-ng: error: out of memory' >&2; exit 3")
        self.assertEqual(r.returncode, 3)
        self.assertIn("out of memory", r.stderr)


class TestParsing(unittest.TestCase):
    def test_metrics(self) -> None:
        got = pistress.parse_metrics(YAML)
        self.assertEqual(set(got), {"cpu", "vm"})
        self.assertEqual(got["vm"]["bogo-ops-per-second-real-time"], 1602.497766)
        self.assertEqual(got["cpu"]["max-rss"], 3872.0)

    def test_system_info_is_not_mistaken_for_metrics(self) -> None:
        self.assertEqual(pistress.parse_metrics("system-info:\n      cpus: 4\n"), {})

    def test_missing_metric_is_an_error(self) -> None:
        with self.assertRaisesRegex(LabError, "no 'bogo-ops-per-second-real-time'"):
            pistress.metric_of("---\nsystem-info:\n      cpus: 4\n...\n", "cpu")
        with self.assertRaises(LabError):
            pistress.metric_of(YAML, "pipe")


class RunsDir(unittest.TestCase):
    """Point RUNS and the config cache at a temp dir."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.runs = Path(tmp.name) / "runs"
        self.cache = Path(tmp.name) / "cache"
        self.cache.mkdir()
        for module, attr, value in (
            (pistress, "RUNS", self.runs),
            (bench, "RUNS", self.runs),
            (pistress.kernel, "CONFIG_CACHE", self.cache),
        ):
            p = mock.patch.object(module, attr, value)
            p.start()
            self.addCleanup(p.stop)


class TestResult(RunsDir):
    def result(self, facts: str, values: dict[str, list[float]], rid: str = "r1") -> dict:
        return pistress.build_result(
            rid, None, "quick", 3, 20, 4, pi.parse_kv(facts), {"temp_mc": "55500"}, values
        )

    def test_running_build(self) -> None:
        self.assertEqual(pistress.running_build(pi.parse_kv(FACTS)), "pi618")
        self.assertEqual(pistress.running_build(pi.parse_kv(STOCK_FACTS)), "stock")
        old = FACTS.replace("release=6.18.55-klab-pi618", "release=6.18.55-klab-older")
        self.assertEqual(
            pistress.running_build(pi.parse_kv(old)), "stock"
        )  # klab/ is stale

    def test_stock_result(self) -> None:
        (self.cache / "pi-6.18.50+rpt-rpi-v8.config").write_text("CONFIG_A=y\n")
        r = self.result(STOCK_FACTS, {"cpu": [1.0, 2.0]})
        self.assertEqual(
            (r["build"], r["profile"], r["target"]), ("stock", "stock", "pi")
        )
        self.assertEqual(r["describe"], "rpi-os 6.18.50+rpt-rpi-v8")
        self.assertEqual(len(r["config_hash"]), 12)
        self.assertEqual(r["host"]["temp_start_c"], 45.0)
        self.assertEqual(r["host"]["temp_end_c"], 55.5)
        self.assertEqual(r["vm"], {"cpus": 4, "mem": "3790M", "pin": None, "append": ""})

    def test_klab_result_takes_its_description_from_the_local_build(self) -> None:
        meta = {
            "describe": "v6.18.55",
            "profile": "perf",
            "config_hash": "abc",
            "tree": "t",
        }
        with mock.patch.object(pistress.kernel, "build_meta", return_value=meta):
            r = self.result(FACTS, {"cpu": [1.0]})
        self.assertEqual(
            (r["build"], r["describe"], r["config_hash"]), ("pi618", "v6.18.55", "abc")
        )

    def test_works_with_runs_show_and_compare(self) -> None:
        for rid, facts, vals in (
            ("20261006-100000-pi-stock", STOCK_FACTS, [100.0, 101.0, 99.0]),
            ("20261006-110000-pi-pi618", FACTS, [120.0, 121.0, 119.0]),
        ):
            r = self.result(facts, {"cpu": vals, "switch": [5e5, 5.1e5, 4.9e5]}, rid)
            (self.runs / rid).mkdir(parents=True)
            (self.runs / rid / "result.json").write_text(json.dumps(r))
        self.assertEqual(len(bench.list_runs()), 2)
        self.assertEqual(bench.resolve_run("latest:stock"), "20261006-100000-pi-stock")
        self.assertEqual(bench.resolve_run("latest:pi618"), "20261006-110000-pi-pi618")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bench.compare(["latest:stock", "latest:pi618"])
            bench.show_run("latest:pi618")
        text = out.getvalue()
        self.assertIn("stress-ng/cpu (bogo-ops/s)", text)
        self.assertIn("better", text)  # cpu went from ~100 to ~120, outside the noise
        self.assertIn("Raspberry Pi 4 Model B Rev 1.5 (4 cores)", text)


class TestRun(RunsDir):
    def setUp(self) -> None:
        super().setUp()
        self.conn = pi.Conn("alice", "pw", "10.0.0.5", 22)
        self.commands: list[str] = []
        p = mock.patch.object(pi, "connection", return_value=self.conn)
        p.start()
        self.addCleanup(p.stop)

    def fake_remote(
        self, present: bool = True, facts: str = FACTS, fail_on: int = 0
    ) -> mock.Mock:
        stress_calls = 0

        def remote(conn: pi.Conn, command: str, stdin: Path | None = None) -> mock.Mock:
            nonlocal stress_calls
            self.commands.append(command)
            if command == pistress.STRESS_NG_PRESENT:
                return mock.Mock(stdout="yes\n" if present else "no\n")
            if command == pistress.FACTS:
                return mock.Mock(stdout=facts)
            if command == pistress.END_FACTS:
                return mock.Mock(stdout="temp_mc=60000\nload=1 1 1\n")
            if command == pistress.INSTALL:
                return mock.Mock(stdout="")
            stress_calls += 1
            if stress_calls == fail_on:
                raise LabError("on the Pi (h): stress-ng: error: boom")
            name = re.search(r"stress-ng --(\w+) ", command).group(1)  # type: ignore[union-attr]
            return mock.Mock(stdout=yaml_for(name, 100.0 + stress_calls))

        return mock.Mock(side_effect=remote)

    def run_stress(self, **kw: object) -> str | None:
        with (
            mock.patch.object(pi, "remote", self.fake_remote(**kw)),
            mock.patch(  # type: ignore[arg-type]
                "builtins.print"
            ),
        ):
            return pistress.stress("quick", repeat=2, timeout=7, label="t")

    def test_runs_every_stressor_every_repeat_and_saves_the_result(self) -> None:
        run_id = self.run_stress()
        self.assertTrue(run_id and run_id.endswith("-pi-pi618-t"))
        stress_cmds = [c for c in self.commands if c.startswith("f=$(mktemp)")]
        self.assertEqual(len(stress_cmds), 2 * len(pistress.load_profile("quick")))
        self.assertTrue(all("--timeout 7s" in c for c in stress_cmds))
        rundir = self.runs / str(run_id)
        self.assertEqual(len(list((rundir / "raw").glob("*.yaml"))), len(stress_cmds))
        result = json.loads((rundir / "result.json").read_text())
        self.assertEqual(len(result["results"]["stress-ng"]["switch"]["values"]), 2)

    def test_does_not_install_when_present(self) -> None:
        self.run_stress()
        self.assertNotIn(pistress.INSTALL, self.commands)

    def test_installs_stress_ng_when_missing(self) -> None:
        self.run_stress(present=False)
        self.assertIn(pistress.INSTALL, self.commands)
        self.assertLess(
            self.commands.index(pistress.INSTALL), self.commands.index(pistress.FACTS)
        )

    def test_a_failing_stressor_keeps_the_raw_output_and_writes_no_result(self) -> None:
        with self.assertRaisesRegex(LabError, "boom") as ctx:
            self.run_stress(fail_on=3)
        self.assertIn("partial output kept in runs/", str(ctx.exception))
        (rundir,) = list(self.runs.iterdir())
        self.assertFalse((rundir / "result.json").exists())
        self.assertEqual(len(list((rundir / "raw").glob("*.yaml"))), 2)
        self.assertEqual(bench.list_runs(), [])  # an unfinished run is not listed

    def test_dry_run_only_prints(self) -> None:
        out = io.StringIO()
        with (
            mock.patch.object(common, "DRY", True),
            mock.patch.object(pi, "remote") as remote,
            contextlib.redirect_stdout(out),
        ):
            self.assertIsNone(pistress.stress("quick", repeat=1, timeout=5))
        remote.assert_not_called()
        self.assertFalse(self.runs.exists())
        self.assertIn("--timeout 5s", out.getvalue())
        self.assertIn("<cpus>", out.getvalue())

    def test_rejects_nonsense_numbers(self) -> None:
        for kw in ({"repeat": 0}, {"timeout": 0}, {"workers": 0}):
            with self.assertRaises(LabError):
                pistress.stress("quick", **kw)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
