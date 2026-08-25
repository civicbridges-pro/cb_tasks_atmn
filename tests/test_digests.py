"""Digests and the `owed` view.

Phase 1's visible output. A correct ledger that nobody reads changes nothing, so what
appears on a digest, in what order, is a design decision worth testing.
"""

from __future__ import annotations

import datetime as dt
import unittest

from cbops import ledger_view
from cbops.digests import exec_rollup, personal
from cbops.reports.render import to_markdown
from tests.support import NOW, ledger_store


class PersonalDigestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store, cls.cfg, _ = ledger_store()

    def _digest(self, person: str):
        return personal.run(self.cfg, self.store, person, now=NOW)

    def test_a_person_sees_only_their_own_obligations(self):
        report = self._digest("jason")
        owners = {
            row["owner"] for row in self.store.query(
                "SELECT owner FROM obligations WHERE id IN ("
                + ",".join(str(row["obligation"]) for row in report.rows) + ")"
            )
        } if report.rows else set()
        self.assertEqual(owners, {"jason"})

    def test_overdue_sorts_above_everything_else(self):
        report = self._digest("jason")
        buckets = [row["act"] for row in report.rows]
        if "OVERDUE" in buckets and "UPCOMING" in buckets:
            self.assertLess(buckets.index("OVERDUE"), buckets.index("UPCOMING"))

    def test_a_review_flag_outranks_a_due_date(self):
        """A misclassified obligation has to be corrected before its clock means anything."""
        report = self._digest("taj")
        self.assertTrue(report.rows)
        self.assertEqual(report.rows[0]["act"], "REVIEW")

    def test_an_unconfirmed_coverage_window_is_disclosed_to_the_person(self):
        report = self._digest("taj")
        self.assertTrue(any("coverage hours" in note for note in report.notes))

    def test_an_empty_digest_says_so_rather_than_rendering_blank(self):
        report = self._digest("amanda")
        self.assertEqual(report.rows, [])
        self.assertTrue(any("Nothing open" in note for note in report.notes))

    def test_the_triage_queue_gets_its_own_digest(self):
        reports = {report.key: report for report in personal.everyone(self.cfg, self.store, NOW)}
        self.assertIn("digest-triage_queue", reports)
        queue = reports["digest-triage_queue"]
        self.assertTrue(queue.rows, "the fixture ledger has unowned obligations")
        self.assertTrue(any("owned by nobody" in note for note in queue.notes))

    def test_everyone_in_all_owners_gets_a_digest(self):
        keys = {report.key for report in personal.everyone(self.cfg, self.store, NOW)}
        for person in self.cfg.group("all_owners"):
            self.assertIn(f"digest-{person}", keys)

    def test_waiting_is_labelled_as_still_carrying_a_clock(self):
        report = self._digest("jason")
        if any(row["act"] == "WAITING" for row in report.rows):
            self.assertTrue(any("still has a clock" in note for note in report.notes))


class ExecRollupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store, cls.cfg, _ = ledger_store()
        cls.report = exec_rollup.run(cls.cfg, cls.store, now=NOW)

    def test_trust_rows_come_first(self):
        priorities = [row["priority"] for row in self.report.rows]
        if "TRUST" in priorities:
            self.assertEqual(priorities[0], "TRUST")

    def test_targets_that_should_be_zero_are_labelled(self):
        labels = " ".join(self.report.metrics)
        self.assertIn("target zero", labels)

    def test_unowned_work_is_surfaced_as_a_decision(self):
        rows = [row for row in self.report.rows if row["item"] == "unowned obligations"]
        self.assertTrue(rows, "unowned obligations must reach the exec rollup")

    def test_load_by_owner_is_reported(self):
        self.assertIn("load by owner", self.report.metrics)

    def test_the_success_test_is_pointed_at(self):
        self.assertTrue(any("./cb owed" in note for note in self.report.notes))


class OwedViewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store, cls.cfg, _ = ledger_store()

    def test_it_answers_what_we_owe_and_what_is_owed_to_us(self):
        report = ledger_view.run(self.cfg, self.store, now=NOW)
        self.assertGreater(report.metrics["we owe them"], 0)
        self.assertGreater(report.metrics["they owe us"], 0)
        self.assertEqual(
            report.metrics["we owe them"] + report.metrics["they owe us"],
            report.metrics["open obligations"],
        )

    def test_overdue_sorts_to_the_top(self):
        report = ledger_view.run(self.cfg, self.store, now=NOW)
        overdue = [i for i, row in enumerate(report.rows) if "overdue" in row["due"]]
        others = [i for i, row in enumerate(report.rows) if row["due"] == "no date"]
        if overdue and others:
            self.assertLess(max(overdue), min(others))

    def test_every_row_names_a_next_move(self):
        report = ledger_view.run(self.cfg, self.store, now=NOW)
        for row in report.rows:
            self.assertTrue(row["next_move"], f"no next move on {row}")

    def test_waiting_rows_say_chase_or_escalate(self):
        report = ledger_view.run(self.cfg, self.store, now=NOW)
        for row in report.rows:
            if row["state"] == "waiting_external":
                self.assertTrue(
                    "chase" in row["next_move"] or "escalate" in row["next_move"],
                    f"waiting obligation with no chase or escalation: {row}",
                )

    def test_filters_narrow_the_view(self):
        everything = ledger_view.run(self.cfg, self.store, now=NOW)
        jason = ledger_view.run(self.cfg, self.store, now=NOW, owner="jason")
        self.assertLess(jason.row_count, everything.row_count)
        self.assertTrue(all(row["owner"] == "jason" for row in jason.rows))

    def test_undated_obligations_are_called_out_as_a_gap(self):
        report = ledger_view.run(self.cfg, self.store, now=NOW)
        if report.metrics["no due date"]:
            self.assertTrue(any("no due date" in note for note in report.notes))

    def test_it_renders(self):
        text = to_markdown(ledger_view.run(self.cfg, self.store, now=NOW))
        self.assertIn("What this company owes", text)
        self.assertIn("Data freshness", text)


class PhaseGateTest(unittest.TestCase):
    def test_committing_requires_phase_1_to_be_declared(self):
        from cbops.cli import _require_phase_1
        from cbops.config import ConfigError
        from tests.support import load_config, phase1_config

        with self.assertRaises(ConfigError) as caught:
            _require_phase_1(load_config(), "triage --commit")
        self.assertIn("preview", str(caught.exception))
        _require_phase_1(phase1_config(), "triage --commit")   # must not raise


if __name__ == "__main__":
    unittest.main()
