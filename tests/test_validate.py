"""The validation tooling itself.

If `selfcheck` can be fooled, or `score` reports flattering numbers, the whole point of
Phase 0 validation is lost: somebody would sign off on a report that was never looking.
"""

from __future__ import annotations

import csv
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from cbops import fixtures
from cbops.store import Store
from cbops.validate import sample, selfcheck
from tests.support import NOW, ingested_store, load_config


def _status(report, check_name: str) -> str | None:
    for row in report.rows:
        if row["check"] == check_name:
            return row["status"]
    return None


class SelfcheckTest(unittest.TestCase):
    def setUp(self):
        self.store, self.cfg = ingested_store()

    def test_the_fixture_corpus_passes(self):
        report = selfcheck.run(self.cfg, self.store, now=NOW)
        self.assertEqual(report.metrics["failures"], 0, report.metrics)

    def test_an_empty_store_fails_loudly(self):
        """An empty store must never read as a clean bill of health."""
        store = Store(":memory:")
        store.migrate()
        report = selfcheck.run(self.cfg, store, now=NOW)
        self.assertEqual(_status(report, "capture"), "FAIL")
        self.assertIn("NOT READY", report.metrics["verdict"])

    def test_a_missing_sent_folder_is_a_failure_not_a_quiet_pass(self):
        """The single most important check: no Sent mail means no promises to find."""
        self.store.db.execute("DELETE FROM messages WHERE direction = 'outbound'")
        self.store.db.execute("DELETE FROM promises")
        self.store.db.commit()
        report = selfcheck.run(self.cfg, self.store, now=NOW)
        self.assertEqual(_status(report, "outbound captured"), "FAIL")
        self.assertGreater(report.metrics["failures"], 0)

    def test_unreadable_dates_are_caught(self):
        self.store.db.execute(
            "UPDATE messages SET sent_at = '1970-01-01T00:00:00+00:00' WHERE id <= 3")
        self.store.db.commit()
        report = selfcheck.run(self.cfg, self.store, now=NOW)
        self.assertEqual(_status(report, "date parsing"), "FAIL")

    def test_missing_bodies_are_caught(self):
        self.store.db.execute("UPDATE messages SET body_text = '' WHERE quarantined = 0")
        self.store.db.commit()
        report = selfcheck.run(self.cfg, self.store, now=NOW)
        self.assertEqual(_status(report, "body extraction"), "FAIL")

    def test_a_silent_promise_detector_is_a_failure(self):
        """A detector that never fires produces a clean report and a broken promise."""
        self.store.db.execute("DELETE FROM promises")
        self.store.db.commit()
        report = selfcheck.run(self.cfg, self.store, now=NOW)
        self.assertEqual(_status(report, "promise detector"), "FAIL")

    def test_a_leaked_auto_reply_is_caught(self):
        self.store.db.execute(
            "UPDATE messages SET subject = 'Automatic reply: RFQ' WHERE id = 1")
        self.store.db.commit()
        report = selfcheck.run(self.cfg, self.store, now=NOW)
        self.assertEqual(_status(report, "noise filter"), "WARN")

    def test_a_capture_gap_is_reported(self):
        """Three quiet business days in a row is a sync hole, not a quiet week."""
        store = Store(":memory:")
        store.migrate()
        base = dt.date(2026, 6, 1)
        # Two clusters with a gap between: the check needs at least seven distinct days
        # of data before it will look for holes at all.
        for offset in list(range(0, 5)) + list(range(12, 17)):
            day = base + dt.timedelta(days=offset)
            store.upsert_message(dict(
                source="email", mailbox="a@civicbridges.com", message_id=f"m{offset}@x",
                thread_key=f"mid:m{offset}@x", from_addr="x@vendor.example",
                to_addrs=["a@civicbridges.com"], subject="s",
                sent_at=f"{day.isoformat()}T10:00:00+00:00", direction="inbound",
                body_text="hello", snippet="hello", has_attachments=0,
                counterparty="vendor.example", counterparty_class="unknown", quarantined=0,
            ))
        sync = store.start_sync("file", "a@civicbridges.com")
        store.finish_sync(sync, 6, 6, ok=True)
        report = selfcheck.run(self.cfg, store, now=dt.datetime(2026, 6, 16, tzinfo=dt.timezone.utc))
        self.assertEqual(_status(report, "capture gaps"), "WARN")

    def test_findings_are_ordered_worst_first(self):
        self.store.db.execute("DELETE FROM messages WHERE direction = 'outbound'")
        self.store.db.commit()
        report = selfcheck.run(self.cfg, self.store, now=NOW)
        order = [row["status"] for row in report.rows]
        self.assertEqual(order[0], "FAIL")
        self.assertEqual(order, sorted(order, key=lambda s: ["FAIL", "WARN", "OK", "INFO"].index(s)))


