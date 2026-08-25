"""Baseline metrics.

The numbers that turn Phase 0 from four lists into an argument. These are measured once at
the start and then tracked, so Phase 2's success test ("median external response under 4
business hours") has something to be measured against.

Every metric here is deliberately computed from captured mail only. Nothing is inferred
from Zoho, because the whole premise of the program is that the obligations are not in Zoho
yet. Where a number cannot be computed from mail, it says so rather than reporting zero:
zero and unmeasurable look identical on a dashboard and mean opposite things.
"""

from __future__ import annotations

import datetime as dt

from ..config import Config
from ..store import Store, parse_ts
from .render import Report, freshness_lines, hours_between, median


def run(cfg: Config, store: Store, now: dt.datetime | None = None) -> Report:
    now = now or dt.datetime.now(dt.timezone.utc)
    lines, stale = freshness_lines(cfg, store, now)

    report = Report(
        key="baseline",
        title="Phase 0 Baseline Metrics",
        subtitle=(
            "Measured from captured mail only. Nothing here is inferred from Zoho, because "
            "the premise of the program is that the obligations are not in Zoho yet."
        ),
        columns=["metric", "value", "sample", "note"],
        freshness=lines,
        stale=stale,
        generated_at=now,
    )

    total = store.query("SELECT COUNT(*) AS n FROM messages")[0]["n"]
    quarantined = store.query("SELECT COUNT(*) AS n FROM messages WHERE quarantined = 1")[0]["n"]

    report.metrics = {
        "messages captured": total,
        "quarantined before ingest": quarantined,
        "threads": store.query("SELECT COUNT(*) AS n FROM threads")[0]["n"],
        "external threads": store.query(
            "SELECT COUNT(*) AS n FROM threads WHERE is_external = 1")[0]["n"],
    }

    report.rows.extend(_response_times(store, now))
    report.rows.append(_unowned_threads(store))
    report.rows.extend(_quote_turnaround(store))
    report.rows.append(_award_to_po(store))
    report.rows.append(_undated_promises(store))
    report.rows.append(_compliance_horizon(cfg, store, now))

    report.notes = [
        "Median, not mean. One three-week outlier should not flatter or wreck the number.",
        "First response is measured against the first inbound message in a thread, in wall "
        "clock hours. Phase 2's target of four *business* hours is a stricter test, and "
        "the business-hours version becomes meaningful once coverage_hours in people.yaml "
        "are confirmed.",
        "`unmeasurable` is a real answer and appears wherever mail alone cannot produce "
        "the number. Award-to-PO needs the Zoho link, and quote turnaround needs the "
        "solicitation intake date, which today lives in a portal nobody records.",
    ]
    return report


def _response_times(store: Store, now: dt.datetime) -> list[dict[str, object]]:
    """Median first-response time by mailbox, from first inbound to first reply out."""
    rows: list[dict[str, object]] = []
    mailboxes = store.query(
        "SELECT DISTINCT mailbox FROM messages WHERE mailbox IS NOT NULL ORDER BY mailbox"
    )
    overall: list[float] = []

    for entry in mailboxes:
        mailbox = entry["mailbox"]
        durations: list[float] = []
        threads = store.query(
            "SELECT DISTINCT thread_key FROM messages WHERE mailbox = ?", (mailbox,)
        )
        for thread in threads:
            messages = store.messages_in_thread(thread["thread_key"])
            first_in = next((m for m in messages if m["direction"] == "inbound"), None)
            if first_in is None:
                continue
            first_in_at = parse_ts(first_in["sent_at"])
            reply = next(
                (
                    m for m in messages
                    if m["direction"] == "outbound"
                    and (parse_ts(m["sent_at"]) or now) > (first_in_at or now)
                ),
                None,
            )
            if reply is None or first_in_at is None:
                continue
            durations.append(hours_between(first_in_at, parse_ts(reply["sent_at"]) or now))

        overall.extend(durations)
        rows.append({
            "metric": f"median first response, {mailbox}",
            "value": f"{median(durations)}h" if durations else "unmeasurable",
            "sample": len(durations),
            "note": "" if durations else "no thread in this mailbox has an inbound then a reply",
        })

    rows.insert(0, {
        "metric": "median first response, all mailboxes",
        "value": f"{median(overall)}h" if overall else "unmeasurable",
        "sample": len(overall),
        "note": "wall clock hours, not business hours",
    })
    return rows


