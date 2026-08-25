"""Triage: a message becomes an obligation, or it becomes a question for a human.

The rule that governs this file is guardrail 8. An agent that quietly mislabels a stop-work
order as routine is worse than no agent, so nothing here guesses. Every candidate carries a
confidence, anything under the threshold is routed to the triage queue rather than to a
person, and stop-work, contract actions, and awards go to a human at any confidence.

Classification is deterministic first: the `match` terms in config/routing.yaml against the
subject and body. The model layer, when enabled, grades that candidate and can raise its
confidence, correct its type, or reject it outright. Rules alone can never clear the action
threshold, so a keyword match by itself never produces an unreviewed obligation.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from ..clock import add_business_hours, coverage_for
from ..config import TRIAGE_QUEUE, Config
from ..extract import rules
from ..store import Store, parse_ts

# A keyword match is evidence, not proof. This ceiling sits below
# guardrails.confidence.min_to_act so a term list can never authorize an action.
RULES_CEILING = 0.80

@dataclass
class Candidate:
    """One proposed obligation. Not persisted until the router accepts it."""

    message_id: int
    thread_key: str
    type: str
    what_is_owed: str
    direction: str
    counterparty: str | None
    counterparty_class: str
    contract_ref: str | None
    source: str
    source_ref: str
    due_at: str | None = None
    due_basis: str | None = None
    external_deadline: str | None = None
    confidence: float = 0.0
    needs_human_review: bool = False
    matched_terms: list[str] = field(default_factory=list)
    reason: str = ""
    extractor: str = "rules"

    def as_obligation_fields(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_ref": self.source_ref,
            "counterparty": self.counterparty,
            "counterparty_class": self.counterparty_class,
            "contract_ref": self.contract_ref,
            "type": self.type,
            "what_is_owed": self.what_is_owed,
            "direction": self.direction,
            "due_at": self.due_at,
            "due_basis": self.due_basis,
            "confidence": self.confidence,
            "needs_human_review": 1 if self.needs_human_review else 0,
        }


# A matched term this long or longer is substantive: it names the subject rather than
# happening to appear in it. Two lanes with a substantive match is genuine ambiguity, and
# that is the case a human should see.
SUBSTANTIVE_TERM_LEN = 7

# Baseline for a single matched term, before clarity and preference adjustments. Calibrated
# so one unmistakable match lands just above guardrails.confidence.min_to_record_without_
# review (0.70) and an ambiguous two-lane match lands just below it.
BASE_CONFIDENCE = 0.50


def _effective_length(term: str) -> int:
    """How much a term is worth as evidence.

    Short identifiers carry more weight than their length suggests. "po#" and "dd250" are
    not English words that happen to appear in a sentence, they are the subject, so they
    count as substantive despite being short. A short *word* like "mod" does not.
    """
    if not term.replace(" ", "").isalpha():
        return max(len(term), SUBSTANTIVE_TERM_LEN)
    return len(term)

# How much a satisfied `prefer_when` moves a lane. Small on purpose: a preference breaks a
# tie between plausible lanes, it does not overturn a clear keyword match.
PREFERENCE_WEIGHT = 6.0


def _preference(rule: dict[str, Any], direction: str | None,
                counterparty_class: str | None) -> tuple[float, bool, list[str]]:
    """(bonus, vetoed, why) from `prefer_when` and `avoid_when` in routing.yaml.

    `avoid_when` is a veto rather than a penalty, because it encodes a statement about what
    a lane *is*: a vendor quote to a contracting officer is not a vendor quote, however many
    vendor words the message contains. A veto only applies while another lane is available,
    so a vetoed lane still wins uncontested, at reduced confidence, rather than the message
    silently classifying as nothing.
    """
    facts = {"direction": direction, "counterparty_class": counterparty_class}
    bonus = 0.0
    vetoed = False
    why: list[str] = []

    for clause_name, clauses in (("prefer_when", rule.get("prefer_when") or {}),
                                 ("avoid_when", rule.get("avoid_when") or {})):
        if not clauses:
            continue
        # Every field in a clause must match. "prefer when inbound and from a government
        # counterparty" is one condition, not two independent nudges: an inbound message
        # from a distributor satisfies neither half of it on its own.
        matched = []
        for field, expected in clauses.items():
            actual = facts.get(field)
            wanted = expected if isinstance(expected, (list, tuple)) else [expected]
            if actual is None or actual not in wanted:
                matched = []
                break
            matched.append(f"{field}={actual}")
        if not matched:
            continue
        why.append(f"{clause_name}:{' and '.join(matched)}")
        if clause_name == "prefer_when":
            bonus += PREFERENCE_WEIGHT
        else:
            vetoed = True
    return bonus, vetoed, why


def classify_type(cfg: Config, text: str, direction: str | None = None,
                  counterparty_class: str | None = None) -> tuple[str | None, float, list[str]]:
    """Obligation type from the routing matrix's own term lists.

    Longest matched term wins, so "stop work" beats "work" and a specific phrase beats a
    generic one. Lanes that share vocabulary are separated by `prefer_when` and `avoid_when`
    in routing.yaml, evaluated against facts the message carries rather than a heuristic
    buried here.

    Confidence measures how clearly one lane owns the message, not how many words matched.
    A single unmistakable term is a confident classification. Two lanes each holding a
    substantive term is an ambiguous one, whatever the arithmetic says, and it drops below
    the routing threshold so a human decides instead of the longest string.
    """
    lowered = (text or "").lower()
    hits: dict[str, list[str]] = {}
    rules_by_type: dict[str, dict[str, Any]] = {}

    for rule in cfg.routing_rules:
        rules_by_type[rule["type"]] = rule
        for term in rule.get("match", []) or []:
            term_text = str(term).lower()
            # Word boundaries, except for terms ending in punctuation like "po#", where a
            # trailing \w boundary could never match.
            trailing = r"(?!\w)" if term_text[-1].isalnum() else ""
            if re.search(r"(?<!\w)" + re.escape(term_text) + trailing, lowered):
                hits.setdefault(rule["type"], []).append(term_text)

    if not hits:
        return None, 0.0, []

    scored: list[dict[str, Any]] = []
    for obligation_type, terms in hits.items():
        longest = max(_effective_length(term) for term in terms)
        bonus, vetoed, why = _preference(
            rules_by_type[obligation_type], direction, counterparty_class
        )
        scored.append({
            "type": obligation_type, "terms": terms, "longest": longest,
            "score": float(longest) + len(terms) + bonus, "vetoed": vetoed, "why": why,
            "substantive": longest >= SUBSTANTIVE_TERM_LEN,
        })

    live = [entry for entry in scored if not entry["vetoed"]]
    if live:
        vetoed_names = [e["type"] for e in scored if e["vetoed"]]
        contenders = live
    else:
        # Every candidate was vetoed. Keep the best one rather than classifying as nothing,
        # and let the confidence penalty send it to a human.
        vetoed_names = []
        contenders = scored

    contenders.sort(key=lambda entry: entry["score"], reverse=True)
    winner = contenders[0]

    confidence = (
        BASE_CONFIDENCE
        + 0.02 * min(winner["longest"], 15)
        + 0.04 * (len(winner["terms"]) - 1)
    )
    if winner["why"]:
        confidence += 0.05
    if winner["vetoed"]:
        confidence -= 0.20

    substantive_lanes = [entry for entry in contenders if entry["substantive"]]
    if len(contenders) == 1:
        confidence += 0.10                       # one lane claimed it, and only one
    elif len(substantive_lanes) > 1:
        # Two lanes each name the subject. That is real ambiguity and it belongs with a
        # human, not with whichever term happened to be longer.
        confidence -= 0.15

    why = list(winner["why"])
    if vetoed_names:
        why.append("ruled out: " + ", ".join(sorted(vetoed_names)))

    return (
        winner["type"],
        round(max(0.05, min(RULES_CEILING, confidence)), 2),
        sorted(set(winner["terms"] + why)),
    )


def _direction_for(message_direction: str, obligation_type: str,
                   counterparty_class: str) -> str:
    """Who owes whom. Getting this wrong puts the clock on the wrong party."""
    if counterparty_class == "internal":
        # Internal work: the receiver owes the sender. Recorded as we_owe_them so the
        # clock and the chase behave the same way as any other commitment.
        return "we_owe_them"
    if message_direction == "inbound":
        # Somebody outside sent us something. Whether it is an ask, an award, or a
        # stop-work, the next move is ours.
        return "we_owe_them"
    # We sent it, so we are waiting on them. The router opens this as waiting_external
    # with a clock, because waiting on a vendor is not done.
    return "they_owe_us"


def _what_is_owed(message: Any, obligation_type: str, direction: str,
                  summary: dict[str, Any]) -> str:
    """One plain sentence. An obligation nobody can read is not an obligation."""
    commitments = summary.get("commitments") or []
    if commitments:
        return _trim(commitments[0]["text"])

    subject = (message["subject"] or "").strip()
    body = (message["body_text"] or "").strip()
    ask = _first_ask_sentence(body)
    if ask:
        return _trim(ask)
    if subject:
        verb = "Respond to" if direction == "we_owe_them" else "Get a response on"
        return _trim(f"{verb}: {subject}")
    return _trim(f"{obligation_type.replace('_', ' ')} with no subject line")


ASK_SENTENCE = re.compile(r"[^.!?\n]{10,240}[.!?]")


def _first_ask_sentence(body: str) -> str | None:
    for sentence in ASK_SENTENCE.findall(body or ""):
        if rules.contains_ask(sentence):
            return sentence.strip()
    return None


def _trim(text: str, limit: int = 200) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def candidate_for(cfg: Config, store: Store, message: Any,
                  now: dt.datetime | None = None) -> Candidate | None:
    """Propose an obligation for one message, or None when there is nothing owed."""
    now = now or dt.datetime.now(dt.timezone.utc)

    if message["quarantined"]:
        # Guardrail 5: the body never left the quarantine, so there is nothing to classify.
        # The envelope still shows up in the unanswered report, and a human decides.
        return Candidate(
            message_id=message["id"], thread_key=message["thread_key"],
            type="internal_request",
            what_is_owed=f"Quarantined message needs a human: {_trim(message['subject'] or '')}",
            direction="we_owe_them", counterparty=message["counterparty"],
            counterparty_class=message["counterparty_class"] or "unknown",
            contract_ref=message["contract_ref"], source=message["source"],
            source_ref=f"{message['thread_key']}#{message['id']}",
            confidence=0.0, needs_human_review=True,
            reason="quarantined before ingest; classification would require the body",
        )

    sent_at = parse_ts(message["sent_at"]) or now
    body = message["body_text"] or ""
    summary = rules.summarize(
        cfg, body, sent_at, message["direction"],
        (message["counterparty_class"] or "") == "internal",
    )

    haystack = f"{message['subject'] or ''}\n{body}"
    obligation_type, confidence, terms = classify_type(
        cfg, haystack, message["direction"], message["counterparty_class"]
    )

    has_ask = summary["contains_ask"]
    has_commitment = bool(summary["commitments"])
    has_handoff = bool(summary["handoffs"])

    if obligation_type is None:
        if not (has_ask or has_commitment or has_handoff):
            return None                       # nothing owed, and nothing to guess about
        # Something is owed but the lane is unclear. That is what the triage queue is for.
        obligation_type = "internal_request" if has_handoff else "rfi"
        confidence = 0.30
        terms = []

    if summary["looks_like_closer"] and not (has_ask or has_commitment):
        return None                           # an acknowledgment closes, it does not open

    direction = _direction_for(
        message["direction"], obligation_type, message["counterparty_class"] or "unknown"
    )
    if has_commitment:
        # We said we would do something, so we owe them, whatever the term list matched.
        direction = "we_owe_them"

    due_at, due_basis = _due_for(cfg, summary, obligation_type,
                                 message["counterparty_class"], sent_at)
    external = (summary.get("solicitation_close") or {}).get("at")

    if has_ask or has_commitment:
        confidence = min(RULES_CEILING, confidence + 0.10)

    return Candidate(
        message_id=message["id"], thread_key=message["thread_key"], type=obligation_type,
        what_is_owed=_what_is_owed(message, obligation_type, direction, summary),
        direction=direction, counterparty=message["counterparty"],
        counterparty_class=message["counterparty_class"] or "unknown",
        contract_ref=message["contract_ref"] or summary["contract_ref"],
        source=message["source"], source_ref=f"{message['thread_key']}#{message['id']}",
        due_at=due_at, due_basis=due_basis, external_deadline=external,
        confidence=confidence,
        needs_human_review=cfg.needs_human_review(obligation_type, confidence),
        matched_terms=terms,
        reason=(
            f"matched {', '.join(terms)}" if terms
            else "no routing term matched; queued for a human to classify"
        ),
    )


def _due_for(cfg: Config, summary: dict[str, Any], obligation_type: str,
             counterparty_class: str | None, sent_at: dt.datetime) -> tuple[str | None, str | None]:
    """A real date the message states, or the SLA clock. Stated dates always win."""
    commitments = summary.get("commitments") or []
    for commitment in commitments:
        if commitment.get("due_at"):
            return commitment["due_at"], f"we committed to it: {commitment['due_text']!r}"

    # A date the counterparty asked us to hit beats anything computed. They wrote it, so it
    # is not arguable, and it is what they will measure us against.
    requested = summary.get("requested_deadline") or {}
    if requested.get("at"):
        return requested["at"], f"they asked for it: {requested.get('text')!r}"

    close = summary.get("solicitation_close") or {}
    if close.get("at"):
        return close["at"], f"external deadline: {close.get('text')!r}"

    sla = cfg.sla_for(obligation_type, counterparty_class)
    if sla.get("deadline_driven"):
        # Deadline driven with no deadline found. Leaving due_at empty is the honest
        # answer, and the auditor reports it as a gap rather than inventing a date.
        return None, f"deadline driven on {sla.get('deadline_field')}, none found in the message"

    hours = sla.get("completion_hours")
    if hours is None:
        return None, "no completion clock configured for this type"
    owner = cfg.owner_for_type(obligation_type)
    coverage = coverage_for(cfg, owner) if owner != TRIAGE_QUEUE else None
    if coverage is None:
        return None, "unassigned, so no coverage window to measure against"
    due = add_business_hours(coverage, sent_at, float(hours))
    basis = f"sla.yaml completion_hours={hours:g} on {owner}'s coverage"
    if not coverage.known:
        basis += " (coverage unconfirmed, wall clock)"
    return due.isoformat(), basis


# ---------------------------------------------------------------------------
# The model layer
# ---------------------------------------------------------------------------

# What the model is allowed to change. Everything else about a candidate is structural and
# stays under the deterministic layer's control.
MODEL_MAY_SET = {"type", "what_is_owed", "direction", "due_at", "due_basis", "confidence"}


def apply_model(cfg: Config, store: Store, candidates: list[Candidate],
                limit: int = 25, everything: bool = False) -> tuple[list[Candidate], list[str]]:
    """Grade candidates with the headless Claude layer. Returns (candidates, notes).

    By default this runs only where the deterministic layer is unsure: candidates below the
    action threshold, and types that always need a human anyway. That is where a model earns
    its cost. Spending a subprocess on a message whose lane was never in doubt buys nothing.

    Three things the model is structurally unable to do, enforced here rather than asked for
    in the prompt, because a prompt is a request and this is a guarantee:

    1.  It cannot clear human review on stop_work, contract_action, or award.
    2.  It cannot invent an owner. Owners come from the routing matrix, full stop.
    3.  It cannot raise confidence past the ceiling for a type it also flagged as ambiguous.

    That last one matters because the message body is untrusted external text. A message
    that tries to talk its way out of review moves *toward* a human, never away.
    """
    from ..extract import claude_extractor
    from ..extract import rules as rules_module

    notes: list[str] = []
    if not claude_extractor.cli_available():
        notes.append(
            "the claude CLI is not on PATH, so every verdict below is deterministic only. "
            "Confidence is capped at the rules ceiling and nothing was graded."
        )
        return candidates, notes

    threshold = float(
        (cfg.guardrails.get("confidence", {}) or {}).get("min_to_act", 0.85)
    )
    always_human = set(
        (cfg.guardrails.get("confidence", {}) or {}).get("always_human_review", []) or []
    )

    if everything:
        targets = list(candidates)
    else:
        targets = [
            candidate for candidate in candidates
            if candidate.confidence < threshold or candidate.type in always_human
        ]
    skipped = len(candidates) - len(targets)

    ungraded = 0
    if len(targets) > limit:
        ungraded = len(targets) - limit
        notes.append(
            f"{len(targets)} candidate(s) qualified for grading and the limit is {limit}, so "
            f"the {limit} least confident were graded and {ungraded} kept their deterministic "
            "verdict. Raise --model-limit to cover the rest."
        )
        targets.sort(key=lambda candidate: candidate.confidence)
        targets = targets[:limit]

    graded = 0
    failed = 0
    quarantined = 0
    for candidate in targets:
        if candidate.confidence == 0.0 and "Quarantined" in candidate.what_is_owed:
            # Guardrail 5: the body never left the quarantine, so there is nothing to send.
            quarantined += 1
            continue
        rows = store.query("SELECT * FROM messages WHERE id = ?", (candidate.message_id,))
        if not rows:
            continue
        message = rows[0]
        summary = rules_module.summarize(
            cfg, message["body_text"] or "", dt.datetime.now(dt.timezone.utc),
            message["direction"], (message["counterparty_class"] or "") == "internal",
        )
        try:
            context = claude_extractor.message_context(cfg, message, summary)
        except ValueError:
            continue

        result = claude_extractor.run(
            "triage-extract", context, store=store, actor="triage.model"
        )
        if not result.available:
            failed += 1
            continue
        graded += 1
        _merge_model_verdict(cfg, candidate, result.payload, always_human)

    if graded:
        notes.append(
            f"{graded} candidate(s) graded by the model. {skipped} were already clear enough "
            "to skip."
        )
    if quarantined:
        notes.append(
            f"{quarantined} quarantined candidate(s) were never sent to the model. "
            "Guardrail 5: the body was not stored, and a prompt is an egress path."
        )
    if failed:
        notes.append(
            f"{failed} model call(s) failed and fell back to the deterministic verdict. "
            "A failed grade never becomes an absent obligation."
        )
    if not graded and not failed and not quarantined:
        notes.append(
            "no candidate needed grading: every one was already above the action threshold."
        )
    return candidates, notes


def _merge_model_verdict(cfg: Config, candidate: Candidate, payload: dict[str, Any],
                         always_human: set[str]) -> None:
    """Fold one model verdict into a candidate, under the constraints above."""
    candidate.extractor = "rules+claude"

    routed_types = {rule.get("type") for rule in cfg.routing_rules}
    ledger_types = routed_types | {"quote_request", "rfi", "internal_request", "vendor_chase"}

    proposed = payload.get("type")
    if proposed in ledger_types and proposed != candidate.type:
        candidate.reason += f"; model reclassified from {candidate.type} to {proposed}"
        candidate.type = proposed

    if payload.get("has_obligation") is False:
        # The model can withdraw a finding, but never silently. It becomes a low-confidence
        # item for a human rather than vanishing, because a wrongly withdrawn obligation is
        # invisible and a wrongly kept one costs somebody two seconds.
        candidate.confidence = min(candidate.confidence, 0.30)
        candidate.needs_human_review = True
        candidate.reason += "; model found no obligation here, kept for a human to confirm"
        return

    for field in ("what_is_owed", "direction", "due_at", "due_basis"):
        value = payload.get(field)
        if not value or field not in MODEL_MAY_SET:
            continue
        if field == "direction" and value not in ("we_owe_them", "they_owe_us"):
            continue
        setattr(candidate, field, value)

    try:
        model_confidence = float(payload.get("confidence", candidate.confidence))
    except (TypeError, ValueError):
        model_confidence = candidate.confidence

    ambiguous = bool(payload.get("ambiguity")) or bool(payload.get("needs_human_review"))
    if ambiguous:
        # A model that says it is unsure does not also get to be confident.
        model_confidence = min(model_confidence, RULES_CEILING)
    candidate.confidence = round(max(0.05, min(1.0, model_confidence)), 2)

    # Recomputed from config, never taken from the payload, so no message body can talk its
    # way out of review.
    candidate.needs_human_review = (
        cfg.needs_human_review(candidate.type, candidate.confidence)
        or candidate.type in always_human
        or ambiguous
    )
    if payload.get("ambiguity"):
        candidate.reason += f"; model flagged ambiguity: {str(payload['ambiguity'])[:120]}"


def scan(cfg: Config, store: Store, now: dt.datetime | None = None,
         since: str | None = None) -> list[Candidate]:
    """Every message with no obligation yet, triaged.

    Deduplicated on (thread_key, type): a thread that already carries an open obligation of
    the same type does not get a second one every time somebody replies.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    sql = "SELECT * FROM messages"
    params: list[Any] = []
    if since:
        sql += " WHERE sent_at >= ?"
        params.append(since)
    sql += " ORDER BY sent_at, id"

    existing = {
        (row["source_ref"].split("#")[0], row["type"])
        for row in store.query(
            "SELECT source_ref, type FROM obligations WHERE source_ref IS NOT NULL"
        )
    }

    candidates: list[Candidate] = []
    seen: set[tuple[str, str]] = set()

    for message in store.query(sql, params):
        candidate = candidate_for(cfg, store, message, now)
        if candidate is None:
            continue
        key = (candidate.thread_key, candidate.type)
        if key in existing or key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)

    return candidates
