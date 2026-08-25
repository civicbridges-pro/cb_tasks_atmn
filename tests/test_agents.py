"""Phase 1 agents: triage, routing, cadence, audit.

The assertions here are about judgment, not plumbing. Each one states a rule the system is
supposed to follow when it is unsure, because that is where an automation like this either
earns trust or loses it.
"""

from __future__ import annotations

import datetime as dt
import unittest

from cbops.agents import auditor, chaser, router, triage
from cbops.config import TRIAGE_QUEUE
from cbops.store import LedgerError, Store
from tests.support import NOW, ingested_store, ledger_store, load_config, phase1_config


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_direction_separates_a_vendor_rfq_from_a_solicitation(self):
        """Both lanes legitimately own the word "rfq". The message's own facts decide."""
        text = "RFQ: requesting price and lead time on NSN 5930-01-234-5678"
        outbound, _, _ = triage.classify_type(self.cfg, text, "outbound", "oem")
        inbound_gov, _, _ = triage.classify_type(self.cfg, text, "inbound", "gov")
        self.assertEqual(outbound, "vendor_quote")
        self.assertEqual(inbound_gov, "solicitation")

    def test_longest_term_wins(self):
        found, _, _ = triage.classify_type(self.cfg, "This is a stop work order", "inbound", "gov")
        self.assertEqual(found, "stop_work")

    def test_a_bare_generic_word_no_longer_pulls_the_logistics_lane(self):
        """"delivery" appears in nearly every contract email."""
        found, _, _ = triage.classify_type(
            self.cfg, "Please confirm the delivery of your quote", "inbound", "unknown"
        )
        self.assertNotEqual(found, "delivery")

    def test_a_term_ending_in_punctuation_still_matches(self):
        found, _, _ = triage.classify_type(self.cfg, "PO# CB-1099 signed", "outbound", "oem")
        self.assertEqual(found, "purchase_order")

    def test_ambiguity_lowers_confidence(self):
        clear = triage.classify_type(self.cfg, "stop work order issued", "inbound", "gov")[1]
        murky = triage.classify_type(
            self.cfg, "invoice and packing slip for this shipment", "inbound", "unknown"
        )[1]
        self.assertLess(murky, clear)

    def test_no_match_returns_nothing_rather_than_a_guess(self):
        self.assertEqual(
            triage.classify_type(self.cfg, "Lunch on Thursday?", "internal", "internal"),
            (None, 0.0, []),
        )

    def test_rules_confidence_never_clears_the_action_threshold(self):
        minimum = self.cfg.guardrails["confidence"]["min_to_act"]
        for text in ("stop work order, cure notice, show cause, termination",
                     "purchase order po issued vendor po signed po"):
            self.assertLess(triage.classify_type(self.cfg, text, "inbound", "gov")[1], minimum)


class CandidateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store, cls.cfg = ingested_store()
        cls.by_type = {}
        for candidate in triage.scan(cls.cfg, cls.store, now=NOW):
            cls.by_type.setdefault(candidate.type, []).append(candidate)

    def test_an_acknowledgment_creates_nothing(self):
        threads = {c.thread_key for group in self.by_type.values() for c in group}
        closer = self.store.query(
            "SELECT thread_key FROM messages WHERE from_addr = 'sam.ortiz@brightonvalve.example'"
        )[0]["thread_key"]
        # The thread carries a real obligation from the PO promise, but the "Got it, thanks"
        # message itself must not have opened a second one.
        self.assertLessEqual(
            sum(1 for group in self.by_type.values() for c in group if c.thread_key == closer), 1
        )

    def test_a_quarantined_message_is_flagged_for_a_human_and_never_classified(self):
        quarantined = [
            c for group in self.by_type.values() for c in group
            if "Quarantined" in c.what_is_owed
        ]
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].confidence, 0.0)
        self.assertTrue(quarantined[0].needs_human_review)

    def test_an_outbound_commitment_lands_on_us(self):
        vendor = self.by_type["vendor_quote"]
        promised = [c for c in vendor if "letter of supply" in c.what_is_owed]
        self.assertEqual(promised[0].direction, "we_owe_them")

    def test_an_outbound_request_makes_them_owe_us(self):
        marotta = [
            c for group in self.by_type.values() for c in group
            if c.counterparty == "marottacontrols.example"
        ]
        self.assertEqual(marotta[0].direction, "they_owe_us")

    def test_a_stated_date_beats_the_sla_clock(self):
        promised = [
            c for group in self.by_type.values() for c in group
            if c.due_basis and "stated in the message" in c.due_basis
        ]
        self.assertTrue(promised, "a date the sender wrote must win over a computed clock")

    def test_one_obligation_per_thread_and_type(self):
        seen = [(c.thread_key, c.type) for group in self.by_type.values() for c in group]
        self.assertEqual(len(seen), len(set(seen)))

    def test_rerunning_triage_creates_no_duplicates(self):
        cfg = phase1_config()
        store, _ = ingested_store()
        first = triage.scan(cfg, store, now=NOW)
        router.build(cfg, store, first, now=NOW, persist=True)
        second = triage.scan(cfg, store, now=NOW)
        self.assertEqual(second, [], "a second pass must not re-open what it already recorded")


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.store, self.cfg = ingested_store()

    def _route(self, **overrides):
        candidate = triage.Candidate(
            message_id=1, thread_key="t1", type=overrides.pop("type", "vendor_quote"),
            what_is_owed="quote on NSN 5930-01-234-5678", direction="they_owe_us",
            counterparty="vendor.example", counterparty_class="oem", contract_ref=None,
            source="email", source_ref="t1#1", confidence=overrides.pop("confidence", 0.80),
            **overrides,
        )
        candidate.needs_human_review = self.cfg.needs_human_review(
            candidate.type, candidate.confidence
        )
        return router.route(self.cfg, self.store, candidate, now=NOW)

    def test_low_confidence_goes_to_the_queue_not_to_a_person(self):
        routed = self._route(confidence=0.40)
        self.assertEqual(routed.owner, TRIAGE_QUEUE)
        self.assertTrue(any("triage queue" in note for note in routed.notes))

    def test_confident_candidates_reach_the_lane_owner(self):
        self.assertEqual(self._route(confidence=0.80).owner, "jason")

    def test_a_high_consequence_type_reaches_its_owner_and_is_still_flagged(self):
        """Review is not the same as unowned: stop-work needs an owner *and* a human."""
        routed = self._route(type="stop_work", confidence=0.50)
        self.assertEqual(routed.owner, "taj")
        self.assertTrue(routed.candidate.needs_human_review)

    def test_a_paging_type_fans_out_past_its_offset_hours_owner(self):
        routed = self._route(type="stop_work", confidence=0.90)
        self.assertIn("taj", routed.page_now)
        self.assertIn("doug", routed.page_now)
        self.assertIn("anna", routed.page_now)

    def test_they_owe_us_opens_waiting_with_a_clock(self):
        routed = self._route()
        self.assertEqual(routed.status, "waiting_external")
        self.assertIsNotNone(routed.waiting_since)
        self.assertIsNotNone(routed.next_chase_at, "waiting on a vendor is not done")

    def test_we_owe_them_opens_without_a_chase(self):
        candidate = triage.Candidate(
            message_id=1, thread_key="t2", type="invoice", what_is_owed="pay it",
            direction="we_owe_them", counterparty="x.example", counterparty_class="service",
            contract_ref=None, source="email", source_ref="t2#1", confidence=0.80,
        )
        routed = router.route(self.cfg, self.store, candidate, now=NOW)
        self.assertEqual(routed.status, "open")
        self.assertIsNone(routed.next_chase_at)

    def test_an_unconfirmed_coverage_window_is_disclosed(self):
        routed = self._route(type="contract_action", confidence=0.90)   # owner taj
        self.assertFalse(routed.coverage_known)
        self.assertTrue(any("unconfirmed" in note for note in routed.notes))

    def test_a_known_deadline_beats_the_fixed_cadence(self):
        """The Marotta fix, asserted."""
        store, cfg = ingested_store()
        marotta = [
            c for c in triage.scan(cfg, store, now=NOW)
            if c.counterparty == "marottacontrols.example"
        ][0]
        routed = router.route(cfg, store, marotta, now=NOW)
        self.assertTrue(any("backward from" in note for note in routed.notes))

    def test_a_missing_deadline_says_so_instead_of_pretending(self):
        routed = self._route()
        self.assertTrue(any("no external deadline" in note for note in routed.notes))

    def test_commit_records_the_originating_message_as_evidence(self):
        cfg = phase1_config()
        routed = self._route()
        obligation_id = router.commit(cfg, self.store, routed)
        self.assertTrue(self.store.has_evidence(obligation_id))

    def test_commit_refuses_an_owner_the_store_will_not_accept(self):
        cfg = phase1_config()
        routed = self._route()
        routed.owner = "procurement"
        with self.assertRaises(LedgerError):
            router.commit(cfg, self.store, routed)


