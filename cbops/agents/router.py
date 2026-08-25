"""Router: one named human, one clock, one escalation path.

Two rules decide everything here.

*   **A low-confidence candidate goes to the triage queue, not to a person.** Dropping an
    unclear obligation on someone's digest teaches them the digest is noise, and a queue of
    noise gets ignitored the same way an inbox does.
*   **`they_owe_us` opens as `waiting_external` with a clock running.** Waiting on a vendor
    is not done. The clock is what turns waiting into a chase instead of into silence.

Phase 1 sends nothing. A `page_immediately` type still produces a page record, because the
page has to be visible somewhere from the day the ledger goes live, and in Phase 1 that
somewhere is the top of the exec rollup.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from ..clock import add_business_hours, backward_checkpoints, coverage_for
from ..config import TRIAGE_QUEUE, Config
from ..store import Store, parse_ts
from .triage import Candidate


@dataclass
class Routed:
    """A candidate with an owner, clocks, and an escalation path attached."""

    candidate: Candidate
    owner: str
    status: str
    sla_response_at: str | None
    next_chase_at: str | None
    waiting_since: str | None
    escalation_path: list[str]
    coverage_known: bool
    page_now: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    obligation_id: int | None = None

    def as_row(self) -> dict[str, Any]:
        """Flat shape for preview tables and digests."""
        return {
            "type": self.candidate.type,
            "owner": self.owner,
            "status": self.status,
            "direction": self.candidate.direction,
            "counterparty": self.candidate.counterparty or "",
            "what_is_owed": self.candidate.what_is_owed,
            "due_at": self.candidate.due_at or "",
            "respond_by": self.sla_response_at or "",
            "next_chase": self.next_chase_at or "",
            "confidence": self.candidate.confidence,
            "review": "yes" if self.candidate.needs_human_review else "",
            "page": ", ".join(self.page_now),
            "thread": self.candidate.thread_key,
        }


def route(cfg: Config, store: Store, candidate: Candidate,
          now: dt.datetime | None = None) -> Routed:
    """Assign owner, clocks, and escalation. Persists nothing."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rule = cfg.rule_for_type(candidate.type)
    notes: list[str] = []

    lane_owner = cfg.owner_for_type(candidate.type)
    threshold = float(
        (cfg.guardrails.get("confidence", {}) or {}).get("min_to_record_without_review", 0.70)
    )
    always_human = set(
        (cfg.guardrails.get("confidence", {}) or {}).get("always_human_review", []) or []
    )

    if candidate.confidence < threshold and candidate.type not in always_human:
        # Guardrail 8. Never guess an owner: an unclear obligation on the wrong person's
        # digest is how a team learns to stop reading the digest.
        owner = TRIAGE_QUEUE
        notes.append(
            f"confidence {candidate.confidence:.2f} is below {threshold:.2f}, so this goes "
            f"to the triage queue rather than to {lane_owner}"
        )
    else:
        owner = lane_owner
        if candidate.type in always_human:
            notes.append(
                f"{candidate.type} always needs a human, whatever the confidence"
            )

    # A page fans out immediately and ignores coverage windows. See open question 7: the
    # primary owner for stop-work works offset hours, so the page cannot wait for them.
    page_now: list[str] = []
    if rule.get("page_immediately"):
        page_now = [owner] if owner != TRIAGE_QUEUE else []
        page_now += [p for p in (rule.get("also_notify") or []) if p not in page_now]
        notes.append("pages immediately; does not enter a queue")

    sla = cfg.sla_for(candidate.type, candidate.counterparty_class)
    coverage = coverage_for(cfg, owner) if owner != TRIAGE_QUEUE else coverage_for(cfg, "doug")
    opened_at = _opened_at(store, candidate, now)

    response_hours = sla.get("response_hours")
    if rule.get("ignore_coverage_hours") or sla.get("ignore_coverage_hours"):
        sla_response_at = (
            (opened_at + dt.timedelta(hours=float(response_hours))).isoformat()
            if response_hours is not None else None
        )
        notes.append("response clock runs on wall time, not coverage hours")
    elif response_hours is not None:
        sla_response_at = add_business_hours(coverage, opened_at, float(response_hours)).isoformat()
    else:
        sla_response_at = None

    if not coverage.known and owner != TRIAGE_QUEUE:
        notes.append(
            f"{owner}'s coverage hours are unconfirmed, so this clock is wall time and "
            "should not be quoted as an SLA breach"
        )

    if candidate.direction == "they_owe_us":
        status = "waiting_external"
        waiting_since = opened_at.isoformat()
        next_chase_at = _first_chase(cfg, store, candidate, coverage, opened_at, now, sla, notes)
    else:
        status = "open"
        waiting_since = None
        next_chase_at = None

    return Routed(
        candidate=candidate, owner=owner, status=status,
        sla_response_at=sla_response_at, next_chase_at=next_chase_at,
        waiting_since=waiting_since,
        escalation_path=cfg.escalation_path(candidate.type),
        coverage_known=coverage.known, page_now=page_now, notes=notes,
    )


