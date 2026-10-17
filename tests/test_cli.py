import io
import json
import plistlib
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from watchdog_kit import cli
from watchdog_kit.plugins import launchd

T0 = 1_800_000_000.0


class Sandbox(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.state = str(root / "state.json")
        self.root = root

    def run_cli(self, argv, stdin=None, **kw):
        clock = kw.pop("now", T0)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin or "[]")), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv, now=clock, **kw)
        return code, out.getvalue(), err.getvalue()


class DiffCommandTest(Sandbox):
    ITEMS = json.dumps([{"id": "job-a", "state": "STALE", "detail": "no runs"}])

    def test_new_problem_exits_one(self):
        code, out, _ = self.run_cli(["diff", "--state", self.state, "--json"], stdin=self.ITEMS)
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual([i["id"] for i in payload["new"]], ["job-a"])

    def test_repeat_is_silent_zero(self):
        self.run_cli(["diff", "--state", self.state, "--json"], stdin=self.ITEMS)
        code, out, _ = self.run_cli(["diff", "--state", self.state, "--json"], stdin=self.ITEMS)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["new"], [])
        self.assertEqual(len(json.loads(out)["persist"]), 1)

    def test_recovery_exits_two(self):
        self.run_cli(["diff", "--state", self.state, "--json"], stdin=self.ITEMS)
        code, out, _ = self.run_cli(
            ["diff", "--state", self.state, "--json"],
            stdin=json.dumps([{"id": "job-a", "state": "OK"}]),
        )
        self.assertEqual(code, 2)
        self.assertEqual([i["id"] for i in json.loads(out)["recover"]], ["job-a"])

    def test_bad_json_is_a_scan_error(self):
        code, _, err = self.run_cli(["diff", "--state", self.state], stdin="{oops")
        self.assertEqual(code, 3)
        self.assertIn("not JSON", err)

    def test_item_without_state_is_rejected(self):
        code, _, err = self.run_cli(["diff", "--state", self.state], stdin=json.dumps([{"id": "x"}]))
        self.assertEqual(code, 3)
        self.assertIn("id and state", err)


class LaunchdCommandTest(Sandbox):
    def make_dir(self, interval=3600, label="com.example.hourly"):
        directory = self.root / "LaunchAgents"
        directory.mkdir(exist_ok=True)
        (directory / f"{label}.plist").write_bytes(
            plistlib.dumps({"Label": label, "ProgramArguments": ["/bin/true"], "StartInterval": interval})
        )
        return str(directory)

    def test_baseline_then_stale_over_successive_clocks(self):
        directory = self.make_dir()
        base = ["launchd", "--dir", directory, "--state", self.state, "--json"]
        code, out, _ = self.run_cli(base, status_reader=lambda label: launchd.Status(runs=7, code=0), now=T0)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["counts"].get("BASELINE"), 1)

        code, out, _ = self.run_cli(base, status_reader=lambda label: launchd.Status(runs=7, code=0),
                                    now=T0 + 10 * 3600)
        self.assertEqual(code, 1, out)
        self.assertEqual(json.loads(out)["counts"].get("STALE"), 1)

    def test_recovered_job_lands_in_recover(self):
        directory = self.make_dir()
        base = ["launchd", "--dir", directory, "--state", self.state, "--json"]
        self.run_cli(base, status_reader=lambda label: launchd.Status(runs=7), now=T0)
        self.run_cli(base, status_reader=lambda label: launchd.Status(runs=7), now=T0 + 10 * 3600)
        code, out, _ = self.run_cli(base, status_reader=lambda label: launchd.Status(runs=8),
                                    now=T0 + 11 * 3600)
        self.assertEqual(code, 2, out)
        self.assertEqual(json.loads(out)["counts"].get("OK"), 1)

    def test_excluded_prefix_is_not_observed(self):
        directory = self.make_dir(label="com.apple.keepout")
        code, out, _ = self.run_cli(
            ["launchd", "--dir", directory, "--state", self.state, "--json", "--exclude", "com.apple."],
            status_reader=lambda label: launchd.Status(runs=7),
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["observed"], 0)

    def test_corrupt_state_is_reported_not_reset(self):
        # --state is namespaced per plugin, so the launchd snapshot is <stem>-launchd.json
        derived = Path(self.state).with_name("state-launchd.json")
        derived.write_text("{broken", encoding="utf-8")
        code, _, err = self.run_cli(
            ["launchd", "--dir", self.make_dir(), "--state", self.state],
            status_reader=lambda label: launchd.Status(runs=1),
        )
        self.assertEqual(code, 3, err)
        self.assertIn("not readable JSON", err)
        self.assertTrue(derived.exists(), "a broken state file must not be overwritten silently")


class RunsCommandTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.db = self.root / "runs.db"
        connection = sqlite3.connect(self.db)
        connection.execute("CREATE TABLE runs (run_id TEXT, status TEXT, updated_at INTEGER, result_json TEXT)")
        connection.execute("INSERT INTO runs VALUES (?,?,?,?)", ("r1", "completed", T0 - 60, ""))
        connection.commit()
        connection.close()
        self.spec = self.root / "spec.json"
        self.spec.write_text(json.dumps({
            "db": str(self.db), "table": "runs",
            "columns": {"id": "run_id", "status": "status", "updated": "updated_at", "output": "result_json"},
            "thresholds": {},
        }), encoding="utf-8")

    def test_finding_then_silence(self):
        argv = ["runs", "--spec", str(self.spec), "--state", self.state, "--json"]
        code, out, _ = self.run_cli(argv)
        self.assertEqual(code, 1, out)
        self.assertEqual(json.loads(out)["counts"].get("COMPLETED_EMPTY"), 1)
        code, out, _ = self.run_cli(argv)
        self.assertEqual(code, 0)

    def test_missing_database_is_a_scan_error(self):
        self.spec.write_text(json.dumps({
            "db": str(self.root / "nope.db"), "table": "runs",
            "columns": {"id": "run_id", "status": "status", "updated": "updated_at"},
        }), encoding="utf-8")
        code, _, err = self.run_cli(["runs", "--spec", str(self.spec), "--state", self.state])
        self.assertEqual(code, 3)
        self.assertIn("database not found", err)

    def test_incomplete_spec_is_rejected_before_touching_sqlite(self):
        self.spec.write_text(json.dumps({"db": str(self.db), "table": "runs", "columns": {"id": "a"}}),
                             encoding="utf-8")
        code, _, err = self.run_cli(["runs", "--spec", str(self.spec), "--state", self.state])
        self.assertEqual(code, 3)
        self.assertIn("status", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