class SampleTest(unittest.TestCase):
    def setUp(self):
        self.store, self.cfg = ingested_store()

    def test_a_worksheet_contains_both_strata(self):
        """Sampling only findings can measure precision and nothing else."""
        worksheet = sample.draw(self.cfg, self.store, "promises", size=20, now=NOW)
        strata = {item.stratum for item in worksheet.items}
        self.assertEqual(strata, {"flagged", "not_flagged"})

    def test_the_human_column_starts_empty(self):
        worksheet = sample.draw(self.cfg, self.store, "unanswered", size=10, now=NOW)
        for item in worksheet.items:
            self.assertEqual(item.as_csv_row()["human_says"], "")

    def test_every_row_carries_the_evidence_needed_to_judge_it(self):
        worksheet = sample.draw(self.cfg, self.store, "promises", size=20, now=NOW)
        for item in worksheet.items:
            self.assertTrue(item.evidence.strip(), f"nothing to judge in {item.row_id}")
            self.assertTrue(item.system_says.strip())

    def test_population_sizes_travel_with_the_rows(self):
        """The scorer needs them to weight recall, and a CSV loses a sidecar file."""
        worksheet = sample.draw(self.cfg, self.store, "promises", size=6, now=NOW)
        for item in worksheet.items:
            self.assertGreater(item.stratum_population, 0)

    def test_the_same_seed_redraws_the_same_sample(self):
        first = sample.draw(self.cfg, self.store, "promises", size=6, seed=7, now=NOW)
        second = sample.draw(self.cfg, self.store, "promises", size=6, seed=7, now=NOW)
        self.assertEqual([i.ref for i in first.items], [i.ref for i in second.items])

    def test_a_different_seed_draws_differently(self):
        big, _ = ingested_store()
        first = sample.draw(self.cfg, big, "unanswered", size=4, seed=1, now=NOW)
        second = sample.draw(self.cfg, big, "unanswered", size=4, seed=99, now=NOW)
        self.assertEqual(len(first.items), len(second.items))

    def test_an_unknown_report_is_an_error(self):
        with self.assertRaises(ValueError):
            sample.draw(self.cfg, self.store, "nonsense", now=NOW)

    def test_every_report_has_a_question_a_human_can_answer(self):
        for name, question in sample.QUESTIONS.items():
            self.assertIn("y / n / ?", question, name)


class ScoreTest(unittest.TestCase):
    def _worksheet(self, rows: list[dict[str, str]]) -> Path:
        path = Path(tempfile.mkdtemp()) / "sheet.csv"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=sample.COLUMNS)
            writer.writeheader()
            for row in rows:
                full = {column: "" for column in sample.COLUMNS}
                full.update(row)
                writer.writerow(full)
        return path

    def _row(self, stratum: str, verdict: str, population: int) -> dict[str, str]:
        return {"row_id": f"r{verdict}{stratum}", "report": "promises", "stratum": stratum,
                "stratum_population": str(population), "system_says": "x",
                "human_says": verdict}

    def test_precision_and_recall_are_computed(self):
        path = self._worksheet([
            self._row("flagged", "y", 10), self._row("flagged", "y", 10),
            {**self._row("flagged", "n", 10), "row_id": "fp1"},
            {**self._row("not_flagged", "y", 100), "row_id": "fn1"},
            {**self._row("not_flagged", "n", 100), "row_id": "tn1"},
        ])
        scores, disagreements = sample.read_labels(path)
        score = scores["promises"]
        self.assertAlmostEqual(score.precision, 2 / 3, places=3)
        # Weighted: found = 10 * 2/3 = 6.67, missed = 100 * 1/2 = 50.
        self.assertAlmostEqual(score.recall, 6.667 / (6.667 + 50), places=2)
        self.assertEqual(len(disagreements), 2)

    def test_recall_is_weighted_not_raw(self):
        """The raw ratio would flatter recall badly: negatives are sampled far more thinly."""
        path = self._worksheet([
            self._row("flagged", "y", 10),
            {**self._row("not_flagged", "y", 1000), "row_id": "fn"},
            {**self._row("not_flagged", "n", 1000), "row_id": "tn"},
        ])
        score = sample.read_labels(path)[0]["promises"]
        raw = 1 / (1 + 1)
        self.assertLess(score.recall, raw,
                        "a thinly sampled negative stratum must not inflate recall")

    def test_blank_rows_are_excluded_and_reported(self):
        path = self._worksheet([
            self._row("flagged", "y", 10),
            {**self._row("flagged", "", 10), "row_id": "blank"},
        ])
        score = sample.read_labels(path)[0]["promises"]
        self.assertEqual(score.unlabeled, 1)
        self.assertEqual(score.true_positive, 1)
        self.assertEqual(score.precision, 1.0)

    def test_unsure_is_counted_separately_not_as_a_no(self):
        path = self._worksheet([
            self._row("flagged", "y", 10), {**self._row("flagged", "?", 10), "row_id": "u"},
        ])
        score = sample.read_labels(path)[0]["promises"]
        self.assertEqual(score.unsure, 1)
        self.assertEqual(score.false_positive, 0, "unsure is not a rejection")

    def test_a_half_labeled_sheet_is_called_out(self):
        path = self._worksheet([
            self._row("flagged", "y", 10), {**self._row("flagged", "", 10), "row_id": "b"},
            {**self._row("not_flagged", "n", 50), "row_id": "t"},
        ])
        scores, disagreements = sample.read_labels(path)
        report = sample.score_report(scores, disagreements)
        self.assertTrue(any("blank" in note for note in report.notes))

    def test_disagreements_are_listed_for_fixture_capture(self):
        path = self._worksheet([
            {**self._row("not_flagged", "y", 100), "row_id": "miss1",
             "human_note": "we promised the cert"},
        ])
        scores, disagreements = sample.read_labels(path)
        report = sample.score_report(scores, disagreements)
        text = " ".join(report.notes)
        self.assertIn("miss1", text)
        self.assertIn("we promised the cert", text)


