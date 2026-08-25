"""Dropped Handoff Report.

Internal threads where work was passed to a named person and nothing happened after.

Two seams get called out explicitly because the business already knows they leak: the
overseas boundary with Taj and Usman, and the procurement-to-logistics seam. A handoff
across an offset-hours boundary is not automatically a problem, it is a normal delay, so
the report ages those against the receiver's own next working window rather than wall clock.
Reporting a handoff as dropped because the receiver was asleep is how a team learns to
ignore a report.
"""

from __future__ import annotations

import datetime as dt

from ..clock import add_business_hours, coverage_for
from ..config import Config
from ..store import Store, parse_ts
from .render import Report, freshness_lines, hours_between

# The procurement-to-logistics seam, by role. A handoff crossing it gets flagged even when
# it is inside the normal SLA, because this is where the business already sees drops.
SEAM_FROM = {"joe", "jason", "usman", "morgan"}
SEAM_TO = {"roy", "taj"}


def run(cfg: Config, store: Store, now: dt.datetime | None = None) -> Report:
    now = now or dt.datetime.now(dt.timezone.utc)
    threshold = float(cfg.phase0("handoff_silence_hours", 48))

    lines, stale = freshness_lines(cfg, store, now)
    report = Report(
        key="handoffs",
        title="Dropped Handoff Report",
        subtitle=(
            "Internal work passed to a named person with no response after. Offset-hours "
            f"receivers are aged against their own coverage window, not wall clock. "
            f"Threshold {threshold:g} coverage hours."
        ),
        columns=["to", "waited", "seam", "from", "handoff", "subject", "thread", "confidence"],
        freshness=lines,
        stale=stale,
        generated_at=now,
    )

    handoffs = store.query(
        """
        SELECT h.*, m.sent_at, m.from_addr, m.subject, m.mailbox
        FROM handoffs h JOIN messages m ON m.id = h.message_id
        ORDER BY m.sent_at
        """
    )

    offset_count = 0
    seam_count = 0

    for handoff in handoffs:
        sent_at = parse_ts(handoff["sent_at"])
        if sent_at is None:
            continue

        responder = _first_response(store, handoff, sent_at, cfg)
        if responder is not None:
            continue

        coverage = coverage_for(cfg, handoff["to_person"])
        # Aged against the receiver's coverage: the deadline is `threshold` working hours
        # after the handoff, in their timezone. For an unconfirmed coverage window this
        # falls back to wall clock and the note says so.
        deadline = add_business_hours(coverage, sent_at, threshold)
        if now < deadline:
            continue

        crosses = _crosses_seam(cfg, handoff["from_addr"], handoff["to_person"])
        if handoff["crosses_offset_boundary"]:
            offset_count += 1
        if crosses:
            seam_count += 1

        report.rows.append({
            "to": handoff["to_person"],
            "waited": f"{hours_between(sent_at, now):.0f}h",
            "seam": ", ".join(
                filter(None, [
                    "overseas" if handoff["crosses_offset_boundary"] else "",
                    "proc→logistics" if crosses else "",
                    "" if coverage.known else "coverage unknown",
                ])
            ),
            "from": handoff["from_addr"],
            "handoff": (handoff["text"] or "")[:90],
            "subject": (handoff["subject"] or "")[:50],
            "thread": handoff["thread_key"],
            "confidence": handoff["confidence"],
        })

    report.rows.sort(key=lambda r: -float(str(r["waited"]).rstrip("h")))

    by_person: dict[str, int] = {}
    for row in report.rows:
        by_person[str(row["to"])] = by_person.get(str(row["to"]), 0) + 1

    report.metrics = {
        "dropped handoffs": len(report.rows),
        "crossing the overseas boundary": offset_count,
        "crossing procurement to logistics": seam_count,
        "by receiver": ", ".join(f"{k}={v}" for k, v in sorted(by_person.items())) or "none",
    }
    report.notes = [
        "A handoff counts as answered by any later message from the receiver in that "
        "thread. Acting without replying therefore reads as dropped, which is itself worth "
        "knowing: an obligation nobody can see the status of is not tracked.",
        "Receivers whose coverage hours are still TODO_CONFIRM in people.yaml are aged "
        "against wall clock and marked `coverage unknown`. Those rows may be false "
        "positives, and confirming the config removes the doubt.",
        "The overseas boundary is a speed advantage when the queue is loaded before their "
        "day starts. Rows tagged `overseas` are the ones where that failed.",
    ]
    return report


def _first_response(store: Store, handoff, sent_at: dt.datetime, cfg: Config):
    """Any later message in the thread from the person the work was handed to."""
    try:
        expected = str(cfg.person(handoff["to_person"]).get("email", "")).lower()
    except Exception:
        expected = ""
    first_name = handoff["to_person"].lower()

    for message in store.messages_in_thread(handoff["thread_key"]):
        stamp = parse_ts(message["sent_at"])
        if stamp is None or stamp <= sent_at:
            continue
        sender = (message["from_addr"] or "").lower()
        # Match on the configured address when it is known, and fall back to the local
        # part containing the person's id, which is how internal addresses read here.
        if expected and expected != "todo_confirm" and sender == expected:
            return message
        if not expected or expected == "todo_confirm":
            if sender.split("@")[0].startswith(first_name):
                return message
    return None


def _crosses_seam(cfg: Config, from_addr: str, to_person: str) -> bool:
    local = (from_addr or "").split("@")[0].lower()
    from_person = next((p for p in cfg.person_ids if local.startswith(p)), None)
    return bool(from_person and from_person in SEAM_FROM and to_person in SEAM_TO)
