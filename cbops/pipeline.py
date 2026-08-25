"""Capture to observation, in one pass.

L1 capture and L2 extraction wired together: raw transport in, normalized messages and
scored observations out. Phase 0 stops here. Nothing routes, nothing sends, nothing writes
to Zoho.

The rules layer runs on every message and its findings are stored. The model layer is
optional and is applied to candidates afterward, so a run with no Claude CLI available
still produces every report, just with rules-level confidence. A pipeline that silently
produced nothing when the model was unreachable would be the worst of both worlds.
"""

from __future__ import annotations

import datetime as dt
import email
import json
from typing import Any, Iterable

from .config import Config
from .extract import rules
from .ingest.base import RawMessage, SyncResult
from .normalize import from_email_message
from .store import Store, parse_ts


def ingest_email(cfg: Config, store: Store, source: Any,
                 since: dt.datetime | None = None) -> list[SyncResult]:
    """Pull every target on a mail source into the store."""
    results: list[SyncResult] = []
    for target in source.targets():
        sync_id = store.start_sync(source.name, target)
        result = SyncResult(target=target)
        try:
            for raw in source.fetch(target, since):
                result.seen += 1
                _absorb_raw(cfg, store, raw, result)
        except Exception as exc:                       # transport failure, never silent
            result.ok = False
            result.error = f"{type(exc).__name__}: {exc}"
            result.partial = True
        store.finish_sync(sync_id, result.seen, result.new, result.ok, result.error)
        results.append(result)
    return results


def _absorb_raw(cfg: Config, store: Store, raw: RawMessage, result: SyncResult) -> None:
    try:
        parsed = email.message_from_bytes(raw.raw_bytes)
    except Exception as exc:
        result.skipped += 1
        result.notes.append(f"unparseable message at {raw.raw_ref}: {exc}")
        # A message we cannot parse marks the sync partial. "We read everything" and "we
        # read what we could" must never look the same on a dashboard.
        result.partial = True
        return

    normalized = from_email_message(cfg, parsed, raw.mailbox, raw.raw_ref)
    if normalized is None:
        result.skipped += 1                            # auto-reply, bounce, or noise
        return

    absorb_normalized(cfg, store, normalized, result)


def absorb_normalized(cfg: Config, store: Store, normalized: dict[str, Any],
                      result: SyncResult | None = None) -> int | None:
    """Store one normalized message and run the deterministic extractors over it."""
    extra = {k: normalized.pop(k) for k in ("unmapped_sender",) if k in normalized}

    summary = None
    if not normalized["quarantined"]:
        sent_at = parse_ts(normalized["sent_at"]) or dt.datetime.now(dt.timezone.utc)
        summary = rules.summarize(
            cfg,
            normalized.get("body_text") or "",
            sent_at,
            normalized["direction"],
            normalized["counterparty_class"] == "internal",
        )
        normalized["contract_ref"] = summary["contract_ref"]

    message_id = store.upsert_message(normalized)
    if message_id is None:
        return None                                     # already seen, idempotent by design

    if result is not None:
        result.new += 1
        if normalized["quarantined"]:
            result.quarantined += 1

    if summary is None:
        # Quarantined: envelope only. The thread still counts toward the unanswered
        # report, and no body text was ever written.
        store.record_extraction(
            message_id, "rules", "quarantined",
            {"reason": normalized.get("quarantine_reason")}, None,
        )
        return message_id

    store.record_extraction(message_id, "rules", "summary", summary, None)
    if extra.get("unmapped_sender"):
        store.record_extraction(
            message_id, "rules", "unmapped_sender",
            {"sender": extra["unmapped_sender"]}, None,
        )

    for commitment in summary["commitments"]:
        store.record_promise(
            message_id=message_id,
            thread_key=normalized["thread_key"],
            promised_by=normalized["from_addr"],
            promised_to=json.dumps(normalized.get("to_addrs") or []),
            text=commitment["text"],
            due_text=commitment["due_text"],
            due_at=commitment["due_at"],
            confidence=commitment["confidence"],
            detector="rules.commitment",
            status="unknown",
        )

    for handoff in summary["handoffs"]:
        store.record_handoff(
            message_id=message_id,
            thread_key=normalized["thread_key"],
            from_person=normalized["from_addr"],
            to_person=handoff["to_person"],
            text=handoff["text"],
            confidence=handoff["confidence"],
            detector="rules.handoff",
            crosses_offset_boundary=1 if _is_offset(cfg, handoff["to_person"]) else 0,
        )

    close = summary.get("solicitation_close")
    if close and close.get("at"):
        # The external clock every vendor chase hangs off. Never overwritten once set, so a
        # later message quoting an old date cannot move a live deadline.
        existing = store.query(
            "SELECT solicitation_close_at FROM threads WHERE thread_key = ?",
            (normalized["thread_key"],),
        )
        if not existing or not existing[0]["solicitation_close_at"]:
            store.set_thread_fields(
                normalized["thread_key"], solicitation_close_at=close["at"]
            )

    if summary["contract_ref"]:
        store.set_thread_fields(
            normalized["thread_key"], contract_ref=summary["contract_ref"]
        )

    solicitations = summary["references"].get("solicitation") or []
    if solicitations:
        store.set_thread_fields(
            normalized["thread_key"], solicitation_ref=solicitations[0]
        )

    return message_id


def _is_offset(cfg: Config, person_id: str) -> bool:
    try:
        return bool(cfg.person(person_id).get("offset_hours"))
    except Exception:
        return False


def ingest_normalized(cfg: Config, store: Store, messages: Iterable[dict[str, Any]],
                      source_name: str, target: str) -> SyncResult:
    """Ingest already-normalized messages, for Telegram exports and manual entry."""
    sync_id = store.start_sync(source_name, target)
    result = SyncResult(target=target)
    try:
        for message in messages:
            result.seen += 1
            absorb_normalized(cfg, store, message, result)
    except Exception as exc:
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        result.partial = True
    store.finish_sync(sync_id, result.seen, result.new, result.ok, result.error)
    return result
