import sqlite3
import tempfile
import unittest
from pathlib import Path

from watchdog_kit.period import Period
from watchdog_kit.plugins import launchd, sqlruns
from watchdog_kit.strike import StrikeLedger

T0 = 1_800_000_000.0
HOURLY = Period(3600, "StartInterval")


def job(label="com.example.hourly", period=HOURLY, stdout_path=None, disabled=False):
    return launchd.Job(label=label, path=Path("/tmp") / f"{label}.plist", period=period,
                       stdout_path=stdout_path, disabled=disabled)


def reader(runs, code=0, state="running"):
    text = f"state = {state}\n\truns = {runs}\n\tlast exit code = {code}\n"
    parsed = launchd.parse_launchctl_print(text)
    return lambda label: parsed


def scan(jobs, seen, now, runs, code=0, slack=2.5, strikes=None):
    return launchd.scan(jobs, seen, now=now, slack=slack, status_reader=reader(runs, code),
                        strikes=strikes)


class ParseTest(unittest.TestCase):
    def test_runs_and_exit_code(self):
        status = launchd.parse_launchctl_print("state = running\n\truns = 42\n\tlast exit code = 3\n")
        self.assertEqual((status.runs, status.code, status.state), (42, 3, "running"))

    def test_unloadable_service_has_no_runs(self):
        self.assertFalse(launchd.parse_launchctl_print("Could not find service").loaded)

    def test_non_numeric_exit_line_is_not_a_crash(self):
        status = launchd.parse_launchctl_print(
            "\tlast exit code = (never exited)\n\truns = 4\n")
        self.assertEqual((status.runs, status.code), (4, None))


class LaunchdScanTest(unittest.TestCase):
    def test_first_sighting_is_a_baseline_never_stale(self):
        items = scan([job()], {}, now=T0, runs=7)
        self.assertEqual([(i.id, i.state) for i in items], [("com.example.hourly", "BASELINE")])

    def test_two_fast_polls_do_not_condemn_an_hourly_job(self):
        # the counter-example that killed an earlier version: judging liveness by
        # the polling gap alone calls every interval job stale
        seen = {}
        scan([job()], seen, now=T0, runs=7)
        items = scan([job()], seen, now=T0 + 20, runs=7)
        self.assertEqual([(i.state) for i in items], ["OK"])

    def test_idle_past_period_times_slack_is_stale(self):
        seen = {"com.example.hourly": {"runs": 7, "last_advance_at": T0}}
        items = scan([job()], seen, now=T0 + 4 * 3600, runs=7)
        self.assertEqual(items[0].state, "STALE")
        self.assertIn("unchanged", items[0].detail)

    def test_empty_log_is_quiet_not_stale(self):
        directory = Path(tempfile.mkdtemp())
        empty = directory / "out.log"
        empty.write_text("")
        seen = {"com.example.hourly": {"runs": 7, "last_advance_at": T0}}
        items = scan([job(stdout_path=empty)], seen, now=T0 + 4 * 3600, runs=7)
        self.assertEqual(items[0].state, "QUIET")

    def test_nonempty_log_is_stale(self):
        directory = Path(tempfile.mkdtemp())
        full = directory / "out.log"
        full.write_text("did something\n")
        seen = {"com.example.hourly": {"runs": 7, "last_advance_at": T0}}
        items = scan([job(stdout_path=full)], seen, now=T0 + 4 * 3600, runs=7)
        self.assertEqual(items[0].state, "STALE")

    def test_ran_but_failed_is_degraded_not_stale(self):
        clock = [T0]
        strikes = StrikeLedger({}, escalate_after=6 * 3600, cooldown=600, now=lambda: clock[0])
        seen = {"com.example.hourly": {"runs": 7, "last_advance_at": T0 - 7200}}
        items = scan([job()], seen, now=T0, runs=8, code=1, strikes=strikes)
        self.assertEqual(items[0].state, "DEGRADED")
        self.assertIn("exit 1", items[0].detail)

    def test_run_count_advance_resets_the_idle_clock(self):
        seen = {"com.example.hourly": {"runs": 7, "last_advance_at": T0}}
        scan([job()], seen, now=T0 + 7200, runs=8)
        self.assertEqual(seen["com.example.hourly"]["last_advance_at"], T0 + 7200)
        items = scan([job()], seen, now=T0 + 9000, runs=8)
        self.assertEqual(items[0].state, "OK")

    def test_disabled_job_is_skipped(self):
        items = scan([job(disabled=True)], {}, now=T0, runs=3)
        self.assertEqual(items[0].state, "SKIP")

    def test_uninferable_cadence_warns_instead_of_guessing(self):
        seen = {"com.example.hourly": {"runs": 7, "last_advance_at": T0 - 10 * 86400}}
        items = scan([job(period=Period(0, "none"))], seen, now=T0, runs=7)
        self.assertEqual(items[0].state, "WARN")

    def test_not_loaded_service_is_reported(self):
        items = launchd.scan([job()], {}, now=T0,
                             status_reader=lambda label: launchd.Status(error="not-loaded"))
        self.assertEqual(items[0].state, "WARN")
        self.assertEqual(items[0].detail, "not-loaded")

    def test_unreadable_plist_is_a_finding_not_a_crash(self):
        directory = Path(tempfile.mkdtemp())
        (directory / "com.example.broken.plist").write_bytes(b"<plist><dict>{{{not xml")
        jobs = launchd.discover([directory])
        self.assertEqual(len(jobs), 1)
        self.assertIn("plist unreadable", jobs[0].error or "")
        items = launchd.scan(jobs, {}, now=T0, status_reader=reader(1))
        self.assertEqual(items[0].state, "WARN")
        self.assertIn("unreadable", items[0].detail)

    def test_discover_skips_disabled_and_excluded_labels(self):
        directory = Path(tempfile.mkdtemp())
        import plistlib

        (directory / "com.example.keep.plist").write_bytes(plistlib.dumps({"StartInterval": 60}))
        (directory / "com.apple.noise.plist").write_bytes(plistlib.dumps({"StartInterval": 60}))
        (directory / "com.example.gone.plist.disabled").write_bytes(plistlib.dumps({"StartInterval": 60}))
        jobs = launchd.discover([directory], exclude_prefixes=("com.apple.",))
        self.assertEqual([j.label for j in jobs], ["com.example.keep"])


class SqlRunsTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "runs.db"
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            "CREATE TABLE runs (run_id TEXT, status TEXT, updated_at INTEGER, result_json TEXT, spent_tokens INTEGER)"
        )
        self.spec = sqlruns.RunsSpec(
            db=str(self.path),
            table="runs",
            columns={"id": "run_id", "status": "status", "updated": "updated_at",
                     "output": "result_json", "tokens": "spent_tokens"},
            thresholds={"min_tokens": 500, "running_stale_seconds": 1800},
        )

    def tearDown(self):
        self.connection.close()

    def add(self, run_id, status, updated_at, result="{}", tokens=9000):
        self.connection.execute("INSERT INTO runs VALUES (?,?,?,?,?)",
                                (run_id, status, updated_at, result, tokens))
        self.connection.commit()

    def scan(self, **kw):
        return sqlruns.scan(self.spec, connection=self.connection, now=T0, **kw)

    def test_clean_run_produces_nothing(self):
        self.add("r1", "completed", T0 - 60, result='{"answer":"x"}', tokens=5000)
        self.assertEqual(self.scan(), [])

    def test_completed_without_payload(self):
        self.add("r2", "completed", T0 - 60, result="")
        items = self.scan()
        self.assertEqual([(i.id, i.state) for i in items], [("r2", "COMPLETED_EMPTY")])

    def test_null_payload_also_counts_as_empty(self):
        self.add("r3", "completed", T0 - 60, result=None)
        self.assertEqual(self.scan()[0].state, "COMPLETED_EMPTY")

    def test_completed_on_too_few_tokens(self):
        self.add("r4", "completed", T0 - 60, result="ok", tokens=120)
        items = self.scan()
        self.assertEqual(items[0].state, "COMPLETED_THIN")
        self.assertIn("120", items[0].detail)

    def test_stuck_running(self):
        self.add("r5", "running", T0 - 3 * 3600)
        self.assertEqual(self.scan()[0].state, "RUNNING_STUCK")

    def test_recent_running_is_fine(self):
        self.add("r6", "running", T0 - 60)
        self.assertEqual(self.scan(), [])

    def test_lookback_window_drops_old_rows(self):
        self.add("r7", "completed", T0 - 100 * 3600, result="")
        self.assertEqual(self.scan(), [])
        self.assertEqual(len(self.scan(lookback_hours=200)), 1)

    def test_identifier_injection_is_rejected(self):
        spec = sqlruns.RunsSpec(
            db=str(self.path), table="runs; DROP TABLE runs",
            columns={"id": "run_id", "status": "status", "updated": "updated_at"},
            thresholds={},
        )
        with self.assertRaises(ValueError):
            sqlruns.scan(spec, connection=self.connection, now=T0)

    def test_column_absent_from_table_is_named_not_silently_ignored(self):
        spec = sqlruns.RunsSpec(
            db=str(self.path), table="runs",
            columns={"id": "run_id", "status": "status", "updated": "updated_at", "tokens": "no_such"},
            thresholds={},
        )
        with self.assertRaises(ValueError) as caught:
            sqlruns.scan(spec, connection=self.connection, now=T0)
        self.assertIn("no_such", str(caught.exception))

    def test_a_spec_without_output_or_tokens_reports_nothing(self):
        # no result column means no way to contradict a "completed" claim, and
        # inventing one would fabricate findings
        spec = sqlruns.RunsSpec(
            db=str(self.path), table="runs",
            columns={"id": "run_id", "status": "status", "updated": "updated_at"},
            thresholds={"min_tokens": 500},
        )
        self.add("r8", "completed", T0 - 60, result="")
        self.assertEqual(sqlruns.scan(spec, connection=self.connection, now=T0), [])

    def test_epoch_milliseconds_are_understood(self):
        spec = sqlruns.RunsSpec(
            db=str(self.path), table="runs",
            columns={"id": "run_id", "status": "status", "updated": "updated_at"},
            thresholds={}, time_unit="epoch_milliseconds",
        )
        self.add("r9", "running", int((T0 - 4 * 3600) * 1000))
        self.assertEqual(sqlruns.scan(spec, connection=self.connection, now=T0)[0].state, "RUNNING_STUCK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
