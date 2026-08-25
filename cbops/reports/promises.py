"""Broken Promise Report.

Commitments made in outbound mail with no matching follow-through.

This is the hardest of the four to extract and the most valuable. It is also the one that
shapes how customers and contracting officers perceive the company: a missed internal task
is invisible to them, a missed "I'll have that to you Friday" is not.

The bar for calling something broken is deliberately high. A false broken destroys trust in
this report faster than a missed one does, and once the team decides the report cries wolf
it stops being read at all. So a promise whose thread went quiet is `unknown`, not `broken`,
because it may well have been kept by phone, in Telegram, or through a portal.
"""

from __future__ import annotations

import datetime as dt

from ..config import Config
from ..extract import rules
from ..store import Store, parse_ts
from .render import Report, freshness_lines


def run(cfg: Config, store: Store, now: dt.datetime | None = None) -> Report:
    now = now or dt.datetime.now(dt.timezone.utc)
    grace = float(cfg.phase0("promise_grace_hours", 4))

    lines, stale = freshness_lines(cfg, store, now)
    report = Report(
        key="promises",
        title="Broken Promise Report",
        subtitle=(
            "Commitments made in outbound mail with no matching follow-through. "
            "A promise whose thread went quiet is reported as unknown, never as broken."
        ),
        columns=["status", "due", "overdue_by", "promised_by", "counterparty", "promise",
                 "contract", "confidence", "thread"],
        freshness=lines,
        stale=stale,
        generated_at=now,
    )

    promises = store.query(
        """
        SELECT p.*, m.direction, m.sent_at AS promised_at, m.counterparty,
               m.counterparty_class, m.contract_ref
        FROM promises p JOIN messages m ON m.id = p.message_id
        ORDER BY p.due_at IS NULL, p.due_at
        """
    )

    counts = {"broken": 0, "kept": 0, "open": 0, "unknown": 0, "undated": 0}

    for promise in promises:
        promised_at = parse_ts(promise["promised_at"])
        due_at = parse_ts(promise["due_at"])
        status, resolver = _judge(store, promise, promised_at, due_at, now, grace)
        counts[status] = counts.get(status, 0) + 1
        if due_at is None:
            counts["undated"] += 1

        store.db.execute(
            "UPDATE promises SET status = ?, resolved_by_message_id = ? WHERE id = ?",
            (status if status != "undated" else "unknown", resolver, promise["id"]),
        )

        if status in ("kept", "open"):
            continue

        overdue = ""
        if due_at and now > due_at:
            overdue = f"{(now - due_at).total_seconds() / 3600:.0f}h"

        report.rows.append({
            "status": status,
            "due": promise["due_text"] or ("(no date)" if not due_at else due_at.date().isoformat()),
            "overdue_by": overdue,
            "promised_by": promise["promised_by"],
            "counterparty": promise["counterparty"] or "",
            "promise": (promise["text"] or "")[:100],
            "contract": promise["contract_ref"] or "",
            "confidence": promise["confidence"],
            "thread": promise["thread_key"],
        })
    store.db.commit()

    # Broken first, then the most overdue.
    order = {"broken": 0, "unknown": 1}
    report.rows.sort(key=lambda r: (order.get(str(r["status"]), 2), r["due"]))

    report.metrics = {
        "promises detected": len(promises),
        "broken": counts["broken"],
        "unknown, may have been kept off-thread": counts["unknown"],
        "kept": counts["kept"],
        "still open": counts["open"],
        "promises with no date at all": counts["undated"],
    }
    report.notes = [
        "Confidence here is from the deterministic detector, which is capped below the "
        "action threshold on purpose. Run the model pass to raise or reject each finding.",
        "A promise with no date is not a failure of the detector, it is a real problem: "
        "an undated commitment cannot be tracked, chased, or kept accountable. That count "
        "is a coaching metric, not a bug report.",
        "`unknown` means the thread has no evidence either way. On Path A there is no way "
        "to check a portal or a phone call, which is one more argument for the mail "
        "migration: see docs/mail-path-decision.md.",
    ]
    return report


def _judge(store: Store, promise, promised_at: dt.datetime | None,
           due_at: dt.datetime | None, now: dt.datetime, grace: float) -> tuple[str, int | None]:
    """Kept, broken, open, or unknown, plus the message that discharged it.

    Follow-through is any later outbound message in the thread that delivers something, or
    any later inbound message that acknowledges receipt. Both count, because a customer
    saying "got it, thanks" is stronger evidence than our own claim to have sent it.
    """
    if promised_at is None:
        return "unknown", None

    later = [
        m for m in store.messages_in_thread(promise["thread_key"])
        if (parse_ts(m["sent_at"]) or promised_at) > promised_at
    ]

    for message in later:
        text = message["body_text"] or message["snippet"] or ""
        if message["direction"] in ("outbound", "internal"):
            if rules.looks_like_followthrough(text, bool(message["has_attachments"])):
                return "kept", message["id"]
        elif rules.looks_like_closer(text) or "received" in text.lower():
            return "kept", message["id"]

    if due_at is None:
        # Undated. Nothing to breach, so it can never be called broken.
        return "unknown", None

    deadline = due_at + dt.timedelta(hours=grace)
    if now <= deadline:
        return "open", None

    # Past due with nothing delivered. Only call it broken when the thread carried on
    # afterward, which means we were present and still did not deliver. A thread that
    # simply stopped may have moved to a phone call, so it stays unknown.
    if later:
        return "broken", None
    return "unknown", None
