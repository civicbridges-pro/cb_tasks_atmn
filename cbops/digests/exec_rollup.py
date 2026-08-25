"""Exec rollup for Doug and Anna.

Not a longer personal digest. It answers three questions, in this order:

1.  Is the system telling the truth right now? (capture freshness, audit criticals)
2.  What is about to cost money? (gov silence, solicitations closing, overdue by owner)
3.  What needs a decision rather than a nudge? (exhausted escalations, unowned work)

Everything on it is either a number that should be zero or a thing waiting on one of them.
A rollup that lists work already moving correctly trains its readers to skim it.
"""

from __future__ import annotations

import datetime as dt

from ..agents import auditor
from ..config import Config
from ..reports.render import Report, freshness_lines, median
from ..store import Store, parse_ts


def run(cfg: Config, store: Store, now: dt.datetime | None = None) -> Report:
    now = now or dt.datetime.now(dt.timezone.utc)
    lines, stale = freshness_lines(cfg, store, now)

    report = Report(
        key="digest-exec",
        title="Exec rollup",
        subtitle=(
            "What the company owes, what is about to break, and what needs a decision. "
            "Numbers that should be zero are marked."
        ),
        columns=["priority", "item", "detail", "owner"],
        freshness=lines, stale=stale, generated_at=now,
    )

    open_obligations = store.query(
        "SELECT * FROM obligations WHERE status NOT IN ('done','dropped')"
    )

    # 1. Is the system telling the truth?
    audit = auditor.run(cfg, store, now)
    criticals = [row for row in audit.rows if row["severity"] == "critical"]
    for row in criticals[:10]:
        report.rows.append({
            "priority": "TRUST", "item": row["check"],
            "detail": f"{row['entity']}: {row['detail']}", "owner": "",
        })

    # 2. What is about to cost money?
    for obligation in sorted(
        (o for o in open_obligations if _overdue_hours(o, now) > 0),
        key=lambda o: -_overdue_hours(o, now),
    )[:10]:
        report.rows.append({
            "priority": "OVERDUE",
            "item": f"{obligation['type']} / {obligation['counterparty'] or 'unknown'}",
            "detail": f"{_overdue_hours(obligation, now):.0f}h past due: "
                      f"{obligation['what_is_owed'][:70]}",
            "owner": obligation["owner"],
        })

    gov_silent = store.query(
        "SELECT COUNT(*) AS n FROM threads WHERE is_external = 1 "
        "AND counterparty_class = 'gov' AND last_direction = 'inbound' AND last_at <= ?",
        ((now - dt.timedelta(hours=float(cfg.phase0("unanswered_after_hours", 48)))).isoformat(),),
    )[0]["n"]
    if gov_silent:
        report.rows.append({
            "priority": "GOV", "item": "unanswered government threads",
            "detail": f"{gov_silent} thread(s) past the 48 hour mark. Target is zero, not low.",
            "owner": "doug",
        })

    closing = [
        row for row in store.query(
            "SELECT thread_key, counterparty, solicitation_ref, solicitation_close_at "
            "FROM threads WHERE solicitation_close_at IS NOT NULL AND last_direction = 'outbound'"
        )
        if 0 < _hours_until(row["solicitation_close_at"], now) <= 72
    ]
    for row in closing:
        report.rows.append({
            "priority": "CLOSING",
            "item": f"{row['solicitation_ref'] or 'solicitation'} / {row['counterparty']}",
            "detail": f"closes in {_hours_until(row['solicitation_close_at'], now):.0f}h with "
                      "no reply from the vendor",
            "owner": "jason",
        })

    # 3. What needs a decision rather than a nudge?
    exhausted = [
        row for row in audit.rows if row["check"] == "escalation_exhausted"
    ]
    for row in exhausted[:10]:
        report.rows.append({
            "priority": "DECIDE", "item": "escalation exhausted",
            "detail": f"{row['entity']}: chasing is over, this needs a decision", "owner": "",
        })

    unowned = [o for o in open_obligations if o["owner"] == "triage_queue"]
    if unowned:
        report.rows.append({
            "priority": "DECIDE", "item": "unowned obligations",
            "detail": f"{len(unowned)} in the triage queue with no named owner. "
                      "Should be zero by end of day.",
            "owner": "doug",
        })

    review = [o for o in open_obligations if o["needs_human_review"]]
    if review:
        report.rows.append({
            "priority": "REVIEW", "item": "awaiting human confirmation",
            "detail": f"{len(review)} obligation(s), including every stop-work, contract "
                      "action, and award, which always reach a human",
            "owner": "",
        })

    by_owner: dict[str, int] = {}
    for obligation in open_obligations:
        by_owner[obligation["owner"]] = by_owner.get(obligation["owner"], 0) + 1

    overdue_all = [_overdue_hours(o, now) for o in open_obligations if _overdue_hours(o, now) > 0]
    report.metrics = {
        "open obligations": len(open_obligations),
        "overdue": len(overdue_all),
        "median hours overdue": median(overdue_all) or 0,
        "unowned (target zero)": len(unowned),
        "unanswered government threads (target zero)": gov_silent,
        "awaiting human review": len(review),
        "audit criticals (target zero)": len(criticals),
        "load by owner": ", ".join(f"{k}={v}" for k, v in sorted(by_owner.items())) or "none",
    }

    report.notes = [
        "The Phase 1 success test: at any moment you can answer what this company owes, to "
        "whom, by when, without asking a person. Run `./cb owed` for that answer in full.",
        "Nothing in Phase 1 sends. Every chase on a personal digest is a human action. "
        "Approve-and-send drafts arrive in Phase 2.",
    ]
    if criticals:
        report.notes.insert(0,
            "TRUST rows come first for a reason: while an invariant is broken or capture is "
            "stale, every other number on this page is a floor rather than a total.")
    return report


def _overdue_hours(obligation, now: dt.datetime) -> float:
    due = parse_ts(obligation["due_at"])
    return 0.0 if due is None else max(0.0, (now - due).total_seconds() / 3600)


def _hours_until(target: str | None, now: dt.datetime) -> float:
    stamp = parse_ts(target)
    return 0.0 if stamp is None else (stamp - now).total_seconds() / 3600
