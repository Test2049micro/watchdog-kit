"""Core semantics: only change makes noise, and a baseline is never an alert."""

import json
import tempfile
import unittest
from pathlib import Path

from watchdog_kit.diff import Item, classify
from watchdog_kit.state import StateCorrupt, load, save

T0 = 1_800_000_000.0


class DiffTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = load(self.path())

    @staticmethod
    def path():
        handle = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        handle.close()
        Path(handle.name).unlink()
        return handle.name

    def round(self, *items, now=T0, escalate_after=7 * 86400):
        return classify(list(items), self.snapshot, now=now, escalate_after=escalate_after)

    def test_first_problem_is_new(self):
        diff = self.round(Item("a", "STALE", "no runs"))
        self.assertEqual([i.id for i in diff.new], ["a"])
        self.assertEqual(diff.exit_code, 1)

    def test_same_problem_next_round_is_silent(self):
        self.round(Item("a", "STALE", "no runs"))
        diff = self.round(Item("a", "STALE", "no runs"), now=T0 + 3600)
        self.assertEqual(diff.new, [])
        self.assertEqual([i.id for i in diff.persisted], ["a"])
        self.assertEqual(diff.exit_code, 0)

    def test_persisting_too_long_escalates_exactly_once(self):
        self.round(Item("a", "STALE"))
        diff = self.round(Item("a", "STALE"), now=T0 + 8 * 86400, escalate_after=7 * 86400)
        self.assertEqual([i.id for i in diff.escalated], ["a"])
        again = self.round(Item("a", "STALE"), now=T0 + 30 * 86400, escalate_after=7 * 86400)
        self.assertEqual(again.escalated, [], "escalation must not repeat every poll")

    def test_state_change_is_loud(self):
        self.round(Item("a", "STALE"))
        diff = self.round(Item("a", "DEGRADED", "exit 1"), now=T0 + 60)
        self.assertEqual([i.id for i in diff.changed], ["a"])
        self.assertEqual(diff.exit_code, 1)

    def test_recovery_is_loud_and_exit_two(self):
        self.round(Item("a", "STALE"))
        diff = self.round(Item("a", "OK"), now=T0 + 60)
        self.assertEqual([i.id for i in diff.recovered], ["a"])
        self.assertEqual(diff.exit_code, 2)

    def test_baseline_is_not_a_problem(self):
        diff = self.round(Item("a", "BASELINE", "first count 3"))
        self.assertEqual(diff.new, [])
        self.assertEqual(diff.exit_code, 0)

    def test_disappearing_subject_reports_recovery_once(self):
        self.round(Item("a", "STALE"))
        diff = self.round(now=T0 + 60)
        self.assertEqual([i.id for i in diff.recovered], ["a"])
        gone_again = self.round(now=T0 + 120)
        self.assertEqual(gone_again.recovered, [])

    def test_escalation_clock_restarts_on_a_different_failure(self):
        self.round(Item("a", "STALE"))
        self.round(Item("a", "DEGRADED"), now=T0 + 8 * 86400)
        diff = self.round(Item("a", "DEGRADED"), now=T0 + 9 * 86400, escalate_after=7 * 86400)
        self.assertEqual(diff.escalated, [], "new failure mode must not inherit the old clock")


class StateTest(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "state.json"

    def test_missing_file_is_first_run_not_corrupt(self):
        snapshot = load(self.path)
        self.assertTrue(snapshot.is_first_run)

    def test_roundtrip_keeps_plugin_counters(self):
        snapshot = load(self.path)
        snapshot.seen["a"] = {"runs": 12, "state": "OK", "first_seen": T0}
        save(snapshot, now=T0)
        reloaded = load(self.path)
        self.assertEqual(reloaded.seen["a"]["runs"], 12)

    def test_corrupt_file_raises_instead_of_looking_empty(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(StateCorrupt):
            load(self.path)

    def test_cap_drops_oldest_seen_and_survives(self):
        snapshot = load(self.path)
        for index in range(10):
            snapshot.seen[f"job{index}"] = {"state": "OK", "seen_at": T0 + index}
        save(snapshot, now=T0 + 100, cap=4)
        reloaded = load(self.path)
        self.assertEqual(len(reloaded.seen), 4)
        self.assertIn("job9", reloaded.seen)

    def test_no_temporary_files_are_left_behind(self):
        snapshot = load(self.path)
        snapshot.seen["a"] = {"state": "OK"}
        save(snapshot, now=T0)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["state.json"])

    def test_payload_is_json_with_version(self):
        snapshot = load(self.path)
        snapshot.seen["a"] = {"state": "OK", "seen_at": T0}
        save(snapshot, now=T0)
        data = json.loads(self.path.read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["ts"], T0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