def _unowned_threads(store: Store) -> dict[str, object]:
    """External threads with nobody assigned. Phase 0 expects this to be nearly all of them."""
    total = store.query("SELECT COUNT(*) AS n FROM threads WHERE is_external = 1")[0]["n"]
    unowned = store.query(
        "SELECT COUNT(*) AS n FROM threads WHERE is_external = 1 AND owner_person IS NULL"
    )[0]["n"]
    share = f"{(100 * unowned / total):.0f}%" if total else "n/a"
    return {
        "metric": "external threads with no owner",
        "value": f"{unowned} of {total} ({share})",
        "sample": total,
        "note": "expected to be ~100% in Phase 0; this is the gap the ledger closes",
    }


def _quote_turnaround(store: Store) -> list[dict[str, object]]:
    """Solicitation received to quote submitted, where both ends are visible in mail."""
    threads = store.query(
        "SELECT thread_key, solicitation_close_at FROM threads WHERE solicitation_ref "
        "IS NOT NULL OR solicitation_close_at IS NOT NULL"
    )
    durations: list[float] = []
    for thread in threads:
        messages = store.messages_in_thread(thread["thread_key"])
        first_in = next((m for m in messages if m["direction"] == "inbound"), None)
        quote_out = next(
            (
                m for m in messages
                if m["direction"] == "outbound"
                and "quote" in ((m["subject"] or "") + (m["body_text"] or "")).lower()
            ),
            None,
        )
        if first_in and quote_out:
            start = parse_ts(first_in["sent_at"])
            end = parse_ts(quote_out["sent_at"])
            if start and end and end > start:
                durations.append(hours_between(start, end))
    return [{
        "metric": "quote turnaround, solicitation seen to quote sent",
        "value": f"{median(durations)}h" if durations else "unmeasurable",
        "sample": len(durations),
        "note": "needs solicitation intake dates from DIBBS; the paste bridge supplies them",
    }]


def _award_to_po(store: Store) -> dict[str, object]:
    """Award notification to vendor PO issued. The metric where margin quietly leaks."""
    awards = store.query(
        "SELECT COUNT(*) AS n FROM messages WHERE LOWER(subject) LIKE '%award%'"
    )[0]["n"]
    return {
        "metric": "award to vendor PO issued",
        "value": "unmeasurable",
        "sample": awards,
        "note": (
            "needs the Zoho link to know when a PO was actually issued; mail alone cannot "
            "prove it. Instrument this first in Phase 1, it has a hard SLA in sla.yaml"
        ),
    }


def _undated_promises(store: Store) -> dict[str, object]:
    total = store.query("SELECT COUNT(*) AS n FROM promises")[0]["n"]
    undated = store.query("SELECT COUNT(*) AS n FROM promises WHERE due_at IS NULL")[0]["n"]
    share = f"{(100 * undated / total):.0f}%" if total else "n/a"
    return {
        "metric": "commitments made with no date",
        "value": f"{undated} of {total} ({share})",
        "sample": total,
        "note": "an undated commitment cannot be tracked, chased, or kept; coaching metric",
    }


def _compliance_horizon(cfg: Config, store: Store, now: dt.datetime) -> dict[str, object]:
    """Compliance items expiring inside 90 days. Target is zero unowned."""
    rows = store.query(
        "SELECT COUNT(*) AS n FROM obligations WHERE type = 'compliance' "
        "AND status NOT IN ('done','dropped') AND due_at IS NOT NULL AND due_at <= ?",
        ((now + dt.timedelta(days=90)).isoformat(),),
    )
    return {
        "metric": "compliance items expiring inside 90 days",
        "value": rows[0]["n"],
        "sample": rows[0]["n"],
        "note": (
            "zero here means the calendar is not built yet, not that nothing expires. "
            "SAM, WOSB, D&B, insurance, and state registrations are all date driven and "
            "catastrophic if missed. Build cert-calendar early, it is cheap"
        ),
    }
