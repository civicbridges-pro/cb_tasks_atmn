"""Business-hours math. A clock that is wrong here makes every SLA a lie."""

from __future__ import annotations

import datetime as dt
import unittest

from cbops import clock
from tests.support import load_config


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.jason = clock.coverage_for(self.cfg, "jason")   # 07:00-17:00 America/Boise

    def test_unconfirmed_coverage_is_reported_not_invented(self):
        """A clock computed on a placeholder looks authoritative and is not."""
        taj = clock.coverage_for(self.cfg, "taj")
        self.assertFalse(taj.known)
        self.assertTrue(self.jason.known)

    def test_add_hours_rolls_over_the_weekend(self):
        friday_4pm = dt.datetime(2026, 8, 21, 16, 0, tzinfo=self.jason.tz)
        result = clock.add_business_hours(self.jason, friday_4pm, 4)
        self.assertEqual(result, dt.datetime(2026, 8, 24, 10, 0, tzinfo=self.jason.tz))

    def test_elapsed_hours_skip_the_weekend(self):
        friday_4pm = dt.datetime(2026, 8, 21, 16, 0, tzinfo=self.jason.tz)
        monday_10am = dt.datetime(2026, 8, 24, 10, 0, tzinfo=self.jason.tz)
        self.assertEqual(clock.business_hours_between(self.jason, friday_4pm, monday_10am), 4.0)

    def test_elapsed_hours_never_go_negative(self):
        later = dt.datetime(2026, 8, 24, 10, 0, tzinfo=self.jason.tz)
        earlier = dt.datetime(2026, 8, 21, 16, 0, tzinfo=self.jason.tz)
        self.assertEqual(clock.business_hours_between(self.jason, later, earlier), 0.0)

    def test_federal_holiday_does_not_count(self):
        """A contracting officer is not reading mail on Thanksgiving."""
        wednesday = dt.datetime(2026, 11, 25, 16, 0, tzinfo=self.jason.tz)
        result = clock.add_business_hours(self.jason, wednesday, 2)
        self.assertEqual(result.date(), dt.date(2026, 11, 27))   # Thursday skipped

    def test_observed_holidays_shift(self):
        holidays = clock.us_federal_holidays(2027)
        # 4 July 2027 is a Sunday, observed on the Monday.
        self.assertIn(dt.date(2027, 7, 5), holidays)
        self.assertNotIn(dt.date(2027, 7, 4), holidays)

    def test_unconfirmed_coverage_falls_back_to_wall_clock(self):
        taj = clock.coverage_for(self.cfg, "taj")
        start = dt.datetime(2026, 8, 22, 3, 0, tzinfo=dt.timezone.utc)   # Saturday
        self.assertEqual(
            clock.add_business_hours(taj, start, 6),
            start + dt.timedelta(hours=6),
        )


class DeadlineTest(unittest.TestCase):
    def test_backward_checkpoints_drop_the_past(self):
        close = dt.datetime(2026, 9, 1, 17, 0, tzinfo=dt.timezone.utc)
        now = dt.datetime(2026, 8, 30, 12, 0, tzinfo=dt.timezone.utc)
        points = clock.backward_checkpoints(close, [168, 72, 48, 24], now=now)
        self.assertEqual([p.isoformat() for p in points], [
            "2026-08-30T17:00:00+00:00", "2026-08-31T17:00:00+00:00",
        ])

    def test_chase_cadence_exhausts_into_escalation(self):
        cfg = load_config()
        coverage = clock.coverage_for(cfg, "jason")
        waiting_since = dt.datetime(2026, 8, 24, 9, 0, tzinfo=coverage.tz)
        cadence = [24, 48, 96]
        self.assertIsNotNone(clock.next_chase_at(coverage, waiting_since, cadence, 0))
        self.assertIsNotNone(clock.next_chase_at(coverage, waiting_since, cadence, 2))
        self.assertIsNone(
            clock.next_chase_at(coverage, waiting_since, cadence, 3),
            "an exhausted cadence must return None so the caller escalates",
        )


if __name__ == "__main__":
    unittest.main()
