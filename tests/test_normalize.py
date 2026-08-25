"""Transport parsing. Everything downstream trusts these fields."""

from __future__ import annotations

import email
import unittest
from pathlib import Path

from cbops import normalize
from tests.support import FIXTURE_MAIL, load_config


def parse_fixture(name: str):
    return email.message_from_bytes((FIXTURE_MAIL / name).read_bytes())


class DirectionTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_directions(self):
        cases = [
            ("doug@civicbridges.com", ["buyer@dla.mil"], "outbound"),
            ("buyer@dla.mil", ["quotes@civicbridges.com"], "inbound"),
            ("doug@civicbridges.com", ["anna@civicbridges.com"], "internal"),
        ]
        for sender, recipients, expected in cases:
            self.assertEqual(normalize.direction_of(self.cfg, sender, recipients), expected)

    def test_a_mixed_thread_is_outbound_not_internal(self):
        """An internal cc must not hide an external send."""
        self.assertEqual(
            normalize.direction_of(
                self.cfg, "doug@civicbridges.com",
                ["anna@civicbridges.com", "buyer@dla.mil"],
            ),
            "outbound",
        )


class CounterpartyTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_the_most_important_party_wins(self):
        """A .mil on the thread makes it a gov thread, whoever else is copied."""
        _, cclass = normalize.classify_counterparty(
            self.cfg, ["sales@vendor.example", "buyer@dla.mil"]
        )
        self.assertEqual(cclass, "gov")

    def test_school_district_is_sled(self):
        _, cclass = normalize.classify_counterparty(self.cfg, ["purchasing@boise.k12.id.us"])
        self.assertEqual(cclass, "customer_sled")

    def test_our_own_domain_is_internal(self):
        _, cclass = normalize.classify_counterparty(self.cfg, ["anna@civicbridges.com"])
        self.assertEqual(cclass, "internal")


class SensitivityTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_distribution_statement_is_quarantined(self):
        flagged, reason = normalize.classify_sensitivity(
            self.cfg, "Drawing", "DISTRIBUTION STATEMENT C applies", []
        )
        self.assertTrue(flagged)
        self.assertIn("DISTRIBUTION STATEMENT C", reason)

    def test_cad_attachment_is_quarantined_on_extension_alone(self):
        """Technical drawings are the likeliest carrier and the least likely to be marked."""
        flagged, reason = normalize.classify_sensitivity(self.cfg, "pkg", "see attached",
                                                         ["assembly.SLDPRT"])
        self.assertTrue(flagged)
        self.assertIn(".sldprt", reason)

    def test_ordinary_mail_is_not_quarantined(self):
        flagged, _ = normalize.classify_sensitivity(
            self.cfg, "Quote", "Pricing attached.", ["quote.pdf"]
        )
        self.assertFalse(flagged)

    def test_a_quarantined_message_keeps_its_envelope_and_loses_its_body(self):
        result = normalize.from_email_message(
            self.cfg, parse_fixture("06-cui-quarantine.eml"), "quotes@civicbridges.com"
        )
        self.assertEqual(result["quarantined"], 1)
        self.assertIsNone(result["body_text"])
        self.assertEqual(result["attachment_names"], [])
        self.assertTrue(result["subject"], "the envelope must survive so the thread counts")
        self.assertEqual(result["counterparty_class"], "gov")


class ThreadingTest(unittest.TestCase):
    def test_a_root_keys_on_its_own_message_id(self):
        """Otherwise every two-message thread splits and both halves look unanswered."""
        root = normalize.thread_key("cb-100@x", "", "", "RFQ CB-1042", ["a@b.example"])
        reply = normalize.thread_key("acme-1@y", "cb-100@x", "<cb-100@x>", "Re: RFQ CB-1042",
                                     ["a@b.example"])
        self.assertEqual(root, reply)

    def test_deep_chain_keys_on_the_root(self):
        key = normalize.thread_key("m3@x", "m2@x", "<m1@x> <m2@x>", "Re: Re: hi", [])
        self.assertEqual(key, "mid:m1@x")

    def test_no_identifiers_falls_back_to_subject_and_participants(self):
        first = normalize.thread_key("", "", "", "Re: Quote request", ["a@b.example"])
        second = normalize.thread_key("", "", "", "quote request", ["a@b.example"])
        self.assertEqual(first, second)
        third = normalize.thread_key("", "", "", "Quote request", ["z@other.example"])
        self.assertNotEqual(first, third, "different counterparties are different threads")


class BodyTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_quoted_history_is_stripped(self):
        body = "Following up.\n\nOn Mon, Aug 3, 2026 Bob wrote:\n> old text"
        self.assertEqual(normalize.new_text(body), "Following up.")

    def test_signature_delimiter_is_stripped(self):
        self.assertEqual(normalize.new_text("Sending today.\n\n-- \nJason\nCivicBridges"),
                         "Sending today.")

    def test_auto_reply_is_noise(self):
        result = normalize.from_email_message(
            self.cfg, parse_fixture("07-autoreply.eml"), "quotes@civicbridges.com"
        )
        self.assertIsNone(result, "an out of office reply must never reach a report")

    def test_html_only_mail_still_yields_text(self):
        raw = (
            b"From: a@b.example\r\nTo: quotes@civicbridges.com\r\nSubject: t\r\n"
            b"Date: Mon, 24 Aug 2026 10:00:00 -0600\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: text/html; charset=utf-8\r\n\r\n"
            b"<div><p>I&#39;ll send the quote Friday.</p></div>"
        )
        result = normalize.from_email_message(
            self.cfg, email.message_from_bytes(raw), "quotes@civicbridges.com"
        )
        self.assertIn("send the quote Friday", result["body_text"])

    def test_undated_mail_sorts_to_the_epoch_not_to_now(self):
        """Sorting unknown-date mail to now puts it at the top of every report."""
        self.assertEqual(normalize.parse_date(None).year, 1970)
        self.assertEqual(normalize.parse_date("not a date").year, 1970)


if __name__ == "__main__":
    unittest.main()