class ChaserTest(unittest.TestCase):
    def setUp(self):
        self.store, self.cfg, _ = ledger_store()

    def _advance_to_first_chase(self):
        rows = self.store.query(
            "SELECT id, next_chase_at FROM obligations WHERE next_chase_at IS NOT NULL "
            "ORDER BY next_chase_at LIMIT 1"
        )
        self.assertTrue(rows, "the fixture ledger should contain a waiting obligation")
        from cbops.store import parse_ts
        return rows[0]["id"], parse_ts(rows[0]["next_chase_at"]) + dt.timedelta(minutes=1)

    def test_nothing_is_due_before_its_clock_fires(self):
        self.assertEqual(chaser.plan(self.cfg, self.store, now=NOW), [])

    def test_a_due_clock_produces_a_chase(self):
        _, when = self._advance_to_first_chase()
        actions = chaser.plan(self.cfg, self.store, now=when)
        self.assertTrue(actions)
        self.assertEqual(actions[0].action, "chase")
        self.assertEqual(actions[0].chase_number, 1)

    def test_applying_a_chase_advances_the_count_and_audits_it(self):
        obligation_id, when = self._advance_to_first_chase()
        chaser.apply(self.cfg, self.store, chaser.plan(self.cfg, self.store, now=when))
        row = self.store.query(
            "SELECT chase_count FROM obligations WHERE id = ?", (obligation_id,))[0]
        self.assertEqual(row["chase_count"], 1)
        audit = self.store.query(
            "SELECT field FROM obligation_audit WHERE obligation_id = ?", (obligation_id,))
        self.assertIn("chase_count", [r["field"] for r in audit])

    def test_an_exhausted_cadence_escalates_rather_than_chasing_forever(self):
        obligation_id, when = self._advance_to_first_chase()
        # Past the external deadline: no checkpoints remain, so chasing is over.
        past_deadline = when + dt.timedelta(days=30)
        actions = chaser.plan(self.cfg, self.store, now=past_deadline)
        self.assertTrue(actions)
        self.assertIn(actions[0].action, ("escalate", "exhausted"))
        if actions[0].action == "escalate":
            self.assertNotEqual(actions[0].escalate_to, actions[0].owner)

    def test_escalation_walks_the_path_and_then_stops(self):
        obligation_id, when = self._advance_to_first_chase()
        past = when + dt.timedelta(days=30)
        seen = []
        for _ in range(6):
            actions = [a for a in chaser.plan(self.cfg, self.store, now=past)
                       if a.obligation_id == obligation_id]
            if not actions:
                break
            seen.append(actions[0].escalate_to)
            chaser.apply(self.cfg, self.store, actions)
            self.store.db.execute(
                "UPDATE obligations SET next_chase_at = ? WHERE id = ?",
                (past.isoformat(), obligation_id))
            self.store.db.commit()
        self.assertIn(None, seen, "the path must run out instead of escalating forever")
        named = [person for person in seen if person]
        self.assertEqual(len(named), len(set(named)), "never escalate to the same person twice")


