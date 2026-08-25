"""Detector behavior. Recall matters more than precision, and both are asserted."""

from __future__ import annotations

import datetime as dt
import unittest

from cbops.extract import rules
from tests.support import load_config

TUESDAY = dt.datetime(2026, 8, 25, 9, 0, tzinfo=dt.timezone.utc)


class CommitmentTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def _first(self, text: str):
        found = rules.find_commitments(text, TUESDAY)
        return found[0] if found else None

    def test_contraction_is_detected(self):
        """The most common real promise shape. A \\s+ here silently drops all of them."""
        found = self._first("I'll send the quote Friday.")
        self.assertIsNotNone(found)
        self.assertEqual(found.due.at.date(), dt.date(2026, 8, 28))

    def test_smart_apostrophe_is_detected(self):
        self.assertIsNotNone(self._first("We’ll get that over to you Monday."))

    def test_hedged_promise_is_kept_but_scored_lower(self):
        firm = self._first("I'll send the quote tomorrow.")
        hedged = self._first("I'll try to send the quote tomorrow, hopefully.")
        self.assertIsNotNone(hedged, "a hedged commitment is still a commitment")
        self.assertLess(hedged.confidence, firm.confidence)

    def test_asking_them_is_not_a_promise(self):
        self.assertIsNone(self._first("Let me know if you need anything else."))
        self.assertIsNone(self._first("Please send the packing slip."))

    def test_stating_a_fact_is_not_a_promise(self):
        self.assertIsNone(self._first("The shipment left our dock yesterday."))

    def test_rules_confidence_never_reaches_the_action_threshold(self):
        """Guardrail: a regex must not be able to authorize an action on its own."""
        minimum_to_act = self.cfg.guardrails["confidence"]["min_to_act"]
        found = self._first("I will send the quote by EOD today, confirmed, guarantee.")
        self.assertLess(found.confidence, minimum_to_act)

    def test_undated_promise_is_detected_with_no_due_date(self):
        found = self._first("I'll get you the signed letter of supply.")
        self.assertIsNotNone(found)
        self.assertIsNone(found.due)

    def test_quoted_history_is_excluded_upstream(self):
        from cbops.normalize import new_text
        body = "Following up.\n\nOn Mon, Aug 3, 2026 Bob wrote:\n> I'll send the quote Friday"
        self.assertEqual(rules.find_commitments(new_text(body), TUESDAY), [])


class DeadlineTest(unittest.TestCase):
    def test_weekday_resolves_forward_and_to_end_of_business(self):
        found = rules.resolve_deadline("by Thursday", TUESDAY)
        self.assertEqual(found.at, dt.datetime(2026, 8, 27, 17, 0, tzinfo=dt.timezone.utc))

    def test_same_weekday_resolves_to_next_week_not_today(self):
        found = rules.resolve_deadline("Tuesday", TUESDAY)
        self.assertEqual(found.at.date(), dt.date(2026, 9, 1))

    def test_end_of_week_is_friday(self):
        self.assertEqual(rules.resolve_deadline("end of week", TUESDAY).at.date(),
                         dt.date(2026, 8, 28))

    def test_bare_numeric_date_resolves(self):
        """find_solicitation_close hands this function a phrase with no preposition."""
        self.assertEqual(rules.resolve_deadline("8/27", TUESDAY).at.date(), dt.date(2026, 8, 27))

    def test_a_quantity_is_not_a_date(self):
        self.assertIsNone(rules.resolve_deadline("quantity 120 EA", TUESDAY))

    def test_solicitation_close_is_found_in_prose(self):
        found = rules.find_solicitation_close("Quotes are due by 8/27.", TUESDAY)
        self.assertEqual(found.at.date(), dt.date(2026, 8, 27))

    def test_solicitation_close_is_found_from_a_dibbs_label(self):
        found = rules.find_solicitation_close("Return By: 09/10/2026", TUESDAY)
        self.assertEqual(found.at.date(), dt.date(2026, 9, 10))


class ReferenceTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_piid_instrument_letter_separates_award_from_solicitation(self):
        """FAR 4.16: T is a request for quote, D is an indefinite delivery contract."""
        refs = rules.find_references(
            self.cfg, "Award SPE4A6-24-D-0123 against solicitation SPE4A6-25-T-4567"
        )
        self.assertEqual(refs.contract, ["SPE4A6-24-D-0123"])
        self.assertEqual(refs.solicitation, ["SPE4A6-25-T-4567"])

    def test_nsn_must_be_thirteen_digits(self):
        refs = rules.find_references(self.cfg, "call 208-555-0100 about NSN 5930-01-234-5678")
        self.assertEqual(refs.nsn, ["5930-01-234-5678"])

    def test_primary_reference_prefers_the_contract(self):
        refs = rules.find_references(
            self.cfg, "PO# CB-1099 under SPE4A6-24-D-0123"
        )
        self.assertEqual(refs.primary, "SPE4A6-24-D-0123")


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_named_request_is_a_handoff(self):
        found = rules.find_handoffs(self.cfg, "Taj, please pick up the mod.")
        self.assertEqual([h.to_person for h in found], ["taj"])

    def test_unknown_name_is_not_a_handoff(self):
        self.assertEqual(rules.find_handoffs(self.cfg, "Priya, please send the PO."), [])

    def test_assignment_phrasing_scores_highest(self):
        assigned = rules.find_handoffs(self.cfg, "Handing this over to Roy.")[0]
        looped = rules.find_handoffs(self.cfg, "Looping in Roy.")[0]
        self.assertGreater(assigned.confidence, looped.confidence)


class AskTest(unittest.TestCase):
    def test_question_is_an_ask(self):
        self.assertTrue(rules.contains_ask("Can you confirm the delivery date?"))

    def test_acknowledgment_is_a_closer(self):
        self.assertTrue(rules.looks_like_closer("Got it, thanks."))

    def test_a_request_is_not_a_closer(self):
        self.assertFalse(rules.looks_like_closer("Thanks, can you send the quote?"))

    def test_attachment_counts_as_follow_through(self):
        self.assertTrue(rules.looks_like_followthrough("", has_attachments=True))
        self.assertTrue(rules.looks_like_followthrough("Attached, as promised.", False))
        self.assertFalse(rules.looks_like_followthrough("Still working on it.", False))


if __name__ == "__main__":
    unittest.main()