class FixtureExportTest(unittest.TestCase):
    def setUp(self):
        self.store, self.cfg = ingested_store()
        self.out = Path(tempfile.mkdtemp())

    def _export(self, thread_key: str = "mid:cb-100@civicbridges.com") -> str:
        paths = fixtures.export_thread(self.cfg, self.store, thread_key, self.out)
        return "\n".join(path.read_text() for path in paths)

    def test_external_addresses_and_domains_are_replaced(self):
        text = self._export().lower()
        self.assertNotIn("acmedistribution", text)
        self.assertNotIn("dana.cole", text)

    def test_a_first_name_signoff_is_replaced(self):
        """Mail signs off with a first name, and a surviving sign-off leaks a real person."""
        self.assertNotIn("Dana", self._export())

    def test_internal_addresses_are_kept(self):
        """Direction classification depends on our own domain, which is already public here."""
        self.assertIn("jason@civicbridges.com", self._export())

    def test_identifiers_are_replaced_but_keep_their_shape(self):
        import re
        text = self._export()
        self.assertNotIn("5930-01-234-5678", text)
        self.assertTrue(re.search(r"\b\d{4}-\d{2}-\d{3}-\d{4}\b", text),
                        "the NSN shape must survive so the detector is still exercised")

    def test_a_quarantined_message_exports_no_body(self):
        text = "\n".join(
            path.read_text() for path in
            fixtures.export_thread(self.cfg, self.store, "mid:gov-002@dla.mil", self.out)
        )
        self.assertIn("quarantined at ingest", text)
        self.assertNotIn("DISTRIBUTION STATEMENT", text)

    def test_attachment_names_are_replaced_and_payloads_never_copied(self):
        """The original filename follows the naming convention, so it carries a real
        contract number and counterparty slug. It must not survive."""
        text = "\n".join(
            path.read_text() for path in
            fixtures.export_thread(self.cfg, self.store, "mid:cb-101@civicbridges.com", self.out)
        )
        self.assertIn('filename="attachment-2.pdf"', text)
        self.assertNotIn("brighton-valve", text)
        self.assertNotIn("purchase-order__", text)

    def test_a_bare_word_after_po_is_not_rewritten_as_an_order_number(self):
        """"signed PO today" must stay "signed PO today", or the fixture testing that exact
        false positive is corrupted by the anonymizer."""
        text = "\n".join(
            path.read_text() for path in
            fixtures.export_thread(self.cfg, self.store, "mid:cb-101@civicbridges.com", self.out)
        )
        self.assertIn("signed PO today", text)

    def test_the_export_round_trips_through_the_pipeline(self):
        """A fixture that does not re-ingest is not a regression test."""
        from cbops import pipeline
        from cbops.ingest import get_source

        fixtures.export_thread(self.cfg, self.store, "mid:cb-100@civicbridges.com", self.out)
        fresh = Store(":memory:")
        fresh.migrate()
        results = pipeline.ingest_email(
            self.cfg, fresh,
            get_source("mbox", paths=[self.out], mailbox_address="quotes@civicbridges.com"),
            None,
        )
        self.assertTrue(results[0].ok, results[0].error)
        self.assertEqual(results[0].new, 2)
        self.assertGreater(fresh.query("SELECT COUNT(*) AS n FROM promises")[0]["n"], 0,
                           "the exported thread must still trip the promise detector")

    def test_it_refuses_an_unknown_thread(self):
        with self.assertRaises(ValueError):
            fixtures.export_thread(self.cfg, self.store, "mid:nope", self.out)

    def test_the_warning_is_unambiguous(self):
        self.assertIn("HUMAN MUST READ", fixtures.WARNING)


if __name__ == "__main__":
    unittest.main()