class AuditorTest(unittest.TestCase):
    def setUp(self):
        self.store, self.cfg, _ = ledger_store()

    def _checks(self, report):
        return {row["check"] for row in report.rows}

    def test_a_close_without_evidence_is_critical(self):
        self.store.db.execute("UPDATE obligations SET status = 'done' WHERE id = 1")
        self.store.db.execute("DELETE FROM evidence WHERE obligation_id = 1")
        self.store.db.commit()
        report = auditor.run(self.cfg, self.store, now=NOW)
        self.assertIn("closed_without_evidence", self._checks(report))
        self.assertGreater(report.metrics["critical"], 0)

    def test_an_owner_removed_from_config_orphans_the_obligation(self):
        self.store.db.execute("UPDATE obligations SET owner = 'ghost' WHERE id = 1")
        self.store.db.commit()
        self.assertIn("unknown_owner", self._checks(auditor.run(self.cfg, self.store, now=NOW)))

    def test_a_group_owner_is_caught_after_the_fact_too(self):
        self.store.db.execute("UPDATE obligations SET owner = 'procurement' WHERE id = 1")
        self.store.db.commit()
        self.assertIn("group_owner", self._checks(auditor.run(self.cfg, self.store, now=NOW)))

    def test_a_high_consequence_obligation_without_a_review_flag_is_critical(self):
        self.store.db.execute(
            "UPDATE obligations SET needs_human_review = 0 WHERE type = 'contract_action'")
        self.store.db.commit()
        self.assertIn("high_consequence_unreviewed",
                      self._checks(auditor.run(self.cfg, self.store, now=NOW)))

    def test_a_deadline_driven_type_with_no_date_is_reported(self):
        self.assertIn("no_external_deadline",
                      self._checks(auditor.run(self.cfg, self.store, now=NOW)))

    def test_stale_capture_is_critical(self):
        much_later = NOW + dt.timedelta(days=2)
        self.assertIn("stale_capture",
                      self._checks(auditor.run(self.cfg, self.store, now=much_later)))

    def test_an_empty_ledger_reports_no_capture_rather_than_all_clear(self):
        store = Store(":memory:")
        store.migrate()
        report = auditor.run(self.cfg, store, now=NOW)
        self.assertIn("no_capture", self._checks(report))

    def test_a_failing_check_does_not_silence_the_others(self):
        broken = ("boom", lambda cfg, store, now: (_ for _ in ()).throw(RuntimeError("boom")))
        original = list(auditor.CHECKS)
        auditor.CHECKS.append(broken)
        try:
            report = auditor.run(self.cfg, self.store, now=NOW)
        finally:
            auditor.CHECKS[:] = original
        self.assertIn("check_failed", self._checks(report))
        self.assertGreater(len(report.rows), 1)

    def test_nothing_is_repaired_automatically(self):
        """Guardrail 3. The auditor reports; a human decides."""
        before = self.store.query("SELECT id, status, owner FROM obligations ORDER BY id")
        auditor.run(self.cfg, self.store, now=NOW)
        after = self.store.query("SELECT id, status, owner FROM obligations ORDER BY id")
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])


if __name__ == "__main__":
    unittest.main()


