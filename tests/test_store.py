"""Ledger invariants. Each of these is a rule the brief says most teams break."""

from __future__ import annotations

import unittest

from cbops.store import LedgerError, Store
from tests.support import load_config


class InvariantTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.store = Store(":memory:")
        self.store.migrate()

    def tearDown(self):
        self.store.close()

    def _obligation(self, **overrides):
        fields = dict(
            source="email", type="vendor_quote",
            what_is_owed="Marotta to return quote on NSN 5930-01-234-5678",
            direction="they_owe_us", owner="jason", counterparty_class="oem",
        )
        fields.update(overrides)
        return self.store.create_obligation(self.cfg, **fields)

    def test_owner_must_be_one_named_human(self):
        for bad in ("procurement", "the team", "nobody", ["jason", "joe"], "", None):
            with self.assertRaises(LedgerError, msg=f"accepted owner {bad!r}"):
                self._obligation(owner=bad)

    def test_triage_queue_is_a_valid_owner(self):
        self.assertTrue(self._obligation(owner="triage_queue"))

    def test_nothing_closes_without_evidence(self):
        oid = self._obligation()
        with self.assertRaises(LedgerError):
            self.store.set_status(oid, "done", "jason")
        self.store.add_evidence(oid, "email", "msg:1", "jason")
        self.store.set_status(oid, "done", "jason")
        row = self.store.query("SELECT status, closed_at FROM obligations WHERE id = ?", (oid,))[0]
        self.assertEqual(row["status"], "done")
        self.assertIsNotNone(row["closed_at"])

    def test_the_system_may_not_drop_an_obligation(self):
        oid = self._obligation()
        with self.assertRaises(LedgerError):
            self.store.set_status(oid, "dropped", "system")
        self.store.set_status(oid, "dropped", "anna")

    def test_waiting_external_always_carries_a_clock(self):
        oid = self._obligation()
        self.store.set_status(oid, "waiting_external", "jason")
        row = self.store.query("SELECT waiting_since FROM obligations WHERE id = ?", (oid,))[0]
        self.assertIsNotNone(row["waiting_since"], "waiting on a vendor is not done")

    def test_what_is_owed_is_required(self):
        with self.assertRaises(LedgerError):
            self._obligation(what_is_owed="")

    def test_every_state_change_is_audited(self):
        oid = self._obligation()
        self.store.add_evidence(oid, "email", "msg:1", "jason")
        self.store.set_status(oid, "waiting_external", "jason")
        self.store.set_status(oid, "done", "jason")
        rows = self.store.query(
            "SELECT actor, field, old_value, new_value FROM obligation_audit "
            "WHERE obligation_id = ? ORDER BY id", (oid,)
        )
        self.assertEqual([r["new_value"] for r in rows],
                         ["vendor_quote", "waiting_external", "done"])
        self.assertTrue(all(r["actor"] for r in rows))

    def test_escalation_path_is_recorded_at_creation(self):
        oid = self._obligation()
        row = self.store.query(
            "SELECT escalation_path FROM obligations WHERE id = ?", (oid,))[0]
        self.assertIn("morgan", row["escalation_path"])

    def test_every_routed_type_is_storable(self):
        """A routing rule for a type the ledger rejects fails at the first real message."""
        for rule in self.cfg.routing_rules:
            self._obligation(type=rule["type"], what_is_owed=f"test {rule['type']}",
                             owner=self.cfg.owner_for_type(rule["type"]))


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.migrate()

    def tearDown(self):
        self.store.close()

    def test_a_failed_sync_is_visible(self):
        sync_id = self.store.start_sync("imap", "quotes@civicbridges.com")
        self.store.finish_sync(sync_id, 0, 0, ok=False, error="connection reset")
        row = self.store.last_sync()[0]
        self.assertIsNone(row["last_success"], "a failed sync must not read as a success")

    def test_message_ingest_is_idempotent(self):
        message = dict(
            source="email", mailbox="quotes@civicbridges.com", message_id="abc@x",
            thread_key="mid:abc@x", from_addr="a@b.example", to_addrs=["quotes@civicbridges.com"],
            subject="test", sent_at="2026-08-20T10:00:00+00:00", direction="inbound",
            body_text="hello", snippet="hello", has_attachments=0,
            counterparty="b.example", counterparty_class="unknown", quarantined=0,
        )
        self.assertIsNotNone(self.store.upsert_message(dict(message)))
        self.assertIsNone(self.store.upsert_message(dict(message)),
                          "a re-read of the same message must not duplicate it")
        self.assertEqual(self.store.query("SELECT COUNT(*) AS n FROM messages")[0]["n"], 1)

    def test_an_auto_registered_mailbox_is_reported_unclaimed(self):
        self.store.upsert_message(dict(
            source="email", mailbox="shared@civicbridges.com", message_id="m1@x",
            thread_key="mid:m1@x", from_addr="a@b.example", to_addrs=[], subject="s",
            sent_at="2026-08-20T10:00:00+00:00", direction="inbound", body_text="",
            snippet="", has_attachments=0, counterparty="b.example",
            counterparty_class="unknown", quarantined=0,
        ))
        unclaimed = [row["address"] for row in self.store.unclaimed_mailboxes()]
        self.assertIn("shared@civicbridges.com", unclaimed)


if __name__ == "__main__":
    unittest.main()
