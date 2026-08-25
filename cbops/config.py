"""Config loading and validation.

Every rule that a human negotiates lives in config/*.yaml, not in code. This module loads
those files, resolves the cross references between them, and refuses to hand back a config
that violates a ledger invariant.

The invariant enforced here that matters most: an obligation owner is exactly one named
human who exists in people.yaml. A group owner means no owner.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TODO = "TODO_CONFIRM"

REPO_ROOT = Path(os.environ.get("CB_ROOT", Path(__file__).resolve().parent.parent))
CONFIG_DIR = REPO_ROOT / "config"

# The one owner value that is legitimately not a person.
TRIAGE_QUEUE = "triage_queue"


class ConfigError(Exception):
    """Raised when config is internally inconsistent. Never swallowed."""


@dataclass
class Problem:
    level: str  # "error" | "warn" | "todo"
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level.upper():5}] {self.where}: {self.message}"


@dataclass
class Config:
    people: dict[str, Any]
    routing: dict[str, Any]
    sla: dict[str, Any]
    guardrails: dict[str, Any]
    naming: dict[str, Any]
    counterparties: dict[str, Any]
    # Optional: the compliance calendar runs off config alone, with no mail and no store,
    # so a checkout without it still works rather than failing to load.
    compliance: dict[str, Any] = field(default_factory=dict)
    problems: list[Problem] = field(default_factory=list)

    # -- people ------------------------------------------------------------

    @property
    def person_ids(self) -> set[str]:
        return set(self.people.get("people", {}) or {})

    def person(self, pid: str) -> dict[str, Any]:
        try:
            return self.people["people"][pid]
        except KeyError as exc:
            raise ConfigError(f"unknown person: {pid!r}") from exc

    def is_person(self, pid: str) -> bool:
        return pid in self.person_ids

    def group(self, gid: str) -> list[str]:
        return list(self.people.get("groups", {}).get(gid, []))

    # -- routing -----------------------------------------------------------

    @property
    def routing_rules(self) -> list[dict[str, Any]]:
        return list(self.routing.get("rules", []) or [])

    def rule_for_type(self, obligation_type: str) -> dict[str, Any]:
        for rule in self.routing_rules:
            if rule.get("type") == obligation_type:
                return rule
        return dict(self.routing.get("unclassified", {}))

    def owner_for_type(self, obligation_type: str) -> str:
        """Resolve one named human. Never a group, never a list."""
        owner = self.rule_for_type(obligation_type).get("primary", TRIAGE_QUEUE)
        if isinstance(owner, (list, tuple, set)):
            raise ConfigError(
                f"routing.primary for {obligation_type!r} is a list; an obligation owner "
                "must be exactly one named human"
            )
        if owner != TRIAGE_QUEUE and not self.is_person(owner):
            raise ConfigError(f"routing.primary for {obligation_type!r} is not a person: {owner!r}")
        return owner

    def escalation_path(self, obligation_type: str) -> list[str]:
        """owner -> backup -> escalation -> exec, deduplicated, order preserved."""
        rule = self.rule_for_type(obligation_type)
        chain: list[str] = []
        for key in ("primary", "backup", "escalates_to"):
            value = rule.get(key)
            if value is None:
                continue
            for pid in value if isinstance(value, (list, tuple)) else [value]:
                if pid not in chain:
                    chain.append(pid)
        for pid in self.group("exec"):
            if pid not in chain:
                chain.append(pid)
        return chain

    # -- sla ---------------------------------------------------------------

    def sla_for(self, obligation_type: str, counterparty_class: str | None = None) -> dict[str, Any]:
        """Merge, in order: default profile, class profile, named type override."""
        profiles = self.sla.get("profiles", {}) or {}
        merged: dict[str, Any] = dict(profiles.get("default", {}))

        type_cfg = dict((self.sla.get("types", {}) or {}).get(obligation_type, {}))

        # An explicit profile on the type beats the counterparty class, because
        # contract_action is gov-paced even when the sender is a distributor.
        profile_name = type_cfg.pop("profile", None)
        if profile_name is None and counterparty_class:
            profile_name = (
                (self.counterparties.get("classes", {}) or {})
                .get(counterparty_class, {})
                .get("sla_profile")
            )
        if profile_name and profile_name in profiles:
            merged.update(profiles[profile_name])

        merged.update(type_cfg)
        return merged

    def phase0(self, key: str, default: Any = None) -> Any:
        return (self.sla.get("phase0", {}) or {}).get(key, default)

    # -- guardrails --------------------------------------------------------

    @property
    def phase(self) -> int:
        return int(self.guardrails.get("phase", 0))

    @property
    def outbound_enabled(self) -> bool:
        """Phase 0 answer is always False. Kill switch overrides everything."""
        if self.killswitch_engaged:
            return False
        return bool((self.guardrails.get("outbound", {}) or {}).get("enabled", False))

    @property
    def killswitch_path(self) -> Path:
        name = (self.guardrails.get("outbound", {}) or {}).get("kill_switch_file", ".killswitch")
        return REPO_ROOT / name

    @property
    def killswitch_engaged(self) -> bool:
        return self.killswitch_path.exists()

    def may_autosend(self, obligation_type: str, counterparty_class: str) -> tuple[bool, str]:
        """Deny by default. Returns (allowed, reason). Reason is always populated."""
        outbound = self.guardrails.get("outbound", {}) or {}
        if self.killswitch_engaged:
            return False, "kill switch engaged"
        if not outbound.get("enabled", False):
            return False, f"outbound disabled in phase {self.phase}"
        if counterparty_class in set(outbound.get("never_autosend_to_classes", []) or []):
            return False, f"counterparty class {counterparty_class!r} is never auto-sent"
        if obligation_type in set(outbound.get("never_autosend_types", []) or []):
            return False, f"obligation type {obligation_type!r} is never auto-sent"
        if obligation_type not in set(outbound.get("autonomy_candidates", []) or []):
            return False, f"obligation type {obligation_type!r} is not an autonomy candidate"
        return True, "allowed by guardrails"

    def needs_human_review(self, obligation_type: str, confidence: float) -> bool:
        conf = self.guardrails.get("confidence", {}) or {}
        if obligation_type in set(conf.get("always_human_review", []) or []):
            return True
        return confidence < float(conf.get("min_to_record_without_review", 0.7))

    # -- counterparties ----------------------------------------------------

    @property
    def our_domains(self) -> set[str]:
        return {d.lower() for d in (self.counterparties.get("our_domains", []) or [])}

    def importance(self, counterparty_class: str) -> int:
        classes = self.counterparties.get("classes", {}) or {}
        return int(classes.get(counterparty_class, {}).get("importance", 50))


def _load_yaml_optional(path: Path) -> dict[str, Any]:
    return _load_yaml(path) if path.exists() else {}


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    with path.open() as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return data


def _find_todos(node: Any, where: str, out: list[Problem]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _find_todos(value, f"{where}.{key}", out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _find_todos(value, f"{where}[{index}]", out)
    elif isinstance(node, str) and node.strip() == TODO:
        out.append(Problem("todo", where, "placeholder must be confirmed by a human"))


_COVERAGE_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


def validate(cfg: Config) -> list[Problem]:
    """Everything wrong with the config, in one pass. Errors block, warns do not."""
    problems: list[Problem] = []
    people = cfg.people.get("people", {}) or {}

    if not people:
        problems.append(Problem("error", "people.yaml", "no people defined"))

    for pid, person in people.items():
        if not person.get("name"):
            problems.append(Problem("error", f"people.{pid}", "missing name"))
        coverage = str(person.get("coverage_hours", ""))
        if coverage != TODO and not _COVERAGE_RE.match(coverage):
            problems.append(
                Problem("error", f"people.{pid}.coverage_hours",
                        f"expected HH:MM-HH:MM, got {coverage!r}")
            )

    for gid, members in (cfg.people.get("groups", {}) or {}).items():
        for pid in members:
            if pid not in people:
                problems.append(Problem("error", f"people.groups.{gid}", f"unknown person {pid!r}"))

    # Routing: one named owner, and an escalation that actually escalates.
    seen_types: set[str] = set()
    for rule in cfg.routing_rules:
        rtype = rule.get("type")
        where = f"routing.{rtype}"
        if not rtype:
            problems.append(Problem("error", "routing", f"rule without a type: {rule!r}"))
            continue
        if rtype in seen_types:
            problems.append(Problem("error", where, "duplicate rule type"))
        seen_types.add(rtype)

        primary = rule.get("primary")
        if isinstance(primary, (list, tuple)):
            problems.append(
                Problem("error", f"{where}.primary",
                        "owner must be exactly one named human, never a list")
            )
        elif primary != TRIAGE_QUEUE and primary not in people:
            problems.append(Problem("error", f"{where}.primary", f"unknown person {primary!r}"))

        backup = rule.get("backup")
        if backup and backup not in people and backup != TRIAGE_QUEUE:
            problems.append(Problem("error", f"{where}.backup", f"unknown person {backup!r}"))
        if backup and backup == primary:
            problems.append(Problem("error", f"{where}.backup", "backup equals primary"))

        escalates = rule.get("escalates_to")
        esc_list = escalates if isinstance(escalates, (list, tuple)) else [escalates] if escalates else []
        for pid in esc_list:
            if pid not in people:
                problems.append(Problem("error", f"{where}.escalates_to", f"unknown person {pid!r}"))
        # An escalation path that terminates at the backup is not an escalation.
        if esc_list and backup and set(esc_list) == {backup}:
            problems.append(
                Problem("warn", f"{where}.escalates_to",
                        f"escalation equals backup ({backup!r}); a breach would go nowhere new")
            )
        if esc_list and primary in esc_list:
            problems.append(
                Problem("warn", f"{where}.escalates_to",
                        "escalation includes the primary owner")
            )

        # Anything that must page immediately cannot rest on an offset-hours owner alone.
        if rule.get("page_immediately"):
            also = set(rule.get("also_notify", []) or [])
            if primary in people and people[primary].get("offset_hours") and not also:
                problems.append(
                    Problem("error", where,
                            f"page_immediately with offset-hours primary {primary!r} and no "
                            "also_notify list; the page would wait for a coverage window")
                )

    # SLA: every routed type should have a clock, and deadline-driven needs its field.
    sla_types = cfg.sla.get("types", {}) or {}
    for rtype in seen_types:
        if rtype not in sla_types:
            problems.append(
                Problem("warn", f"sla.types.{rtype}", "no clock defined, falling back to default")
            )
    for tname, tcfg in sla_types.items():
        if tcfg.get("deadline_driven") and not tcfg.get("deadline_field"):
            problems.append(
                Problem("error", f"sla.types.{tname}", "deadline_driven without deadline_field")
            )

    # Guardrails: phase 0 must not have outbound on, and approvers must be real.
    outbound = cfg.guardrails.get("outbound", {}) or {}
    if cfg.phase == 0 and outbound.get("enabled"):
        problems.append(
            Problem("error", "guardrails.outbound.enabled",
                    "phase 0 is read-only; outbound must be false")
        )
    overlap = set(outbound.get("never_autosend_types", []) or []) & set(
        outbound.get("autonomy_candidates", []) or []
    )
    if overlap:
        problems.append(
            Problem("error", "guardrails.outbound",
                    f"types are both never-autosend and autonomy candidates: {sorted(overlap)}")
        )
    for pid in (cfg.guardrails.get("commitments", {}) or {}).get("approvers", []) or []:
        if pid not in people:
            problems.append(Problem("error", "guardrails.commitments.approvers", f"unknown person {pid!r}"))

    # Naming regex must compile, or evidence lookup silently finds nothing.
    try:
        re.compile(cfg.naming.get("filename_regex", ""))
    except re.error as exc:
        problems.append(Problem("error", "naming.filename_regex", f"does not compile: {exc}"))
    for name, pattern in (cfg.naming.get("reference_patterns", {}) or {}).items():
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(Problem("error", f"naming.reference_patterns.{name}", f"does not compile: {exc}"))

    for key, item in (cfg.compliance.get("items", {}) or {}).items():
        where = f"compliance.{key}"
        if not item.get("name"):
            problems.append(Problem("error", where, "missing name"))
        owner = item.get("owner") or (cfg.compliance.get("defaults", {}) or {}).get("owner")
        if owner and owner not in people:
            problems.append(Problem("error", f"{where}.owner", f"unknown person {owner!r}"))
        elif not owner:
            problems.append(Problem("error", where, "no owner; a compliance item with no "
                                                    "named human is how a lapse happens"))
        if not item.get("consequence"):
            problems.append(Problem(
                "warn", where,
                "no consequence written. A row nobody understands the stakes of is a row "
                "nobody renews",
            ))
        lead = item.get("renewal_lead_days")
        if lead is not None and (not isinstance(lead, int) or lead < 0):
            problems.append(Problem("error", f"{where}.renewal_lead_days",
                                    f"expected a non-negative integer, got {lead!r}"))

    if not cfg.our_domains:
        problems.append(
            Problem("error", "counterparties.our_domains",
                    "empty; without this every message looks inbound")
        )

    for source, where in (
        (cfg.people, "people.yaml"),
        (cfg.routing, "routing.yaml"),
        (cfg.sla, "sla.yaml"),
        (cfg.guardrails, "guardrails.yaml"),
        (cfg.counterparties, "counterparties.yaml"),
        (cfg.compliance, "compliance.yaml"),
    ):
        _find_todos(source, where, problems)

    return problems


def load(config_dir: Path | None = None) -> Config:
    """Load and validate. Raises ConfigError on any error-level problem."""
    cfg = load_unvalidated(config_dir)
    cfg.problems = validate(cfg)
    errors = [p for p in cfg.problems if p.level == "error"]
    if errors:
        raise ConfigError(
            "config has "
            + str(len(errors))
            + " error(s):\n"
            + "\n".join(f"  {p}" for p in errors)
        )
    return cfg


def load_unvalidated(config_dir: Path | None = None) -> Config:
    """Load without raising, so `./cb doctor` can report every problem at once."""
    directory = config_dir or CONFIG_DIR
    cfg = Config(
        people=_load_yaml(directory / "people.yaml"),
        routing=_load_yaml(directory / "routing.yaml"),
        sla=_load_yaml(directory / "sla.yaml"),
        guardrails=_load_yaml(directory / "guardrails.yaml"),
        naming=_load_yaml(directory / "naming.yaml"),
        counterparties=_load_yaml(directory / "counterparties.yaml"),
        compliance=_load_yaml_optional(directory / "compliance.yaml"),
    )
    cfg.problems = validate(cfg)
    return cfg
