"""The compliance calendar.

Date driven, catastrophic if missed, trivially automatable. That combination makes this the
cheapest tail-risk reduction in the program, and unlike everything else here it depends on
nothing: not the mail path, not Zoho, not a single decision being made first.

The design rule that matters: **an item with no date is not healthy, it is unknown.** An
expired registration and an unrecorded one look identical from a config file, and only one
of them is survivable. So `unknown` outranks everything except `expired` in this report,
rather than being quietly filtered out as missing data.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from .config import TODO, Config
from .reports.render import Report

# Ordered worst first. `unknown` sits second because a date nobody recorded may already
# have passed, and reporting it as healthy is exactly how a lapse goes unnoticed.
STATUS_ORDER = {"expired": 0, "unknown": 1, "act now": 2, "approaching": 3, "ok": 4}


@dataclass
class Item:
    key: str
    name: str
    owner: str
    authority: str
    status: str
    expires_at: dt.datetime | None
    days_left: int | None
    lead_days: int
    consequence: str
    evidence: str | None
    last_verified: str | None
    cadence: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "status": self.status.upper(),
            "item": self.name,
            "owner": self.owner,
            "expires": self.expires_at.date().isoformat() if self.expires_at else "not recorded",
            "days left": "unknown" if self.days_left is None else self.days_left,
            "renew from": f"{self.lead_days}d out",
            "evidence": "on file" if self._has(self.evidence) else "missing",
            "last verified": self.last_verified if self._has(self.last_verified) else "never",
            "if it lapses": " ".join((self.consequence or "").split())[:180],
        }

    @staticmethod
    def _has(value: str | None) -> bool:
        return bool(value) and str(value).strip() != TODO


def _parse_date(value: Any) -> dt.datetime | None:
    if not value or str(value).strip() == TODO:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip())
    except ValueError:
        try:
            parsed = dt.datetime.combine(
                dt.date.fromisoformat(str(value).strip()[:10]), dt.time()
            )
        except ValueError:
            return None
    return parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc)


def load_items(cfg: Config, now: dt.datetime | None = None) -> list[Item]:
    now = now or dt.datetime.now(dt.timezone.utc)
    defaults = cfg.compliance.get("defaults", {}) or {}
    default_lead = int(defaults.get("renewal_lead_days", 60))

    items: list[Item] = []
    for key, spec in (cfg.compliance.get("items", {}) or {}).items():
        expires_at = _parse_date(spec.get("expires_at"))
        lead_days = int(spec.get("renewal_lead_days", default_lead) or default_lead)

        if expires_at is None:
            status, days_left = "unknown", None
        else:
            days_left = (expires_at - now).days
            if days_left < 0:
                status = "expired"
            elif days_left <= max(7, lead_days // 4):
                status = "act now"
            elif days_left <= lead_days:
                status = "approaching"
            else:
                status = "ok"

        items.append(Item(
            key=key,
            name=str(spec.get("name", key)),
            owner=str(spec.get("owner", defaults.get("owner", "triage_queue"))),
            authority=str(spec.get("authority", "")),
            status=status,
            expires_at=expires_at,
            days_left=days_left,
            lead_days=lead_days,
            consequence=str(spec.get("consequence", "")),
            evidence=spec.get("evidence"),
            last_verified=spec.get("last_verified"),
            cadence=str(spec.get("cadence", "")),
        ))

    items.sort(key=lambda item: (
        STATUS_ORDER.get(item.status, 9),
        item.days_left if item.days_left is not None else 0,
    ))
    return items


def run(cfg: Config, now: dt.datetime | None = None) -> Report:
    """The calendar as a report. Needs no mail, no store, and no decisions."""
    now = now or dt.datetime.now(dt.timezone.utc)
    items = load_items(cfg, now)

    report = Report(
        key="compliance",
        title="Compliance calendar",
        subtitle=(
            "Date driven, catastrophic if missed. An item with no recorded date is reported "
            "as unknown, not as healthy: an expired registration and an unrecorded one look "
            "identical from here."
        ),
        columns=["status", "item", "owner", "expires", "days left", "renew from",
                 "evidence", "last verified", "if it lapses"],
        rows=[item.as_row() for item in items],
        generated_at=now,
    )

    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    unowned = [item for item in items if not cfg.is_person(item.owner)]
    no_evidence = [item for item in items if not Item._has(item.evidence)]

    report.metrics = {
        "items tracked": len(items),
        "expired (target zero)": counts.get("expired", 0),
        "date not recorded (target zero)": counts.get("unknown", 0),
        "inside the renewal window": counts.get("act now", 0) + counts.get("approaching", 0),
        "with no named owner (target zero)": len(unowned),
        "with no evidence on file": len(no_evidence),
    }

    report.notes = [
        "UNKNOWN is the state to fix first. Every one of these may already have lapsed, and "
        "nothing in this system can tell you which.",
        "Renewal lead times are not padding. SAM renewal needs notarized documents and can "
        "take weeks, which is why it starts 90 days out rather than 30.",
        "These never auto-close. `sla.yaml` sets `never_auto_close` on the compliance type: "
        "a renewal is closed by a human attaching the new certificate, never by a date "
        "passing quietly.",
    ]
    if unowned:
        report.notes.append(
            "Items with no named owner in people.yaml: "
            + ", ".join(item.key for item in unowned)
        )
    if no_evidence:
        report.notes.append(
            "No evidence link on file for: " + ", ".join(item.key for item in no_evidence)
            + ". Guardrail: nothing closes without evidence, so these cannot be marked "
            "renewed even once they are."
        )
    return report


def as_obligations(cfg: Config, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Compliance items as ledger obligations, so they land on a person's digest.

    Only items inside their renewal window, expired, or with no recorded date. An item that
    is genuinely fine for another eight months does not belong on a daily digest, and
    putting it there is how a digest becomes wallpaper.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    obligations: list[dict[str, Any]] = []

    for item in load_items(cfg, now):
        if item.status == "ok":
            continue
        if item.status == "unknown":
            what = f"Find and record the expiration date for {item.name}"
        elif item.status == "expired":
            what = f"{item.name} EXPIRED {abs(item.days_left)} days ago, renew immediately"
        else:
            what = f"Renew {item.name}, expires in {item.days_left} days"

        obligations.append({
            "source": "manual",
            "source_ref": f"compliance:{item.key}",
            "counterparty": item.authority or "compliance",
            "counterparty_class": "service",
            "type": "compliance",
            "what_is_owed": what,
            "direction": "we_owe_them",
            "owner": item.owner if cfg.is_person(item.owner) else "triage_queue",
            "due_at": item.expires_at.isoformat() if item.expires_at else None,
            "due_basis": (
                f"{item.authority} expiration, renewal starts {item.lead_days} days out"
                if item.expires_at else "no expiration date recorded, which is the finding"
            ),
            # A date on a calendar is not a judgment call, so confidence is total. Human
            # review is still required for an unknown date, because somebody has to go
            # look it up rather than confirm a classification.
            "confidence": 1.0,
            "needs_human_review": 1 if item.status == "unknown" else 0,
        })
    return obligations


def sync(cfg: Config, store: Any, now: dt.datetime | None = None,
         persist: bool = False) -> list[dict[str, Any]]:
    """Create ledger obligations for compliance items that need attention.

    Deliberately one-directional. An item whose date moves back out of its renewal window
    does **not** close its obligation: guardrail 3 forbids auto-close, and a renewal is
    finished when a human attaches the new certificate, never when a date passes quietly.
    That asymmetry is the whole point of a compliance calendar.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    existing = {
        row["source_ref"] for row in store.query(
            "SELECT source_ref FROM obligations WHERE source_ref LIKE 'compliance:%' "
            "AND status NOT IN ('done','dropped')"
        )
    }

    created: list[dict[str, Any]] = []
    for fields in as_obligations(cfg, now):
        if fields["source_ref"] in existing:
            continue
        created.append(fields)
        if persist:
            obligation_id = store.create_obligation(cfg, **fields)
            store.log_action(
                actor="compliance", action="create", entity="obligation",
                entity_id=str(obligation_id),
                detail={"item": fields["source_ref"], "due_at": fields["due_at"]},
            )
    return created
