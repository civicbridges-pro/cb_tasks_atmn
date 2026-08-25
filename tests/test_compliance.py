"""The compliance calendar.

Date driven, catastrophic if missed, and the only part of this system that depends on
nothing else being decided first. The assertions below are mostly about one rule: an item
with no recorded date is not healthy, it is unknown.
"""

from __future__ import annotations

import copy
import datetime as dt
import unittest

from cbops import compliance
from cbops.config import Config, validate
from cbops.store import Store
from tests.support import NOW, load_config, phase1_config


def _cfg_with(items: dict) -> Config:
    base = load_config()
    clone = Config(
        people=base.people, routing=base.routing, sla=base.sla, guardrails=base.guardrails,
        naming=base.naming, counterparties=base.counterparties,
        compliance=copy.deepcopy(base.compliance),
    )
    clone.compliance["items"] = items
    return clone


def _item(**overrides) -> dict:
    spec = {"name": "Test item", "owner": "anna", "renewal_lead_days": 60,
            "consequence": "something bad happens", "authority": "test"}
    spec.update(overrides)
    return spec


class StatusTest(unittest.TestCase):
    def _status(self, **overrides) -> str:
        cfg = _cfg_with({"thing": _item(**overrides)})
        return compliance.load_items(cfg, now=NOW)[0].status

    def test_no_date_is_unknown_not_ok(self):
        """The whole point. An expired item and an unrecorded one look identical here."""
        self.assertEqual(self._status(expires_at="TODO_CONFIRM"), "unknown")
        self.assertEqual(self._status(), "unknown")

    def test_a_past_date_is_expired(self):
        self.assertEqual(self._status(expires_at="2026-01-01"), "expired")

    def test_inside_the_lead_window_is_approaching(self):
        self.assertEqual(self._status(expires_at="2026-09-25"), "approaching")

    def test_close_to_expiry_is_act_now(self):
        self.assertEqual(self._status(expires_at="2026-08-30"), "act now")

    def test_far_out_is_ok(self):
        self.assertEqual(self._status(expires_at="2027-06-01"), "ok")

    def test_an_unparseable_date_is_unknown_not_a_crash(self):
        self.assertEqual(self._status(expires_at="next spring sometime"), "unknown")

    def test_lead_days_shift_the_window(self):
        self.assertEqual(self._status(expires_at="2026-10-25", renewal_lead_days=30), "ok")
        self.assertEqual(self._status(expires_at="2026-10-25", renewal_lead_days=90),
                         "approaching")


class OrderingTest(unittest.TestCase):
    def test_expired_first_then_unknown_then_soonest(self):
        """Unknown ranks above imminent because it may already have lapsed."""
        cfg = _cfg_with({
            "fine": _item(name="fine", expires_at="2027-06-01"),
            "gone": _item(name="gone", expires_at="2026-01-01"),
            "soon": _item(name="soon", expires_at="2026-08-30"),
            "nodate": _item(name="nodate", expires_at="TODO_CONFIRM"),
        })
        order = [item.name for item in compliance.load_items(cfg, now=NOW)]
        self.assertEqual(order[0], "gone")
        self.assertEqual(order[1], "nodate")
        self.assertLess(order.index("soon"), order.index("fine"))


class ReportTest(unittest.TestCase):
    def test_the_shipped_calendar_covers_what_the_brief_names(self):
        cfg = load_config()
        keys = set(cfg.compliance.get("items", {}))
        for expected in ("sam_registration", "wosb_certification", "dnb_profile",
                         "general_liability_insurance", "workers_comp",
                         "state_registrations"):
            self.assertIn(expected, keys)

    def test_every_item_has_a_named_human(self):
        cfg = load_config()
        for item in compliance.load_items(cfg, now=NOW):
            self.assertTrue(cfg.is_person(item.owner), f"{item.key} owner {item.owner!r}")

    def test_every_item_states_its_consequence(self):
        """A row nobody understands the stakes of is a row nobody renews."""
        cfg = load_config()
        for item in compliance.load_items(cfg, now=NOW):
            self.assertGreater(len(item.consequence.strip()), 40, item.key)

    def test_undated_items_are_a_target_zero_metric(self):
        report = compliance.run(load_config(), now=NOW)
        self.assertIn("date not recorded (target zero)", report.metrics)
        self.assertGreater(report.metrics["date not recorded (target zero)"], 0)

    def test_missing_evidence_is_called_out(self):
        report = compliance.run(load_config(), now=NOW)
        self.assertTrue(any("evidence" in note for note in report.notes))

    def test_it_needs_no_store_and_no_mail(self):
        """The one part of this program that depends on nothing being decided first."""
        report = compliance.run(load_config(), now=NOW)
        self.assertTrue(report.rows)


class ObligationTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.migrate()

    def test_healthy_items_do_not_reach_a_digest(self):
        """An item fine for eight months on a daily digest is how a digest becomes wallpaper."""
        cfg = _cfg_with({"fine": _item(expires_at="2027-06-01")})
        self.assertEqual(compliance.as_obligations(cfg, now=NOW), [])

    def test_expired_and_unknown_and_approaching_all_produce_obligations(self):
        cfg = _cfg_with({
            "gone": _item(name="gone", expires_at="2026-01-01"),
            "nodate": _item(name="nodate", expires_at="TODO_CONFIRM"),
            "soon": _item(name="soon", expires_at="2026-09-25"),
        })
        self.assertEqual(len(compliance.as_obligations(cfg, now=NOW)), 3)

    def test_an_undated_item_asks_a_human_to_go_look_it_up(self):
        cfg = _cfg_with({"nodate": _item(expires_at="TODO_CONFIRM")})
        obligation = compliance.as_obligations(cfg, now=NOW)[0]
        self.assertTrue(obligation["needs_human_review"])
        self.assertIn("Find and record", obligation["what_is_owed"])
        self.assertIsNone(obligation["due_at"])

    def test_sync_is_idempotent(self):
        cfg = phase1_config()
        cfg.compliance = _cfg_with({"gone": _item(expires_at="2026-01-01")}).compliance
        first = compliance.sync(cfg, self.store, now=NOW, persist=True)
        second = compliance.sync(cfg, self.store, now=NOW, persist=True)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [], "a second run must not duplicate the obligation")

    def test_an_item_leaving_its_window_does_not_auto_close(self):
        """Guardrail 3. A renewal closes when a human attaches the certificate."""
        cfg = phase1_config()
        cfg.compliance = _cfg_with({"soon": _item(expires_at="2026-09-25")}).compliance
        compliance.sync(cfg, self.store, now=NOW, persist=True)
        cfg.compliance = _cfg_with({"soon": _item(expires_at="2027-09-25")}).compliance
        compliance.sync(cfg, self.store, now=NOW, persist=True)
        rows = self.store.query(
            "SELECT status FROM obligations WHERE source_ref = 'compliance:soon'")
        self.assertEqual([row["status"] for row in rows], ["open"])

    def test_preview_persists_nothing(self):
        cfg = phase1_config()
        compliance.sync(cfg, self.store, now=NOW, persist=False)
        self.assertEqual(
            self.store.query("SELECT COUNT(*) AS n FROM obligations")[0]["n"], 0)

    def test_obligations_satisfy_the_ledger_invariants(self):
        cfg = phase1_config()
        created = compliance.sync(cfg, self.store, now=NOW, persist=True)
        self.assertTrue(created)
        for row in self.store.query("SELECT owner, type, what_is_owed FROM obligations"):
            self.assertTrue(cfg.is_person(row["owner"]) or row["owner"] == "triage_queue")
            self.assertEqual(row["type"], "compliance")
            self.assertTrue(row["what_is_owed"])


class ConfigValidationTest(unittest.TestCase):
    def test_an_unknown_owner_is_rejected(self):
        cfg = _cfg_with({"thing": _item(owner="ghost")})
        problems = validate(cfg)
        self.assertTrue(any(p.level == "error" and "compliance.thing.owner" in p.where
                            for p in problems))

    def test_a_missing_consequence_warns(self):
        cfg = _cfg_with({"thing": _item(consequence="")})
        problems = validate(cfg)
        self.assertTrue(any(p.level == "warn" and "compliance.thing" in p.where
                            for p in problems))

    def test_a_bad_lead_time_is_rejected(self):
        cfg = _cfg_with({"thing": _item(renewal_lead_days="soon")})
        problems = validate(cfg)
        self.assertTrue(any(p.level == "error" and "renewal_lead_days" in p.where
                            for p in problems))

    def test_the_shipped_calendar_has_no_errors(self):
        problems = [p for p in load_config().problems
                    if p.level == "error" and p.where.startswith("compliance")]
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