def _opened_at(store: Store, candidate: Candidate, now: dt.datetime) -> dt.datetime:
    row = store.query(
        "SELECT sent_at FROM messages WHERE id = ?", (candidate.message_id,)
    )
    return (parse_ts(row[0]["sent_at"]) if row else None) or now


def _first_chase(cfg: Config, store: Store, candidate: Candidate, coverage: Any,
                 opened_at: dt.datetime, now: dt.datetime, sla: dict[str, Any],
                 notes: list[str]) -> str | None:
    """Backward from a real deadline when we have one, otherwise the fixed cadence.

    This is the Marotta fix. Chasing on a fixed interval when a solicitation closes
    tomorrow is theater, so a known close date always wins over the cadence.
    """
    deadline = candidate.external_deadline or _thread_deadline(store, candidate.thread_key)
    if deadline:
        close_at = parse_ts(deadline)
        checkpoints = backward_checkpoints(
            close_at, sla.get("backward_checkpoints", []) or [], now=now
        )
        if checkpoints:
            notes.append(
                f"chase planned backward from {close_at.date().isoformat()}, "
                f"{len(checkpoints)} checkpoint(s) remaining"
            )
            return checkpoints[0].isoformat()
        notes.append(
            f"external deadline {close_at.date().isoformat()} leaves no room to chase; "
            "escalate to a named human at the counterparty instead"
        )
        return None

    cadence = sla.get("chase_cadence", []) or []
    if not cadence:
        notes.append("no chase cadence configured for this type")
        return None
    notes.append(
        "no external deadline found, so the chase falls back to the fixed cadence; "
        "capturing the close date at intake fixes this"
    )
    return add_business_hours(coverage, opened_at, float(cadence[0])).isoformat()


def _thread_deadline(store: Store, thread_key: str) -> str | None:
    rows = store.query(
        "SELECT solicitation_close_at FROM threads WHERE thread_key = ?", (thread_key,)
    )
    return rows[0]["solicitation_close_at"] if rows else None


def commit(cfg: Config, store: Store, routed: Routed, actor: str = "router") -> int:
    """Persist a routed candidate as an obligation.

    The store enforces the single-human-owner rule, so a routing bug fails here loudly
    rather than producing an obligation nobody owns.
    """
    fields = routed.candidate.as_obligation_fields()
    fields.update({
        "owner": routed.owner,
        "status": routed.status,
        "sla_response_at": routed.sla_response_at,
        "waiting_since": routed.waiting_since,
        "next_chase_at": routed.next_chase_at,
        "escalation_path": json.dumps(routed.escalation_path),
    })
    obligation_id = store.create_obligation(cfg, **fields)
    routed.obligation_id = obligation_id

    # Evidence, from the first moment. The message that created the obligation is the
    # first link in the chain that will later be required to close it.
    store.add_evidence(
        obligation_id, "email" if routed.candidate.source == "email" else "note",
        routed.candidate.source_ref, actor, label="originating message",
    )
    store.audit(
        obligation_id, actor=actor, field="routed", new_value=routed.owner,
        note="; ".join(routed.notes) or routed.candidate.reason,
    )
    store.log_action(
        actor=actor, action="route", entity="obligation", entity_id=str(obligation_id),
        detail={
            "type": routed.candidate.type, "owner": routed.owner,
            "confidence": routed.candidate.confidence,
            "needs_human_review": routed.candidate.needs_human_review,
            "page_now": routed.page_now, "notes": routed.notes,
        },
    )
    return obligation_id


def build(cfg: Config, store: Store, candidates: list[Candidate],
          now: dt.datetime | None = None, persist: bool = False) -> list[Routed]:
    """Route every candidate. `persist=False` is a preview and writes nothing."""
    routed = [route(cfg, store, candidate, now) for candidate in candidates]
    if persist:
        for item in routed:
            commit(cfg, store, item)
    return routed
