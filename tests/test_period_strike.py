import unittest

from watchdog_kit.period import DAY, HOUR, MONTH, WEEK, YEAR, infer_period
from watchdog_kit.strike import ACTION_FAIL, ACTION_OK, ACTION_WARN, StrikeLedger


class PeriodTest(unittest.TestCase):
    def test_start_interval_is_exact(self):
        self.assertEqual(infer_period({"StartInterval": 900}).seconds, 900)
        self.assertEqual(infer_period({"StartInterval": 900}).approximate, False)

    def test_hour_only_is_daily(self):
        self.assertEqual(infer_period({"StartCalendarInterval": {"Hour": 8, "Minute": 0}}).seconds, DAY)

    def test_weekday_is_weekly(self):
        self.assertEqual(infer_period({"StartCalendarInterval": {"Weekday": 1, "Hour": 9}}).seconds, WEEK)

    def test_day_is_monthly(self):
        self.assertEqual(infer_period({"StartCalendarInterval": {"Day": 15, "Hour": 3}}).seconds, MONTH)

    def test_month_is_yearly(self):
        self.assertEqual(infer_period({"StartCalendarInterval": {"Month": 3, "Day": 1}}).seconds, YEAR)

    def test_several_times_a_day_shorten_the_cadence(self):
        plist = {"StartCalendarInterval": [{"Hour": 8}, {"Hour": 20}]}
        self.assertEqual(infer_period(plist).seconds, DAY // 2)

    def test_both_triggers_take_the_shorter(self):
        plist = {"StartInterval": 3600, "StartCalendarInterval": {"Hour": 8}}
        self.assertEqual(infer_period(plist).seconds, HOUR)

    def test_no_trigger_is_not_guessed(self):
        period = infer_period({"ProgramArguments": ["/bin/true"]})
        self.assertEqual(period.seconds, 0)
        self.assertTrue(period.approximate)

    def test_empty_calendar_list(self):
        self.assertEqual(infer_period({"StartCalendarInterval": []}).seconds, 0)

    def test_slack_multiplier(self):
        self.assertAlmostEqual(infer_period({"StartInterval": 3600}).with_slack(2.5), 9000)


class StrikeTest(unittest.TestCase):
    """The static-history trap: launchd's last exit code does not advance."""

    def ledger(self, clock):
        self.state = {}
        return StrikeLedger(self.state, escalate_after=6 * 3600, cooldown=600, now=lambda: clock[0])

    def test_first_failure_warns_not_fails(self):
        clock = [1000.0]
        decision = self.ledger(clock).observe("job", 1)
        self.assertEqual(decision.action, ACTION_WARN)
        self.assertEqual(decision.reason, "first-failure")

    def test_same_code_re_read_is_not_another_failure(self):
        clock = [1000.0]
        ledger = self.ledger(clock)
        ledger.observe("job", 1, runs=7)
        clock[0] = 1200.0
        decision = ledger.observe("job", 1, runs=7)
        self.assertEqual(decision.action, ACTION_WARN)
        self.assertEqual(decision.reason, "persisting")

    def test_same_code_still_unresolved_after_window_escalates_once(self):
        clock = [1000.0]
        ledger = self.ledger(clock)
        ledger.observe("job", 1, runs=7)
        clock[0] = 1000.0 + 7 * 3600
        self.assertEqual(ledger.observe("job", 1, runs=7).action, ACTION_FAIL)
        clock[0] += 60
        self.assertEqual(ledger.observe("job", 1, runs=7).action, ACTION_WARN, "escalation is one-shot")

    def test_second_failure_event_beyond_cooldown_is_a_failure(self):
        clock = [1000.0]
        ledger = self.ledger(clock)
        self.assertEqual(ledger.observe("job", 1, runs=7).action, ACTION_WARN)
        clock[0] = 1000.0 + 3600
        self.assertEqual(ledger.observe("job", 2, runs=8).action, ACTION_FAIL)

    def test_second_failure_event_within_cooldown_is_still_a_warning(self):
        # a job relaunching every minute must not be told twice per minute
        clock = [1000.0]
        ledger = self.ledger(clock)
        ledger.observe("job", 1, runs=7)
        clock[0] = 1200.0
        decision = ledger.observe("job", 2, runs=8)
        self.assertEqual(decision.action, ACTION_WARN)
        self.assertIn("in-cooldown", decision.reason)
        self.assertEqual(decision.streak, 2, "the streak still counts, only the raising is gated")

    def test_cooldown_limits_raising(self):
        clock = [1000.0]
        ledger = self.ledger(clock)
        ledger.observe("job", 1, runs=7)
        clock[0] = 1200.0
        self.assertEqual(ledger.observe("job", 2, runs=8).action, ACTION_WARN)
        self.assertEqual(ledger.observe("job", 3, runs=9).action, ACTION_WARN)
        self.assertIn("in-cooldown", ledger.observe("job", 4, runs=10).reason)
        clock[0] = 2000.0
        self.assertEqual(ledger.observe("job", 5, runs=11).action, ACTION_FAIL)

    def test_recovery_clears_the_streak(self):
        clock = [1000.0]
        ledger = self.ledger(clock)
        ledger.observe("job", 1, runs=7)
        clock[0] = 5000.0
        self.assertEqual(ledger.observe("job", 0, runs=8).action, ACTION_OK)
        self.assertEqual(self.state["job"]["streak"], 0)

    def test_absent_reading_is_neither_pass_nor_failure(self):
        clock = [1000.0]
        decision = self.ledger(clock).observe("job", None)
        self.assertEqual(decision.action, ACTION_WARN)
        self.assertEqual(decision.reason, "no-reading")

    def test_signal_death_is_not_a_zero(self):
        clock = [1000.0]
        decision = self.ledger(clock).observe("job", None, signal=True)
        self.assertEqual(decision.action, ACTION_WARN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
