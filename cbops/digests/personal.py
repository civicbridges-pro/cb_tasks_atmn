"""One person's daily digest.

Ordered by what breaks first, not by what arrived first. The sections exist in this order
because that is the order a person should act in, and a digest that buries the overdue item
under six new ones is a digest that gets skimmed.

Anything the reader cannot act on is left out. Their unconfirmed coverage hours, the state
of the mail sync, the config placeholders: all real problems, none of them theirs.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..clock import coverage_for
from ..config import Config
from ..reports.render import Report, freshness_lines
from ..store import Store, parse_ts


def _hours_until(target: str | None, now: dt.datetime) -> float | None:
    stamp = parse_ts(target)
    return None if stamp is None else (stamp - now).total_seconds() / 3600


def _when(hours: float | None) -> str:
    if hours is None:
        return "no date"
    if hours < 0:
        return f"{abs(hours):.0f}h overdue"
    if hours < 24:
        return f"in {hours:.0f}h"
    return f"in {hours / 24:.0f}d"


def run(cfg: Config, store: Store, person: str,
        now: dt.datetime | None = None) -> Report:
    """Build one person's digest. `person` must be an id from people.yaml."""
    now = now or dt.datetime.now(dt.timezone.utc)
    name = cfg.person(person).get("name", person) if cfg.is_person(person) else person
    lines, stale = freshness_lines(cfg, store, now)

    report = Report(
        key=f"digest-{person}",
        title=f"Daily digest: {name}",
        subtitle=(
            "What you owe, ordered by what breaks first. Nothing in this digest was sent on "
            "your behalf: every line is an action for you."
        ),
        columns=["act", "when", "what", "counterparty", "contract", "obligation"],
        freshness=lines, stale=stale, generated_at=now,
    )

    obligations = store.query(
        "SELECT * FROM obligations WHERE owner = ? AND status NOT IN ('done','dropped') "
        "ORDER BY due_at IS NULL, due_at",
        (person,),
    )

    buckets = {
        "OVERDUE": [], "RESPOND": [], "TODAY": [], "CHASE": [],
        "ESCALATE": [], "REVIEW": [], "WAITING": [], "UPCOMING": [],
    }

    for obligation in obligations:
        due_in = _hours_until(obligation["due_at"], now)
        respond_in = _hours_until(obligation["sla_response_at"], now)
        chase_in = _hours_until(obligation["next_chase_at"], now)

        if obligation["needs_human_review"]:
            bucket, marker = "REVIEW", respond_in if respond_in is not None else due_in
        elif due_in is not None and due_in < 0:
            bucket, marker = "OVERDUE", due_in
        elif respond_in is not None and respond_in < 0:
            bucket, marker = "RESPOND", respond_in
        elif obligation["status"] == "waiting_external" and obligation["next_chase_at"] is None:
            bucket, marker = "ESCALATE", due_in
        elif chase_in is not None and chase_in <= 24:
            bucket, marker = "CHASE", chase_in
        elif due_in is not None and due_in <= 24:
            bucket, marker = "TODAY", due_in
        elif obligation["status"] == "waiting_external":
            bucket, marker = "WAITING", chase_in
        else:
            bucket, marker = "UPCOMING", due_in

        buckets[bucket].append((marker if marker is not None else 1e9, obligation))

    order = ["OVERDUE", "RESPOND", "REVIEW", "ESCALATE", "CHASE", "TODAY", "WAITING", "UPCOMING"]
    for bucket in order:
        for marker, obligation in sorted(buckets[bucket], key=lambda item: item[0]):
            report.rows.append({
                "act": bucket,
                "when": _when(marker if marker < 1e8 else None),
                "what": obligation["what_is_owed"],
                "counterparty": obligation["counterparty"] or "",
                "contract": obligation["contract_ref"] or "",
                "obligation": obligation["id"],
            })

    report.metrics = {
        "on your plate": len(obligations),
        "overdue": len(buckets["OVERDUE"]),
        "past the response clock": len(buckets["RESPOND"]),
        "needs your decision": len(buckets["REVIEW"]) + len(buckets["ESCALATE"]),
        "chase today": len(buckets["CHASE"]),
        "waiting on someone else": len(buckets["WAITING"]),
    }

    notes = []
    if buckets["ESCALATE"]:
        notes.append(
            "ESCALATE means the chase cadence is spent. Another follow-up will not move it; "
            "somebody has to decide something."
        )
    if buckets["REVIEW"]:
        notes.append(
            "REVIEW means the system is not confident enough to route this on its own, or "
            "the type always needs a human. Confirm or correct it."
        )
    if buckets["WAITING"]:
        notes.append(
            "WAITING still has a clock. It is not done, and it will come back as a chase."
        )
    if cfg.is_person(person) and not coverage_for(cfg, person).known:
        notes.append(
            "Your coverage hours are still unconfirmed in config/people.yaml, so the clocks "
            "above run on wall time and may read as late when they are not. Worth fixing."
        )
    if not obligations:
        notes.append("Nothing open. If that seems wrong, check the data freshness above.")
    report.notes = notes
    return report


def everyone(cfg: Config, store: Store, now: dt.datetime | None = None) -> list[Report]:
    """A digest for every owner in people.yaml, plus the triage queue.

    The triage queue gets one too. An unowned obligation that nobody is shown is exactly
    the failure this program exists to end.
    """
    people = list(cfg.people.get("groups", {}).get("all_owners") or sorted(cfg.person_ids))
    reports = [run(cfg, store, person, now) for person in people]
    reports.append(_triage_queue(cfg, store, now))
    return reports


def _triage_queue(cfg: Config, store: Store, now: dt.datetime | None) -> Report:
    report = run(cfg, store, "triage_queue", now)
    report.title = "Daily digest: triage queue (unowned)"
    report.subtitle = (
        "Obligations the system would not guess an owner for. Every line here needs a human "
        "to assign it. An unowned obligation nobody looks at is the failure mode this whole "
        "program exists to end."
    )
    report.notes.insert(
        0, "Assign these. Anything left here is, by definition, owned by nobody."
    )
    return report