class VetoAndPreferenceTest(unittest.TestCase):
    """The mechanism that separates lanes sharing vocabulary."""

    def setUp(self):
        self.cfg = load_config()

    def test_a_preference_clause_is_conjunctive(self):
        """"prefer when inbound and from gov" is one condition, not two nudges.

        An inbound reply from a distributor about our RFQ satisfies neither half on its
        own, so it stays ambiguous and lands with a human rather than in the federal
        solicitation lane.
        """
        gov = triage.classify_type(self.cfg, "RFQ: quotes due 9/4", "inbound", "gov")
        distributor = triage.classify_type(
            self.cfg, "Re: RFQ CB-1042. Can you advise?", "inbound", "unknown"
        )
        self.assertEqual(gov[0], "solicitation")
        self.assertGreater(gov[1], distributor[1])
        threshold = self.cfg.guardrails["confidence"]["min_to_record_without_review"]
        self.assertLess(distributor[1], threshold,
                        "an ambiguous counterparty should route to a human")

    def test_a_veto_removes_a_lane_that_cannot_apply(self):
        """A vendor quote to a contracting officer is not a vendor quote."""
        _, _, why = triage.classify_type(
            self.cfg, "RFQ: requesting price and lead time", "inbound", "gov"
        )
        self.assertTrue(any("ruled out: vendor_quote" in reason for reason in why))

    def test_a_vetoed_lane_still_wins_uncontested_at_lower_confidence(self):
        """Better a flagged classification than silently classifying as nothing."""
        found, confidence, _ = triage.classify_type(
            self.cfg, "authorized distributor letter of supply", "inbound", "gov"
        )
        self.assertEqual(found, "vendor_quote")
        self.assertLess(confidence,
                        triage.classify_type(
                            self.cfg, "authorized distributor letter of supply",
                            "outbound", "oem")[1])

    def test_a_short_identifier_counts_as_substantive(self):
        """"po#" is the subject of the sentence, not a word that happens to appear in it."""
        threshold = self.cfg.guardrails["confidence"]["min_to_record_without_review"]
        found, confidence, _ = triage.classify_type(self.cfg, "PO# CB-1099 signed",
                                                    "outbound", "oem")
        self.assertEqual(found, "purchase_order")
        self.assertGreaterEqual(confidence, threshold)

    def test_two_substantive_lanes_fall_below_the_routing_threshold(self):
        threshold = self.cfg.guardrails["confidence"]["min_to_record_without_review"]
        _, confidence, _ = triage.classify_type(
            self.cfg, "invoice and packing slip for this shipment", "inbound", "unknown"
        )
        self.assertLess(confidence, threshold)

    def test_a_single_clear_match_clears_the_threshold(self):
        threshold = self.cfg.guardrails["confidence"]["min_to_record_without_review"]
        _, confidence, _ = triage.classify_type(self.cfg, "this is a stop work order",
                                                "inbound", "gov")
        self.assertGreaterEqual(confidence, threshold)


class SeverityTest(unittest.TestCase):
    """Critical means the ledger cannot be trusted, not that somebody is behind."""

    def setUp(self):
        self.store, self.cfg, _ = ledger_store()

    def test_late_work_is_never_critical(self):
        very_late = NOW + dt.timedelta(days=60)
        report = auditor.run(self.cfg, self.store, now=very_late)
        past_due = [row for row in report.rows if row["check"] == "past_due"]
        self.assertTrue(past_due, "the fixture ledger should have overdue obligations")
        for row in past_due:
            self.assertNotEqual(row["severity"], "critical", row["detail"])

    def test_a_broken_invariant_is_critical(self):
        self.store.db.execute("UPDATE obligations SET owner = 'procurement' WHERE id = 1")
        self.store.db.commit()
        report = auditor.run(self.cfg, self.store, now=NOW)
        group = [row for row in report.rows if row["check"] == "group_owner"]
        self.assertEqual(group[0]["severity"], "critical")

    def test_a_clean_ledger_with_late_work_exits_zero(self):
        """The audit exit code must mean 'do not trust this', not 'somebody is behind'."""
        report = auditor.run(self.cfg, self.store, now=NOW + dt.timedelta(days=60))
        integrity = [row for row in report.rows if row["severity"] == "critical"]
        self.assertTrue(
            all(row["check"] in {"stale_capture", "no_capture", "config_error"}
                for row in integrity),
            f"unexpected critical checks: {[r['check'] for r in integrity]}",
        )
