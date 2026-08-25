"""Vendor Silence Report.

Outbound vendor and OEM requests with no reply, aged against the relevant solicitation or
delivery clock rather than a fixed interval.

This is the Marotta pattern made visible. An OEM sits on a quote, the solicitation clock
runs out, and the bid is lost. A report that ages vendor silence on a fixed 72-hour
interval would rank a request with three weeks of runway alongside one that closes
tomorrow. So the sort key here is time remaining until the external deadline, not time
elapsed since we asked.
"""

from __future__ import annotations

import datetime as dt

from ..clock import backward_checkpoints
from ..config import Config
from ..store import Store, parse_ts
from .render import Report, freshness_lines, hours_between

VENDOR_CLASSES = ("oem", "distributor", "unknown")


def run(cfg: Config, store: Store, now: dt.datetime | None = None) -> Report:
    now = now or dt.datetime.now(dt.timezone.utc)
    default_hours = float(cfg.phase0("vendor_silence_default_hours", 72))

    lines, stale = freshness_lines(cfg, store, now)
    report = Report(
        key="vendor-silence",
        title="Vendor Silence Report",
        subtitle=(
            "Outbound vendor and OEM requests with no reply, aged against the solicitation "
            "or delivery clock. Sorted by time remaining, not by time waited."
        ),
        columns=["urgency", "closes_in", "silent_for", "counterparty", "class", "subject",
                 "reference", "chases_left", "thread"],
        freshness=lines,
        stale=stale,
        generated_at=now,
    )

    threads = store.query(
        """
        SELECT t.*, m.sent_at AS last_out_at, m.subject AS last_subject
        FROM threads t
        JOIN messages m ON m.thread_key = t.thread_key
        WHERE t.is_external = 1
          AND t.last_direction IN ('outbound')
          AND m.sent_at = t.last_at
          AND t.counterparty_class IN (?, ?, ?)
        """,
        VENDOR_CLASSES,
    )

    deadline_driven = 0
    at_risk = 0

    for thread in threads:
        last_out = parse_ts(thread["last_out_at"])
        if last_out is None:
            continue
        silent_for = hours_between(last_out, now)

        close_at = parse_ts(thread["solicitation_close_at"])
        if close_at is not None:
            deadline_driven += 1
            remaining = (close_at - now).total_seconds() / 3600
            sla = cfg.sla_for("vendor_quote", thread["counterparty_class"])
            checkpoints = backward_checkpoints(
                close_at, sla.get("backward_checkpoints", []) or [], now=now
            )
            urgency = _urgency(remaining)
            closes_in = f"{remaining:.0f}h" if remaining > 0 else f"CLOSED {abs(remaining):.0f}h ago"
            chases_left = str(len(checkpoints))
        else:
            # No external clock found, so fall back to the fixed interval and say so. A
            # missing close date is itself a finding: the chase cadence cannot be planned.
            if silent_for < default_hours:
                continue
            remaining = float("inf")
            urgency = "no deadline known"
            closes_in = "unknown"
            chases_left = "n/a"

        if urgency in ("critical", "high"):
            at_risk += 1

        report.rows.append({
            "urgency": urgency,
            "closes_in": closes_in,
            "silent_for": f"{silent_for:.0f}h",
            "counterparty": thread["counterparty"] or "(unknown)",
            "class": thread["counterparty_class"],
            "subject": (thread["last_subject"] or "")[:60],
            "reference": thread["solicitation_ref"] or thread["contract_ref"] or "",
            "chases_left": chases_left,
            "thread": thread["thread_key"],
            "_remaining": remaining,
        })

    # Least runway first. A closed solicitation sorts to the very top: that is a lost bid,
    # and it is the most important row on the page.
    report.rows.sort(key=lambda r: float(r["_remaining"]))
    for row in report.rows:
        row.pop("_remaining", None)

    report.metrics = {
        "silent vendor threads": len(report.rows),
        "with a known external deadline": deadline_driven,
        "critical or high urgency": at_risk,
        "no deadline found, aged on the fixed fallback": len(report.rows) - deadline_driven,
    }
    report.notes = [
        "Rows with `closes_in = unknown` are the real problem. Without a close date the "
        "chase cannot be planned backward, so the request sits on a generic interval. "
        "Capturing the close date at solicitation intake fixes this permanently.",
        "`chases_left` counts the backward-planned checkpoints in sla.yaml that are still "
        "in the future. Zero means chasing is over and escalation to a named human at the "
        "OEM is the only remaining move, which is exactly the Marotta failure.",
        "Escalation should reach a named contact, not a shared sales alias. "
        "`named_escalation_contacts` in counterparties.yaml is still empty.",
    ]
    return report


def _urgency(hours_remaining: float) -> str:
    if hours_remaining <= 0:
        return "closed"
    if hours_remaining <= 24:
        return "critical"
    if hours_remaining <= 72:
        return "high"
    if hours_remaining <= 168:
        return "medium"
    return "low"
