"""Chaser: the cadence engine that keeps `waiting_external` from meaning forgotten.

Waiting on a vendor is not done. An obligation in `waiting_external` carries a clock, and
when that clock fires this agent advances it: another chase if the cadence has room, an
escalation up the path if it does not.

Phase 1 computes and records. It does not send. Every chase this agent schedules becomes a
line on the owner's daily digest saying "chase this today", and the human sends it. Phase 2
turns those into approve-and-send drafts, and only the four reversible categories in
`guardrails.outbound.autonomy_candidates` ever become autonomous, in Phase 4.

The cadence is driven backward from a real external deadline whenever one is known. A fixed
interval is the fallback, not the default: chasing every 72 hours when a solicitation closes
tomorrow is exactly the failure this system exists to prevent.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any

from ..clock import add_business_hours, backward_checkpoints, coverage_for
from ..config import TRIAGE_QUEUE, Config
from ..store import Store, parse_ts


@dataclass
class ChaseAction:
    obligation_id: int
    action: str            # "chase" | "escalate" | "exhausted"
    owner: str
    escalate_to: str | None
    chase_number: int
    next_chase_at: str | None
    due_at: str | None
    counterparty: str | None
    what_is_owed: str
    reason: str

    def as_row(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "obligation": self.obligation_id,
            "owner": self.owner,
            "escalate_to": self.escalate_to or "",
            "chase": self.chase_number,
            "counterparty": self.counterparty or "",
            "what_is_owed": self.what_is_owed,
            "next_chase": self.next_chase_at or "",
            "reason": self.reason,
        }


def due_chases(store: Store, now: dt.datetime) -> list[Any]:
    return store.query(
        "SELECT * FROM obligations WHERE status = 'waiting_external' "
        "AND next_chase_at IS NOT NULL AND next_chase_at <= ? ORDER BY next_chase_at",
        (now.isoformat(),),
    )


def plan(cfg: Config, store: Store, now: dt.datetime | None = None) -> list[ChaseAction]:
    """Every chase or escalation that is due right now. Persists nothing."""
    now = now or dt.datetime.now(dt.timezone.utc)
    actions: list[ChaseAction] = []

    for obligation in due_chases(store, now):
        sla = cfg.sla_for(obligation["type"], obligation["counterparty_class"])
        cadence = [float(h) for h in (sla.get("chase_cadence", []) or [])]
        limit = int(sla.get("escalate_after_chases", len(cadence) or 1))
        chase_number = int(obligation["chase_count"]) + 1

        deadline = _deadline_for(store, obligation)
        next_at = _next_chase(cfg, store, obligation, sla, cadence, chase_number, deadline, now)

        if chase_number > limit or next_at is None:
            escalate_to = _next_escalation(cfg, obligation)
            actions.append(ChaseAction(
                obligation_id=obligation["id"],
                action="escalate" if escalate_to else "exhausted",
                owner=obligation["owner"], escalate_to=escalate_to,
                chase_number=chase_number, next_chase_at=next_at,
                due_at=obligation["due_at"], counterparty=obligation["counterparty"],
                what_is_owed=obligation["what_is_owed"],
                reason=(
                    f"cadence exhausted after {int(obligation['chase_count'])} chase(s)"
                    if next_at is None or chase_number > limit
                    else "escalation threshold reached"
                ) + (
                    ""
                    if escalate_to
                    else "; escalation path is exhausted, this needs a decision not another chase"
                ),
            ))
            continue

        reason = f"chase {chase_number} of {limit}"
        if deadline:
            remaining = (parse_ts(deadline) - now).total_seconds() / 3600
            reason += f", {remaining:.0f}h until the external deadline"
        actions.append(ChaseAction(
            obligation_id=obligation["id"], action="chase", owner=obligation["owner"],
            escalate_to=None, chase_number=chase_number, next_chase_at=next_at,
            due_at=obligation["due_at"], counterparty=obligation["counterparty"],
            what_is_owed=obligation["what_is_owed"], reason=reason,
        ))

    return actions


def _deadline_for(store: Store, obligation: Any) -> str | None:
    """The real external date this obligation hangs off, if the thread carried one."""
    thread_key = (obligation["source_ref"] or "").split("#")[0]
    if not thread_key:
        return None
    rows = store.query(
        "SELECT solicitation_close_at FROM threads WHERE thread_key = ?", (thread_key,)
    )
    return rows[0]["solicitation_close_at"] if rows else None


def _next_chase(cfg: Config, store: Store, obligation: Any, sla: dict[str, Any],
                cadence: list[float], chase_number: int, deadline: str | None,
                now: dt.datetime) -> str | None:
    if deadline:
        checkpoints = backward_checkpoints(
            parse_ts(deadline), sla.get("backward_checkpoints", []) or [], now=now
        )
        return checkpoints[0].isoformat() if checkpoints else None
    if chase_number >= len(cadence):
        return None
    owner = obligation["owner"]
    coverage = coverage_for(cfg, owner if owner != TRIAGE_QUEUE else "doug")
    return add_business_hours(coverage, now, cadence[chase_number]).isoformat()


def _next_escalation(cfg: Config, obligation: Any) -> str | None:
    """The next person up the path. None means the path is spent and a human must decide."""
    try:
        path = json.loads(obligation["escalation_path"] or "[]")
    except json.JSONDecodeError:
        path = cfg.escalation_path(obligation["type"])
    level = int(obligation["escalation_level"]) + 1
    return path[level] if 0 <= level < len(path) else None


def apply(cfg: Config, store: Store, actions: list[ChaseAction],
          actor: str = "chaser") -> int:
    """Record the planned chases and escalations. Still sends nothing.

    Each recorded chase becomes a line on the owner's daily digest. In Phase 1 a human
    sends it; the ledger's job is to guarantee the line exists.
    """
    for action in actions:
        if action.action == "chase":
            store.db.execute(
                "UPDATE obligations SET chase_count = chase_count + 1, next_chase_at = ?, "
                "updated_at = ? WHERE id = ?",
                (action.next_chase_at, dt.datetime.now(dt.timezone.utc).isoformat(),
                 action.obligation_id),
            )
            store.audit(action.obligation_id, actor=actor, field="chase_count",
                        old_value=action.chase_number - 1, new_value=action.chase_number,
                        note=action.reason)
        else:
            store.db.execute(
                "UPDATE obligations SET escalation_level = escalation_level + 1, "
                "next_chase_at = NULL, updated_at = ? WHERE id = ?",
                (dt.datetime.now(dt.timezone.utc).isoformat(), action.obligation_id),
            )
            store.audit(action.obligation_id, actor=actor, field="escalation_level",
                        new_value=action.escalate_to or "path exhausted",
                        note=action.reason)
        store.log_action(
            actor=actor, action=action.action, entity="obligation",
            entity_id=str(action.obligation_id),
            detail={"chase_number": action.chase_number, "reason": action.reason,
                    "escalate_to": action.escalate_to},
        )
    store.db.commit()
    return len(actions)
