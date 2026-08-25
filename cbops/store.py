"""SQLite store for messages, threads, and the ledger mirror.

Phase 0 this is the whole database. From Phase 1 the system of record for obligations is a
custom module in Zoho CRM and this becomes the message index plus a mirror, so reports run
without hammering the Zoho API.

Two things this module refuses to do, because they are ledger invariants and not
preferences:

*   close an obligation with no evidence row
*   accept an owner that is not exactly one named human
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import REPO_ROOT, TRIAGE_QUEUE, Config

SCHEMA_PATH = REPO_ROOT / "ledger" / "schema.sql"
DEFAULT_DB = Path(os.environ.get("CB_DB", REPO_ROOT / "var" / "ledger.db"))

GROUP_OWNERS = {
    "procurement", "the team", "team", "ops", "operations", "sales", "support",
    "accounting", "logistics", "everyone", "all", "us", "we",
}


class LedgerError(Exception):
    """An invariant violation. Never caught and logged; it means code is wrong."""


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    stamp = dt.datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp


class Store:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    # -- lifecycle ---------------------------------------------------------

    def migrate(self) -> None:
        self.db.executescript(SCHEMA_PATH.read_text())
        self.db.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (1, ?)",
            (utcnow(),),
        )
        self.db.commit()

    def snapshot(self) -> "Store":
        """An in-memory copy of this store.

        Preview mode writes to a snapshot and throws it away. Without this, previewing the
        ledger produces a triage table and a set of empty digests, which is the opposite of
        useful: the digests are what a person actually has to judge before agreeing to turn
        Phase 1 on.
        """
        clone = Store(":memory:")
        self.db.backup(clone.db)
        clone.db.execute("PRAGMA foreign_keys = ON")
        return clone

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- messages ----------------------------------------------------------

    def upsert_message(self, msg: dict[str, Any]) -> int | None:
        """Insert a normalized message. Returns row id, or None if already present.

        Idempotent on (source, message_id) so a re-sync of an IMAP folder is free. IMAP
        sync will re-read overlapping windows constantly, by design, because the
        alternative is trusting UIDs across a connection that silently stalls.
        """
        columns = (
            "source", "mailbox", "message_id", "thread_key", "in_reply_to", "refs",
            "from_addr", "from_name", "to_addrs", "cc_addrs", "subject", "sent_at",
            "direction", "body_text", "snippet", "has_attachments", "attachment_names",
            "counterparty", "counterparty_class", "contract_ref", "quarantined",
            "quarantine_reason", "raw_ref",
        )
        values = [msg.get(c) for c in columns]
        for key in ("to_addrs", "cc_addrs", "attachment_names"):
            index = columns.index(key)
            if isinstance(values[index], (list, tuple)):
                values[index] = json.dumps(list(values[index]))

        if msg.get("mailbox"):
            self._ensure_mailbox(str(msg["mailbox"]))

        placeholders = ", ".join("?" for _ in columns)
        cur = self.db.execute(
            f"INSERT OR IGNORE INTO messages ({', '.join(columns)}, ingested_at) "
            f"VALUES ({placeholders}, ?)",
            (*values, utcnow()),
        )
        self.db.commit()
        if cur.rowcount == 0:
            return None
        self._touch_thread(msg)
        return int(cur.lastrowid)

    def _ensure_mailbox(self, address: str) -> None:
        """Auto-register a mailbox seen during capture.

        Registered this way it has no named reader, which matters: an obligation sourced
        from a mailbox nobody is recorded as reading cannot satisfy the single-owner rule.
        `./cb doctor` lists these as unclaimed. Pass `--mailbox-file` to declare kind,
        readers, and owner explicitly, which is the only way to describe a shared alias
        honestly on a platform with no delegation model.
        """
        self.db.execute(
            "INSERT OR IGNORE INTO mailboxes (address, kind, enabled) VALUES (?, ?, 1)",
            (address, "individual"),
        )

    def _touch_thread(self, msg: dict[str, Any]) -> None:
        """Keep the thread rollup current. Cheaper than recomputing it per report."""
        key = msg["thread_key"]
        row = self.db.execute(
            "SELECT MIN(sent_at) AS first_at, MAX(sent_at) AS last_at, COUNT(*) AS n "
            "FROM messages WHERE thread_key = ?",
            (key,),
        ).fetchone()
        last = self.db.execute(
            "SELECT direction, counterparty, counterparty_class, contract_ref, subject "
            "FROM messages WHERE thread_key = ? ORDER BY sent_at DESC, id DESC LIMIT 1",
            (key,),
        ).fetchone()
        # Thread-level counterparty is the first external one seen, so an internal reply
        # at the end of a customer thread does not reclassify the whole thread.
        external = self.db.execute(
            "SELECT counterparty, counterparty_class FROM messages "
            "WHERE thread_key = ? AND counterparty_class NOT IN ('internal') "
            "ORDER BY sent_at ASC LIMIT 1",
            (key,),
        ).fetchone()
        counterparty = (external or last)["counterparty"]
        cclass = (external or last)["counterparty_class"]

        self.db.execute(
            """
            INSERT INTO threads (thread_key, subject, first_at, last_at, last_direction,
                                 message_count, counterparty, counterparty_class,
                                 contract_ref, is_external)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_key) DO UPDATE SET
                subject = COALESCE(threads.subject, excluded.subject),
                first_at = excluded.first_at,
                last_at = excluded.last_at,
                last_direction = excluded.last_direction,
                message_count = excluded.message_count,
                counterparty = excluded.counterparty,
                counterparty_class = excluded.counterparty_class,
                contract_ref = COALESCE(threads.contract_ref, excluded.contract_ref),
                is_external = excluded.is_external
            """,
            (
                key, last["subject"], row["first_at"], row["last_at"], last["direction"],
                row["n"], counterparty, cclass, last["contract_ref"],
                0 if cclass == "internal" else 1,
            ),
        )
        self.db.commit()

    def reconcile_threads(self) -> int:
        """Merge threads that are one conversation split by a broken References chain.

        Two threads merge when they share a normalized subject and the same counterparty.
        Namecheap traffic includes clients that drop References, and a split thread is
        actively harmful: each half looks unanswered, and a promise made in one half can
        never be discharged by the follow-through in the other.

        Merging is conservative. Subject alone is not enough, because "Quote request" from
        two different vendors is two conversations.
        """
        from .normalize import normalize_subject

        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in self.db.execute(
            "SELECT thread_key, subject, counterparty, first_at FROM threads "
            "ORDER BY first_at, thread_key"
        ):
            subject = normalize_subject(row["subject"] or "")
            if not subject:
                continue
            groups.setdefault((subject, row["counterparty"] or ""), []).append(row)

        merged = 0
        for members in groups.values():
            if len(members) < 2:
                continue
            keeper = members[0]["thread_key"]
            for extra in members[1:]:
                self.db.execute(
                    "UPDATE messages SET thread_key = ? WHERE thread_key = ?",
                    (keeper, extra["thread_key"]),
                )
                self.db.execute(
                    "UPDATE promises SET thread_key = ? WHERE thread_key = ?",
                    (keeper, extra["thread_key"]),
                )
                self.db.execute(
                    "UPDATE handoffs SET thread_key = ? WHERE thread_key = ?",
                    (keeper, extra["thread_key"]),
                )
                self.db.execute("DELETE FROM threads WHERE thread_key = ?", (extra["thread_key"],))
                merged += 1
            self.db.commit()
            row = self.db.execute(
                "SELECT * FROM messages WHERE thread_key = ? ORDER BY sent_at DESC LIMIT 1",
                (keeper,),
            ).fetchone()
            if row is not None:
                self._touch_thread(dict(row))
        return merged

    def set_thread_fields(self, thread_key: str, **fields: Any) -> None:
        allowed = {
            "solicitation_ref", "solicitation_close_at", "contract_ref", "owner_person",
            "importance", "subject",
        }
        bad = set(fields) - allowed
        if bad:
            raise LedgerError(f"cannot set thread fields {sorted(bad)}")
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(
            f"UPDATE threads SET {assignments} WHERE thread_key = ?",
            (*fields.values(), thread_key),
        )
        self.db.commit()

    def messages_in_thread(self, thread_key: str) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM messages WHERE thread_key = ? ORDER BY sent_at, id",
                (thread_key,),
            )
        )

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.db.execute(sql, params))

    # -- obligations -------------------------------------------------------

    def create_obligation(self, cfg: Config, **fields: Any) -> int:
        owner = fields.get("owner")
        self._assert_single_human_owner(cfg, owner)
        if not fields.get("what_is_owed"):
            raise LedgerError("what_is_owed is required; an obligation nobody can read is not one")
        now = utcnow()
        fields.setdefault("escalation_path", json.dumps(cfg.escalation_path(fields["type"])))
        fields.setdefault("status", "open")
        if fields["status"] == "waiting_external":
            fields.setdefault("waiting_since", now)
        columns = list(fields)
        cur = self.db.execute(
            f"INSERT INTO obligations ({', '.join(columns)}, created_at, updated_at) "
            f"VALUES ({', '.join('?' for _ in columns)}, ?, ?)",
            (*fields.values(), now, now),
        )
        self.db.commit()
        oid = int(cur.lastrowid)
        self.audit(oid, actor="system", field="created", new_value=fields["type"])
        return oid

    @staticmethod
    def _assert_single_human_owner(cfg: Config, owner: Any) -> None:
        if owner is None or owner == "":
            raise LedgerError("obligation owner is required")
        if isinstance(owner, (list, tuple, set)):
            raise LedgerError("obligation owner must be one named human, not a group")
        if str(owner).strip().lower() in GROUP_OWNERS:
            raise LedgerError(
                f"obligation owner {owner!r} is a group; owner must be one named human"
            )
        if owner != TRIAGE_QUEUE and not cfg.is_person(owner):
            raise LedgerError(f"obligation owner {owner!r} is not in people.yaml")

    def add_evidence(self, obligation_id: int, kind: str, ref: str, added_by: str,
                     label: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO evidence (obligation_id, kind, ref, label, added_by, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (obligation_id, kind, ref, label, added_by, utcnow()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def has_evidence(self, obligation_id: int) -> bool:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM evidence WHERE obligation_id = ?", (obligation_id,)
        ).fetchone()
        return bool(row["n"])

    def set_status(self, obligation_id: int, status: str, actor: str,
                   note: str | None = None) -> None:
        """Nothing closes without evidence, and nothing closes without a named actor."""
        if status == "done" and not self.has_evidence(obligation_id):
            raise LedgerError(
                f"obligation {obligation_id} cannot close: no evidence. "
                "A quote is not sent until there is a link to what was sent."
            )
        if status == "dropped" and actor == "system":
            raise LedgerError(
                f"obligation {obligation_id} cannot be dropped by the system; "
                "a human decides to drop an obligation"
            )
        row = self.db.execute(
            "SELECT status FROM obligations WHERE id = ?", (obligation_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(f"unknown obligation {obligation_id}")
        now = utcnow()
        self.db.execute(
            "UPDATE obligations SET status = ?, updated_at = ?, "
            "waiting_since = CASE WHEN ? = 'waiting_external' "
            "                THEN COALESCE(waiting_since, ?) ELSE waiting_since END, "
            "closed_at = CASE WHEN ? IN ('done','dropped') THEN ? ELSE NULL END "
            "WHERE id = ?",
            (status, now, status, now, status, now, obligation_id),
        )
        self.db.commit()
        self.audit(obligation_id, actor=actor, field="status",
                   old_value=row["status"], new_value=status, note=note)

    def audit(self, obligation_id: int, actor: str, field: str | None = None,
              old_value: Any = None, new_value: Any = None, note: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO obligation_audit (obligation_id, ts, actor, field, old_value, "
            "new_value, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (obligation_id, utcnow(), actor, field,
             None if old_value is None else str(old_value),
             None if new_value is None else str(new_value), note),
        )
        self.db.commit()

    # -- phase 0 observations ---------------------------------------------

    def record_extraction(self, message_id: int, extractor: str, kind: str,
                          payload: Any, confidence: float | None) -> int:
        cur = self.db.execute(
            "INSERT INTO extractions (message_id, extractor, kind, payload_json, "
            "confidence, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (message_id, extractor, kind, json.dumps(payload), confidence, utcnow()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def record_promise(self, **fields: Any) -> int:
        columns = list(fields)
        cur = self.db.execute(
            f"INSERT INTO promises ({', '.join(columns)}, created_at) "
            f"VALUES ({', '.join('?' for _ in columns)}, ?)",
            (*fields.values(), utcnow()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def record_handoff(self, **fields: Any) -> int:
        columns = list(fields)
        cur = self.db.execute(
            f"INSERT INTO handoffs ({', '.join(columns)}, created_at) "
            f"VALUES ({', '.join('?' for _ in columns)}, ?)",
            (*fields.values(), utcnow()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def clear_observations(self) -> None:
        """Phase 0 reports are recomputed from scratch each run, never incrementally.

        Incremental observation state is how a report quietly stops finding things.
        """
        self.db.executescript("DELETE FROM promises; DELETE FROM handoffs;")
        self.db.commit()

    # -- health ------------------------------------------------------------

    def start_sync(self, source: str, target: str) -> int:
        cur = self.db.execute(
            "INSERT INTO sync_health (source, target, started_at) VALUES (?, ?, ?)",
            (source, target, utcnow()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def finish_sync(self, sync_id: int, seen: int, new: int, ok: bool,
                    error: str | None = None) -> None:
        self.db.execute(
            "UPDATE sync_health SET finished_at = ?, messages_seen = ?, messages_new = ?, "
            "ok = ?, error = ? WHERE id = ?",
            (utcnow(), seen, new, 1 if ok else 0, error, sync_id),
        )
        self.db.commit()

    def last_sync(self) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT target, MAX(started_at) AS last_attempt, "
                "       MAX(CASE WHEN ok = 1 THEN started_at END) AS last_success "
                "FROM sync_health GROUP BY target ORDER BY target"
            )
        )

    def log_action(self, actor: str, action: str, entity: str | None = None,
                   entity_id: str | None = None, prompt: str | None = None,
                   output: str | None = None, detail: Any = None) -> None:
        self.db.execute(
            "INSERT INTO action_log (ts, actor, action, entity, entity_id, prompt, output, "
            "detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (utcnow(), actor, action, entity, entity_id, prompt, output,
             None if detail is None else json.dumps(detail)),
        )
        self.db.commit()

    def record_report_run(self, report: str, row_count: int, params: Any,
                          path: str | None) -> None:
        self.db.execute(
            "INSERT INTO report_runs (report, generated_at, row_count, params_json, path) "
            "VALUES (?, ?, ?, ?, ?)",
            (report, utcnow(), row_count, json.dumps(params), path),
        )
        self.db.commit()

    def unclaimed_mailboxes(self) -> list[sqlite3.Row]:
        """Mailboxes with no named reader. Each one is a hole in the ownership model."""
        return list(
            self.db.execute(
                "SELECT address, kind FROM mailboxes WHERE owner_person IS NULL "
                "ORDER BY address"
            )
        )

    def register_mailboxes(self, mailboxes: Iterable[dict[str, Any]]) -> None:
        for box in mailboxes:
            self.db.execute(
                "INSERT INTO mailboxes (address, label, kind, readers, owner_person, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(address) DO UPDATE SET label = excluded.label, "
                "kind = excluded.kind, readers = excluded.readers, "
                "owner_person = excluded.owner_person, enabled = excluded.enabled",
                (
                    box["address"], box.get("label"), box.get("kind", "individual"),
                    json.dumps(box.get("readers", [])), box.get("owner_person"),
                    1 if box.get("enabled", True) else 0,
                ),
            )
        self.db.commit()
