"""`./cb owed`: the Phase 1 success test, as one command.

> At any moment you can answer "what does this company owe, to whom, by when" without
> asking a person.

That sentence is the entire exit test for Phase 1, so it deserves to be a single command
whose output can be read out loud in a meeting. Everything else in Phase 1 exists to make
this answer correct.

The view is split by direction, because the two halves need different actions: what we owe
is work, and what they owe us is a chase. Merging them into one list is how a company ends
up chasing itself.
"""

from __future__ import annotations

import datetime as dt

from .config import Config
from .reports.render import Report, freshness_lines
from .store import Store, parse_ts


def run(cfg: Config, store: Store, now: dt.datetime | None = None,
        owner: str | None = None, counterparty: str | None = None,
        contract: str | None = None) -> Report:
    now = now or dt.datetime.now(dt.timezone.utc)
    lines, stale = freshness_lines(cfg, store, now)

    filters = []
    params: list[object] = []
    if owner:
        filters.append("owner = ?")
        params.append(owner)
    if counterparty:
        filters.append("counterparty LIKE ?")
        params.append(f"%{counterparty}%")
    if contract:
        filters.append("contract_ref LIKE ?")
        params.append(f"%{contract}%")
    where = (" AND " + " AND ".join(filters)) if filters else ""

    scope = ", ".join(
        filter(None, [
            f"owner={owner}" if owner else "",
            f"counterparty~{counterparty}" if counterparty else "",
            f"contract~{contract}" if contract else "",
        ])
    )

    report = Report(
        key="owed",
        title="What this company owes",
        subtitle=(
            "Every open obligation, split by direction and ordered by what breaks first."
            + (f" Filtered: {scope}." if scope else "")
        ),
        columns=["direction", "due", "state", "owner", "counterparty", "what", "contract",
                 "next_move"],
        freshness=lines, stale=stale, generated_at=now,
    )

    obligations = store.query(
        "SELECT * FROM obligations WHERE status NOT IN ('done','dropped')" + where
        + " ORDER BY due_at IS NULL, due_at",
        params,
    )

    we_owe = 0
    they_owe = 0
    overdue = 0
    undated = 0

    for obligation in obligations:
        due = parse_ts(obligation["due_at"])
        if due is None:
            undated += 1
            due_text = "no date"
        else:
            hours = (due - now).total_seconds() / 3600
            if hours < 0:
                overdue += 1
                due_text = f"{abs(hours):.0f}h overdue"
            elif hours < 48:
                due_text = f"in {hours:.0f}h"
            else:
                due_text = due.date().isoformat()

        if obligation["direction"] == "we_owe_them":
            we_owe += 1
        else:
            they_owe += 1

        report.rows.append({
            "direction": "we owe" if obligation["direction"] == "we_owe_them" else "they owe",
            "due": due_text,
            "state": obligation["status"],
            "owner": obligation["owner"],
            "counterparty": obligation["counterparty"] or "",
            "what": obligation["what_is_owed"],
            "contract": obligation["contract_ref"] or "",
            "next_move": _next_move(obligation, now),
        })

    report.metrics = {
        "open obligations": len(obligations),
        "we owe them": we_owe,
        "they owe us": they_owe,
        "overdue": overdue,
        "no due date": undated,
    }
    report.notes = [
        "`they owe` lines are `waiting_external`, which is not done: each one carries a "
        "clock and decays into a chase.",
        "A line with no due date is a real gap, not a formatting quirk. An obligation "
        "without a date cannot be chased on a plan, only on somebody remembering.",
    ]
    if undated:
        report.notes.append(
            f"{undated} obligation(s) have no due date. Most are deadline-driven types where "
            "no external date was captured; see the audit report."
        )
    return report


def _next_move(obligation, now: dt.datetime) -> str:
    if obligation["needs_human_review"]:
        return "confirm the classification"
    if obligation["status"] == "waiting_external":
        chase = parse_ts(obligation["next_chase_at"])
        if chase is None:
            return "escalate, chasing is spent"
        hours = (chase - now).total_seconds() / 3600
        return "chase now" if hours <= 0 else f"chase in {hours:.0f}h"
    if obligation["status"] == "blocked":
        return "unblock"
    response = parse_ts(obligation["sla_response_at"])
    if response is not None and response <= now:
        return "respond, past the clock"
    return "respond"
