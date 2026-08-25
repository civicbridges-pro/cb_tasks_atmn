"""Unanswered Thread Report.

Every external thread where the last message is inbound and older than the threshold.

Sorted by counterparty importance, not by date. A contracting officer waiting six hours
outranks a distributor waiting six days, and a report sorted by age buries the one that
costs money. This is the single most important design choice in this file.

Threads whose last inbound message reads as a closer ("thanks, got it") are separated out
rather than dropped, so the headline count stays honest without hiding the judgment call.
"""

from __future__ import annotations

import datetime as dt
import json

from ..config import Config
from ..extract import rules
from ..store import Store, parse_ts
from .render import Report, freshness_lines, hours_between


def run(cfg: Config, store: Store, now: dt.datetime | None = None,
        threshold_hours: float | None = None) -> Report:
    now = now or dt.datetime.now(dt.timezone.utc)
    threshold = float(
        threshold_hours if threshold_hours is not None else cfg.phase0("unanswered_after_hours", 48)
    )

    lines, stale = freshness_lines(cfg, store, now)
    report = Report(
        key="unanswered",
        title="Unanswered Thread Report",
        subtitle=(
            f"External threads whose last message is inbound and older than {threshold:g} "
            "hours. Sorted by counterparty importance, not by age."
        ),
        columns=["importance", "counterparty", "class", "waiting", "subject", "contract",
                 "last_from", "asked", "thread"],
        freshness=lines,
        stale=stale,
        generated_at=now,
    )

    threads = store.query(
        """
        SELECT t.*, m.from_addr AS last_from, m.body_text AS last_body,
               m.snippet AS last_snippet, m.id AS last_message_id
        FROM threads t
        JOIN messages m ON m.thread_key = t.thread_key
        WHERE t.is_external = 1
          AND t.last_direction = 'inbound'
          AND m.sent_at = t.last_at
        GROUP BY t.thread_key
        """
    )

    closers: list[dict[str, object]] = []
    for thread in threads:
        last_at = parse_ts(thread["last_at"])
        if last_at is None:
            continue
        waiting = hours_between(last_at, now)
        if waiting < threshold:
            continue

        body = thread["last_body"] or thread["last_snippet"] or ""
        row = {
            "importance": cfg.importance(thread["counterparty_class"] or "unknown"),
            "counterparty": thread["counterparty"] or "(unknown)",
            "class": thread["counterparty_class"] or "unknown",
            "waiting": f"{waiting:.0f}h",
            "subject": (thread["subject"] or "(no subject)")[:70],
            "contract": thread["contract_ref"] or "",
            "last_from": thread["last_from"],
            "asked": "yes" if rules.contains_ask(body) else "no",
            "thread": thread["thread_key"],
            "_waiting_hours": waiting,
        }
        if rules.looks_like_closer(body) and not rules.contains_ask(body):
            closers.append(row)
        else:
            report.rows.append(row)

    # Importance first, then age. Within one counterparty class, oldest is worst.
    report.rows.sort(key=lambda r: (-int(r["importance"]), -float(r["_waiting_hours"])))
    for row in report.rows:
        row.pop("_waiting_hours", None)

    by_class: dict[str, int] = {}
    for row in report.rows:
        by_class[str(row["class"])] = by_class.get(str(row["class"]), 0) + 1

    report.metrics = {
        "unanswered threads": len(report.rows),
        "of those, government": by_class.get("gov", 0),
        "oldest": max((r["waiting"] for r in report.rows), default="none"),
        "by counterparty class": json.dumps(by_class, sort_keys=True),
        "excluded as closers": len(closers),
    }
    report.notes = [
        "Target for the government row is zero. Not low. Zero.",
        f"{len(closers)} thread(s) excluded because the last inbound message reads as an "
        "acknowledgment with no ask. Those are the ones a human should spot-check first: "
        "if the classifier is wrong here, real obligations are being hidden.",
        "A thread with `asked = no` still counts. Silence after an inbound message is a "
        "relationship signal whether or not a question mark was involved.",
    ]
    if closers:
        report.notes.append(
            "Excluded threads: " + ", ".join(str(c["thread"]) for c in closers[:20])
        )
    return report
