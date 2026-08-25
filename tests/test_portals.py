"""Paste bridge. The rule is that an unreadable field is reported, never guessed."""

from __future__ import annotations

import datetime as dt
import unittest

from cbops.ingest import portals
from tests.support import load_config

NOW = dt.datetime(2026, 8, 25, tzinfo=dt.timezone.utc)

DIBBS = """
Solicitation: SPE4A6-25-T-4567
NSN: 5930-01-234-5678
Item Name: SWITCH, TOGGLE
Quantity: 120
UI: EA
Return By: 09/10/2026
Buyer: J. Ramirez
Delivery Days: 120
"""


class DibbsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_a_complete_paste_parses(self):
        parsed = portals.parse("dibbs", DIBBS, self.cfg, now=NOW)
        self.assertEqual(parsed.fields["solicitation_ref"], "SPE4A6-25-T-4567")
        self.assertEqual(parsed.fields["nsn"], "5930-01-234-5678")
        self.assertEqual(parsed.fields["close_date_at"][:10], "2026-09-10")
        self.assertFalse(parsed.needs_human)

    def test_a_missing_close_date_demands_a_human(self):
        """A wrong close date would silently break the whole backward planned cadence."""
        text = DIBBS.replace("Return By: 09/10/2026", "")
        parsed = portals.parse("dibbs", text, self.cfg, now=NOW)
        self.assertTrue(parsed.needs_human)
        self.assertIn("close_date", parsed.missing)

    def test_an_unparseable_date_is_flagged_not_guessed(self):
        text = DIBBS.replace("09/10/2026", "see attachment")
        parsed = portals.parse("dibbs", text, self.cfg, now=NOW)
        self.assertIn("close_date_at", parsed.missing)
        self.assertNotIn("close_date_at", parsed.fields)


class WawfTest(unittest.TestCase):
    def test_contract_and_invoice_are_required(self):
        cfg = load_config()
        parsed = portals.parse("wawf", "Invoice No: CB-2026-014\n", cfg, now=NOW)
        self.assertIn("contract_ref", parsed.missing)

    def test_full_record_parses(self):
        cfg = load_config()
        parsed = portals.parse(
            "wawf",
            "Contract: SPE4A6-24-D-0123\nDelivery Order: 0004\n"
            "Invoice No: CB-2026-014\nTotal: 48,300.00\nStatus: Processed\n",
            cfg, now=NOW,
        )
        self.assertEqual(parsed.fields["contract_ref"], "SPE4A6-24-D-0123")
        self.assertEqual(parsed.fields["delivery_order"], "0004")
        self.assertFalse(parsed.needs_human)


class SamTest(unittest.TestCase):
    def test_expiration_is_required_because_the_calendar_depends_on_it(self):
        cfg = load_config()
        parsed = portals.parse("sam", "UEI: ABC123DEF456\nStatus: Active\n", cfg, now=NOW)
        self.assertEqual(parsed.fields["uei"], "ABC123DEF456")
        self.assertIn("expiration_date", parsed.missing)

    def test_expiration_resolves(self):
        cfg = load_config()
        parsed = portals.parse(
            "sam", "UEI: ABC123DEF456\nExpiration Date: March 14, 2027\n", cfg, now=NOW
        )
        self.assertEqual(parsed.fields["expiration_date_at"][:10], "2027-03-14")
        self.assertFalse(parsed.needs_human)


class UnknownKindTest(unittest.TestCase):
    def test_an_unknown_portal_is_an_error_not_a_silent_pass(self):
        with self.assertRaises(ValueError):
            portals.parse("mysba", "anything", load_config(), now=NOW)


if __name__ == "__main__":
    unittest.main()
