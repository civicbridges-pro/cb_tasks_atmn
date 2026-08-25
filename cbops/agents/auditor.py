"""Auditor: nightly consistency checks over the ledger.

Every check here corresponds to an invariant the ledger claims to hold. The store enforces
those invariants on the paths it controls, but a Zoho sync, a manual edit, or a config
change can still produce a state the store would have refused. The auditor is how that gets
noticed the same night rather than in a quarterly surprise.

A finding is never fixed automatically. Guardrail 3: no auto-archive, auto-delete, or
auto-close. The auditor reports, a human decides.

**Severity means one specific thing here.** `critical` is reserved for integrity: an
invariant is broken, or capture has stopped, and the ledger cannot currently be trusted.
Work being late is `high`, however late it is. That distinction is what makes the exit code
useful: `./cb audit` failing means "do not trust these numbers", not "somebody is behind".
Overdue work belongs on the owner's digest and the exec rollup, where it can be acted on,
and mixing it into the same alert would train everyone to ignore both.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, Callable

from ..config import TRIAGE_QUEUE, Config
from ..store import GROUP_OWNERS, Store, parse_ts
from ..reports.render import Report, freshness_lines

Check = Callable[[Config, Store, dt.datetime], list[dict[str, Any]]]


@dataclass
class Finding:
    severity: str        # "critical" | "high" | "medium"
    check: str
    entity: str
    detail: str

    def as_row(self) -> dict[str, Any]:
        return {"severity": self.severity, "check": self.check,
                "entity": self.entity, "detail": self.detail}


def _finding(severity: str, check: str, entity: Any, detail: str) -> dict[str, Any]:
    return Finding(severity, check, str(entity), detail).as_row()


def closed_without_evidence(cfg: Config, store: Store, now: dt.datetime) -> list[dict[str, Any]]:
    """Invariant 3. A close with no evidence row is a close nobody can defend."""
    rows = store.query(
        "SELECT o.id, o.type, o.what_is_owed FROM obligations o "
        "LEFT JOIN evidence e ON e.obligation_id = o.id "
        "WHERE o.status = 'done' AND e.id IS NULL"
    )
    return [
        _finding("critical", "closed_without_evidence", f"obligation {row['id']}",
                 f"{row['type']}: closed with no evidence link. {row['what_is_owed'][:80]}")
        for row in rows
    ]


def waiting_without_clock(cfg: Config, store: Store, now: dt.datetime) -> list[dict[str, Any]]:
    """Invariant 2. Waiting on a vendor with no clock is indistinguishable from forgotten."""
    rows = store.query(
        "SELECT id, type, counterparty FROM obligations WHERE status = 'waiting_external' "
        "AND (waiting_since IS NULL OR (next_chase_at IS NULL AND escalation_level = 0))"
    )
    return [
        _finding("high", "waiting_without_clock", f"obligation {row['id']}",
                 f"{row['type']} waiting on {row['counterparty'] or 'unknown'} with no chase "
                 "scheduled and no escalation recorded")
        for row in rows
    ]


def owner_not_a_named_human(cfg: Config, store: Store, now: dt.datetime) -> list[dict[str, Any]]:
    """Invariant 1. Catches config drift: a person removed from people.yaml."""
    findings: list[dict[str, Any]] = []
    for row in store.query(
        "SELECT id, owner, type FROM obligations WHERE status NOT IN ('done','dropped')"
    ):
        owner = row["owner"]
        if owner == TRIAGE_QUEUE:
            continue
        if str(owner).strip().lower() in GROUP_OWNERS:
            findings.append(_finding(
                "critical", "group_owner", f"obligation {row['id']}",
                f"owner {owner!r} is a group; a group owner means no owner"))
        elif not cfg.is_person(owner):
            findings.append(_finding(
                "critical", "unknown_owner", f"obligation {row['id']}",
                f"owner {owner!r} is not in people.yaml; the obligation is orphaned"))
    return findings


def past_sla_response(cfg: Config, store: Store, now: dt.datetime) -> list[dict[str, Any]]:
    """Response clock breached. Reported by owner, because that is who acts on it."""
    rows = store.query(
        "SELECT id, owner, type, sla_response_at, counterparty FROM obligations "
        "WHERE status = 'open' AND sla_response_at IS NOT NULL AND sla_response_at <= ? "
        "ORDER BY sla_response_at",
        (now.isoformat(),),
    )
    findings = []
    for row in rows:
        overdue = (now - parse_ts(row["sla_response_at"])).total_seconds() / 3600
        findings.append(_finding(
            "high" if overdue > 24 else "medium", "past_sla_response",
            f"obligation {row['id']}",
            f"{row['owner']} is {overdue:.0f}h past the response clock on {row['type']} "
            f"for {row['counterparty'] or 'unknown'}"))
    return findings


def past_due(cfg: Config, store: Store, now: dt.datetime) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT id, owner, type, due_at, what_is_owed FROM obligations "
        "WHERE status IN ('open','waiting_external','blocked') AND due_at IS NOT NULL "
        "AND due_at <= ? ORDER BY due_at",
        (now.isoformat(),),
    )
    findings = []
    for row in rows:
        overdue = (now - parse_ts(row["due_at"])).total_seconds() / 3600
        # Late work is never `critical`, however late. Critical is reserved for integrity,
        # so that a failing audit means the ledger cannot be trusted rather than that
        # somebody is behind. Lateness is what the digests are for.
        findings.append(_finding(
            "high", "past_due", f"obligation {row['id']}",
            f"{overdue:.0f}h past due, owner {row['owner']}: {row['what_is_owed'][:70]}"))
    return findings


def deadline_driven_without_deadline(cfg: Config, store: Store,
                                     now: dt.datetime) -> list[dict[str, Any]]:
    """A deadline-driven type with no date cannot be chased on a plan, only on a guess."""
    findings = []
    for row in store.query(
        "SELECT id, type, counterparty, source_ref FROM obligations "
        "WHERE status NOT IN ('done','dropped') AND due_at IS NULL"
    ):
        sla = cfg.sla_for(row["type"], None)
        if not sla.get("deadline_driven"):
            continue
        findings.append(_finding(
            "high", "no_external_deadline", f"obligation {row['id']}",
            f"{row['type']} for {row['counterparty'] or 'unknown'} is deadline driven on "
            f"{sla.get('deadline_field')} but no date was captured; the chase cadence "
            "falls back to a fixed interval"))
    return findings


def unreviewed_low_confidence(cfg: Config, store: Store,
                              now: dt.datetime) -> list[dict[str, Any]]:
    """Guardrail 8, checked after the fact as well as before."""
    threshold = float(
        (cfg.guardrails.get("confidence", {}) or {}).get("min_to_record_without_review", 0.70)
    )
    rows = store.query(
        "SELECT id, type, confidence FROM obligations WHERE needs_human_review = 0 "
        "AND confidence IS NOT NULL AND confidence < ? AND status NOT IN ('done','dropped')",
        (threshold,),
    )
    return [
        _finding("high", "unreviewed_low_confidence", f"obligation {row['id']}",
                 f"{row['type']} recorded at confidence {row['confidence']:.2f} without a "
                 f"review flag; threshold is {threshold:.2f}")
        for row in rows
    ]


def high_consequence_unreviewed(cfg: Config, store: Store,
                                now: dt.datetime) -> list[dict[str, Any]]:
    """stop_work, contract_action, and award always reach a human. No exceptions."""
    always = (cfg.guardrails.get("confidence", {}) or {}).get("always_human_review", []) or []
    if not always:
        return []
    placeholders = ", ".join("?" for _ in always)
    rows = store.query(
        f"SELECT id, type FROM obligations WHERE type IN ({placeholders}) "
        "AND needs_human_review = 0 AND status NOT IN ('done','dropped')",
        list(always),
    )
    return [
        _finding("critical", "high_consequence_unreviewed", f"obligation {row['id']}",
                 f"{row['type']} is not flagged for human review; guardrail says it always is")
        for row in rows
    ]


def duplicate_obligations(cfg: Config, store: Store, now: dt.datetime) -> list[dict[str, Any]]:
    """Two open obligations of the same type on one thread means one is noise."""
    rows = store.query(
        "SELECT type, COUNT(*) AS n, GROUP_CONCAT(id) AS ids, source_ref FROM obligations "
        "WHERE status NOT IN ('done','dropped') AND source_ref IS NOT NULL "
        "GROUP BY type, SUBSTR(source_ref, 1, INSTR(source_ref || '#', '#') - 1) HAVING n > 1"
    )
    return [
        _finding("medium", "duplicate_obligations", f"obligations {row['ids']}",
                 f"{row['n']} open {row['type']} obligations on the same thread")
        for row in rows
    ]


def stale_capture(cfg: Config, store: Store, now: dt.datetime) -> list[dict[str, Any]]:
    """Failure mode number one. A stalled sync makes every other number a lie."""
    limit = int((cfg.guardrails.get("health", {}) or {}).get("mailbox_stale_after_minutes", 60))
    findings = []
    rows = store.last_sync()
    if not rows:
        return [_finding("critical", "no_capture", "capture",
                         "no sync has ever run; the ledger is empty for that reason, "
                         "not because nothing is owed")]
    for row in rows:
        last_success = parse_ts(row["last_success"])
        if last_success is None:
            findings.append(_finding("critical", "stale_capture", row["target"],
                                     "never synced successfully"))
            continue
        age = (now - last_success).total_seconds() / 60
        if age > limit:
            findings.append(_finding(
                "critical", "stale_capture", row["target"],
                f"{int(age)} minutes since the last successful sync, limit {limit}"))
    return findings


def unclaimed_mailboxes(cfg: Config, store: Store, now: dt.datetime) -> list[dict[str, Any]]:
    """A mailbox nobody is recorded as reading breaks the single-owner rule at the source."""
    return [
        _finding("medium", "unclaimed_mailbox", row["address"],
                 f"{row['kind']} mailbox with no recorded reader; obligations arriving here "
                 "cannot be attributed to a named human")
        for row in store.unclaimed_mailboxes()
    ]


def config_drift(cfg: Config, store: Store, now: dt.datetime) -> list[dict[str, Any]]:
    """Config problems reported alongside ledger problems, because both break the same night."""
    findings = []
    for problem in cfg.problems:
        if problem.level == "error":
            findings.append(_finding("critical", "config_error", problem.where, problem.message))
        elif problem.level == "warn":
            findings.append(_finding("medium", "config_warning", problem.where, problem.message))
    return findings


def broken_escalation_paths(cfg: Config, store: Store, now: dt.datetime) -> list[dict[str, Any]]:
    """An escalation that never reaches anyone new is not an escalation."""
    findings = []
    for row in store.query(
        "SELECT id, type, escalation_path, escalation_level FROM obligations "
        "WHERE status NOT IN ('done','dropped')"
    ):
        try:
            path = json.loads(row["escalation_path"] or "[]")
        except json.JSONDecodeError:
            findings.append(_finding("high", "unreadable_escalation_path",
                                     f"obligation {row['id']}", "escalation_path is not JSON"))
            continue
        if len(path) < 2:
            findings.append(_finding(
                "high", "escalation_path_too_short", f"obligation {row['id']}",
                f"{row['type']} has an escalation path of {len(path)}; a breach has nowhere to go"))
        elif int(row["escalation_level"]) >= len(path) - 1:
            findings.append(_finding(
                "high", "escalation_exhausted", f"obligation {row['id']}",
                f"{row['type']} has run out of escalation path; this needs a decision, "
                "not another chase"))
    return findings


CHECKS: list[tuple[str, Check]] = [
    ("closed_without_evidence", closed_without_evidence),
    ("high_consequence_unreviewed", high_consequence_unreviewed),
    ("owner_not_a_named_human", owner_not_a_named_human),
    ("stale_capture", stale_capture),
    ("config_drift", config_drift),
    ("past_due", past_due),
    ("past_sla_response", past_sla_response),
    ("waiting_without_clock", waiting_without_clock),
    ("unreviewed_low_confidence", unreviewed_low_confidence),
    ("deadline_driven_without_deadline", deadline_driven_without_deadline),
    ("broken_escalation_paths", broken_escalation_paths),
    ("duplicate_obligations", duplicate_obligations),
    ("unclaimed_mailboxes", unclaimed_mailboxes),
]

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2}


def run(cfg: Config, store: Store, now: dt.datetime | None = None) -> Report:
    """Every check, worst first. Nothing is repaired: the auditor reports, humans decide."""
    now = now or dt.datetime.now(dt.timezone.utc)
    lines, stale = freshness_lines(cfg, store, now)

    report = Report(
        key="audit",
        title="Ledger Audit",
        subtitle=(
            "Nightly consistency checks over the ledger and its config. Nothing here is "
            "repaired automatically: guardrail 3 says no auto-close, so the auditor reports "
            "and a human decides."
        ),
        columns=["severity", "check", "entity", "detail"],
        freshness=lines, stale=stale, generated_at=now,
    )

    counts: dict[str, int] = {}
    for name, check in CHECKS:
        try:
            found = check(cfg, store, now)
        except Exception as exc:
            # A check that raises must not silence the other twelve.
            found = [_finding("critical", "check_failed", name,
                              f"{type(exc).__name__}: {exc}")]
        report.rows.extend(found)
        if found:
            counts[name] = len(found)

    report.rows.sort(key=lambda row: (SEVERITY_ORDER.get(str(row["severity"]), 3),
                                      str(row["check"])))

    by_severity = {level: 0 for level in SEVERITY_ORDER}
    for row in report.rows:
        by_severity[str(row["severity"])] = by_severity.get(str(row["severity"]), 0) + 1

    report.metrics = {
        "findings": len(report.rows),
        "critical": by_severity.get("critical", 0),
        "high": by_severity.get("high", 0),
        "medium": by_severity.get("medium", 0),
        "checks that fired": ", ".join(sorted(counts)) or "none",
    }
    report.notes = [
        "A critical finding means an invariant the ledger claims to hold is not holding, or "
        "capture has stopped. Either way the numbers cannot be trusted right now. The store "
        "refuses these on the paths it controls, so a critical here points at a manual edit, "
        "a sync, or a config change.",
        "Overdue work is `high`, never `critical`, however overdue. It belongs on the owner's "
        "digest where somebody can act on it, not in an integrity alert.",
        "`stale_capture` outranks everything else in practice: while capture is stale, every "
        "other count on this page is a floor rather than a total.",
        "`escalation_exhausted` is not a bug. It means chasing has run out and somebody has "
        "to make a decision, which is exactly the moment this system exists to surface.",
    ]
    return report
