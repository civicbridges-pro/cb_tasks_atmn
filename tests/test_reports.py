"""End to end: the fixture corpus through the real pipeline into the four reports.

These are the tests that matter most. Every fixture exists because it represents a failure
mode the business confirmed is active, and each assertion below states what the report is
supposed to notice about it.
"""

from __future__ import annotations

import unittest

from cbops.reports import baseline, handoffs, promises, render, unanswered, vendor_silence
from tests.support import NOW, ingested_store, rows_by


class IngestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store, cls.cfg = ingested_store()

    def test_noise_is_excluded_and_cui_is_quarantined(self):
        counts = {
            "total": self.store.query("SELECT COUNT(*) AS n FROM messages")[0]["n"],
            "quarantined": self.store.query(
                "SELECT COUNT(*) AS n FROM messages WHERE quarantined = 1")[0]["n"],
        }
        self.assertEqual(counts["quarantined"], 1)
        addresses = [
            r["from_addr"] for r in self.store.query("SELECT from_addr FROM messages")
        ]
        self.assertEqual(
            sum(1 for a in addresses if a == "dana.cole@acmedistribution.example"), 1,
            "the out of office reply should not have been stored",
        )

    def test_replies_land_in_their_parent_thread(self):
        row = self.store.query(
            "SELECT message_count FROM threads WHERE thread_key = 'mid:cb-101@civicbridges.com'"
        )[0]
        self.assertEqual(row["message_count"], 3)

    def test_solicitation_close_date_is_captured_from_prose(self):
        row = self.store.query(
            "SELECT solicitation_close_at, solicitation_ref FROM threads "
            "WHERE thread_key = 'mid:cb-103@civicbridges.com'"
        )[0]
        self.assertTrue(row["solicitation_close_at"].startswith("2026-08-27"))
        self.assertEqual(row["solicitation_ref"], "SPE4A6-25-T-4567")


class UnansweredTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store, cfg = ingested_store()
        cls.report = unanswered.run(cfg, store, now=NOW)

    def test_government_thread_is_first(self):
        """Sorted by counterparty importance, never by age. This is the whole design."""
        self.assertEqual(self.report.rows[0]["class"], "gov")

    def test_sled_thread_is_found_but_ranked_below_government(self):
        classes = [row["class"] for row in self.report.rows]
        self.assertIn("customer_sled", classes)
        self.assertLess(classes.index("gov"), classes.index("customer_sled"))

    def test_an_acknowledgment_is_excluded_and_disclosed(self):
        threads = {row["thread"] for row in self.report.rows}
        self.assertNotIn("mid:cb-101@civicbridges.com", threads)
        self.assertEqual(self.report.metrics["excluded as closers"], 1)
        self.assertTrue(
            any("spot-check" in note for note in self.report.notes),
            "exclusions must be disclosed, not silent",
        )

    def test_a_thread_we_already_answered_is_not_listed(self):
        threads = {row["thread"] for row in self.report.rows}
        self.assertNotIn("mid:cust-500@northstarutility.example", threads)

    def test_a_quarantined_thread_still_counts(self):
        """The body is gone, the obligation is not."""
        threads = {row["thread"] for row in self.report.rows}
        self.assertIn("mid:gov-002@dla.mil", threads)

    def test_an_inbound_message_with_no_question_still_counts(self):
        row = rows_by(self.report, "thread")["mid:gov-002@dla.mil"]
        self.assertEqual(row["asked"], "no")


class PromiseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store, cfg = ingested_store()
        cls.report = promises.run(cfg, store, now=NOW)

    def test_a_promise_with_a_passed_date_and_a_live_thread_is_broken(self):
        broken = [row for row in self.report.rows if row["status"] == "broken"]
        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["promised_by"], "jason@civicbridges.com")

    def test_a_promise_delivered_with_an_attachment_is_kept_and_not_reported(self):
        threads = {row["thread"] for row in self.report.rows}
        self.assertNotIn("mid:cb-101@civicbridges.com", threads)
        self.assertEqual(self.report.metrics["kept"], 1)

    def test_an_undated_promise_is_unknown_never_broken(self):
        undated = [row for row in self.report.rows if row["due"] == "(no date)"]
        self.assertTrue(undated)
        for row in undated:
            self.assertEqual(row["status"], "unknown",
                             "a promise with no date cannot be breached")

    def test_undated_commitments_are_counted_as_their_own_problem(self):
        self.assertGreater(self.report.metrics["promises with no date at all"], 0)


class HandoffTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store, cfg = ingested_store()
        cls.report = handoffs.run(cfg, store, now=NOW)

    def test_the_overseas_handoff_is_found_and_labelled(self):
        row = rows_by(self.report, "to")["taj"]
        self.assertIn("overseas", row["seam"])
        self.assertIn("proc→logistics", row["seam"])

    def test_degraded_clocks_are_labelled_not_hidden(self):
        row = rows_by(self.report, "to")["taj"]
        self.assertIn("coverage unknown", row["seam"])

    def test_both_seams_are_counted(self):
        self.assertEqual(self.report.metrics["crossing the overseas boundary"], 1)
        self.assertEqual(self.report.metrics["crossing procurement to logistics"], 1)


class VendorSilenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store, cfg = ingested_store()
        cls.report = vendor_silence.run(cfg, store, now=NOW)

    def test_the_deadline_driven_row_outranks_the_older_silent_one(self):
        """The Marotta pattern: least runway first, not longest wait first."""
        first = self.report.rows[0]
        self.assertEqual(first["counterparty"], "marottacontrols.example")
        older = rows_by(self.report, "counterparty")["pioneerspares.example"]
        self.assertGreater(
            float(older["silent_for"].rstrip("h")), float(first["silent_for"].rstrip("h")),
            "the lower ranked row should be the one that waited longer",
        )

    def test_urgency_comes_from_the_solicitation_clock(self):
        row = rows_by(self.report, "counterparty")["marottacontrols.example"]
        self.assertEqual(row["urgency"], "high")     # closes in 50 hours
        # Two backward-planned checkpoints are still in the future: 48h and 24h before close.
        self.assertEqual(row["chases_left"], "2")

    def test_a_missing_close_date_is_itself_reported(self):
        row = rows_by(self.report, "counterparty")["pioneerspares.example"]
        self.assertEqual(row["closes_in"], "unknown")
        self.assertEqual(self.report.metrics["with a known external deadline"], 1)


class BaselineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store, cfg = ingested_store()
        cls.report = baseline.run(cfg, store, now=NOW)

    def test_median_first_response_is_measured(self):
        row = rows_by(self.report, "metric")["median first response, all mailboxes"]
        self.assertEqual(row["value"], "5.0h")

    def test_unmeasurable_is_reported_as_unmeasurable_not_zero(self):
        """Zero and unmeasurable look identical on a dashboard and mean opposites."""
        row = rows_by(self.report, "metric")["award to vendor PO issued"]
        self.assertEqual(row["value"], "unmeasurable")

    def test_every_external_thread_is_unowned_in_phase_zero(self):
        row = rows_by(self.report, "metric")["external threads with no owner"]
        self.assertIn("100%", str(row["value"]))


class FreshnessTest(unittest.TestCase):
    def test_a_stale_sync_puts_a_warning_at_the_top(self):
        import datetime as dt
        store, cfg = ingested_store()
        much_later = NOW + dt.timedelta(days=3)
        report = unanswered.run(cfg, store, now=much_later)
        self.assertTrue(report.stale)
        text = render.to_markdown(report)
        self.assertIn("Data freshness warning", text)
        self.assertLess(text.index("Data freshness warning"), text.index("## Findings"))

    def test_a_report_with_no_sync_at_all_says_so(self):
        from cbops.store import Store
        from tests.support import load_config
        store = Store(":memory:")
        store.migrate()
        report = unanswered.run(load_config(), store, now=NOW)
        self.assertTrue(report.stale)
        self.assertIn("No sync has ever run", " ".join(report.freshness))


if __name__ == "__main__":
    unittest.main()
