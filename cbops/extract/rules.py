"""Deterministic signal detection: references, commitments, handoffs, deadlines.

Nothing here decides anything. It finds candidates and scores how strongly the language
supports each one. The model layer and the human triage queue decide.

Bias is toward recall. A false positive costs a human two seconds in triage. A false
negative is a broken promise nobody knew about.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import Config

# ---------------------------------------------------------------------------
# References. Contract number is the primary key across every system, so tying a
# message to one is the highest-value thing this module does.
# ---------------------------------------------------------------------------


@dataclass
class References:
    contract: list[str] = field(default_factory=list)
    solicitation: list[str] = field(default_factory=list)
    nsn: list[str] = field(default_factory=list)
    purchase_order: list[str] = field(default_factory=list)
    rfq: list[str] = field(default_factory=list)
    delivery_order: list[str] = field(default_factory=list)

    @property
    def primary(self) -> str | None:
        """Best single reference for the contract_ref column."""
        for bucket in (self.contract, self.solicitation, self.purchase_order, self.rfq):
            if bucket:
                return bucket[0]
        return None

    def as_dict(self) -> dict[str, list[str]]:
        return {k: v for k, v in self.__dict__.items() if v}


def find_references(cfg: Config, text: str) -> References:
    patterns = cfg.naming.get("reference_patterns", {}) or {}
    refs = References()

    def grab(pattern: str) -> list[str]:
        out: list[str] = []
        for match in re.finditer(pattern, text or "", re.I):
            value = (match.group(1) if match.groups() else match.group(0)).strip()
            if value.upper() not in {v.upper() for v in out}:
                out.append(value)
        return out

    if "dla_contract" in patterns:
        refs.contract = grab(patterns["dla_contract"])
    if "dla_solicitation" in patterns:
        # A DLA contract and solicitation look alike, so anything already matched as a
        # contract is not double counted as a solicitation.
        seen = {v.upper() for v in refs.contract}
        refs.solicitation = [v for v in grab(patterns["dla_solicitation"]) if v.upper() not in seen]
    if "nsn" in patterns:
        refs.nsn = [v for v in grab(patterns["nsn"]) if _plausible_nsn(v)]
    if "purchase_order" in patterns:
        refs.purchase_order = grab(patterns["purchase_order"])
    if "rfq" in patterns:
        refs.rfq = grab(patterns["rfq"])
    if "delivery_order" in patterns:
        refs.delivery_order = grab(patterns["delivery_order"])
    return refs


def _plausible_nsn(value: str) -> bool:
    """An NSN is 13 digits. A phone number and a date range are not."""
    digits = re.sub(r"\D", "", value)
    return len(digits) == 13


# ---------------------------------------------------------------------------
# Deadlines and relative dates
# ---------------------------------------------------------------------------

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4,
    "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8,
    "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# End of business, in local hours. Used when a promise names a day but not a time.
EOD_HOUR = 17

DUE_PATTERNS: list[tuple[str, str]] = [
    (r"\b(today|this morning|this afternoon|by end of day|by eod|eod|by close of business|by cob)\b", "today"),
    (r"\b(tomorrow|tmrw|first thing tomorrow)\b", "tomorrow"),
    (r"\b(end of (the )?week|eow|by friday)\b", "eow"),
    (r"\b(next week|early next week|beginning of next week)\b", "next_week"),
    (r"\bwithin (\d+) (hour|hours|business hours)\b", "hours"),
    (r"\bin (\d+) (day|days|business days)\b", "days"),
    (r"\bwithin (\d+) (day|days|business days)\b", "days"),
    (r"\b(?:by|on|before)?\s*(mon|monday|tue|tues|tuesday|wed|wednesday|thu|thur|thurs|thursday|fri|friday)\b", "weekday"),
    # The preposition is optional because callers hand this function a date phrase that
    # has already been split off its prefix. `find_solicitation_close` matches
    # "Quotes are due by 8/27" and passes "8/27" here, so requiring "by" would drop
    # exactly the external deadline the whole vendor chase cadence hangs off.
    (r"\b(?:by|on|before)?\s*(jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\.?\s+(\d{1,2})\b", "month_day"),
    (r"\b(?:by|on|before)?\s*(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", "numeric_date"),
    (r"\b(?:by|before)\s+the\s+(\d{1,2})(?:st|nd|rd|th)\b", "day_of_month"),
]


@dataclass
class Deadline:
    text: str
    at: dt.datetime | None
    precision: str   # "exact" | "day" | "week" | "vague"


def resolve_deadline(text: str, sent_at: dt.datetime) -> Deadline | None:
    """First deadline phrase in `text`, resolved against when the message was sent.

    Resolution is intentionally conservative: a day-precision promise resolves to end of
    business that day, so "Friday" does not read as breached at 9am Friday.
    """
    lowered = (text or "").lower()
    for pattern, kind in DUE_PATTERNS:
        match = re.search(pattern, lowered)
        if not match:
            continue
        phrase = match.group(0).strip()
        resolved = _resolve(kind, match, sent_at)
        if resolved is not None:
            precision = {"hours": "exact", "eow": "week", "next_week": "week"}.get(kind, "day")
            return Deadline(text=phrase, at=resolved, precision=precision)
    return None


def _eod(day: dt.date, tz: dt.tzinfo) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(EOD_HOUR, 0), tzinfo=tz)


def _resolve(kind: str, match: re.Match[str], sent_at: dt.datetime) -> dt.datetime | None:
    tz = sent_at.tzinfo or dt.timezone.utc
    today = sent_at.date()

    if kind == "today":
        return _eod(today, tz)
    if kind == "tomorrow":
        return _eod(today + dt.timedelta(days=1), tz)
    if kind == "eow":
        return _eod(today + dt.timedelta(days=(4 - today.weekday()) % 7), tz)
    if kind == "next_week":
        # Wednesday of next week: the honest midpoint of a vague commitment.
        return _eod(today + dt.timedelta(days=(7 - today.weekday()) + 2), tz)
    if kind == "hours":
        return sent_at + dt.timedelta(hours=int(match.group(1)))
    if kind == "days":
        return _eod(today + dt.timedelta(days=int(match.group(1))), tz)
    if kind == "weekday":
        target = WEEKDAYS[match.group(1)]
        ahead = (target - today.weekday()) % 7 or 7
        return _eod(today + dt.timedelta(days=ahead), tz)
    if kind == "month_day":
        month = MONTHS[match.group(1)]
        day = int(match.group(2))
        year = today.year + (1 if month < today.month - 6 else 0)
        try:
            return _eod(dt.date(year, month, day), tz)
        except ValueError:
            return None
    if kind == "numeric_date":
        month, day = int(match.group(1)), int(match.group(2))
        year_text = match.group(3)
        year = today.year if not year_text else int(year_text) + (2000 if len(year_text) == 2 else 0)
        try:
            return _eod(dt.date(year, month, day), tz)
        except ValueError:
            return None
    if kind == "day_of_month":
        day = int(match.group(1))
        month, year = today.month, today.year
        if day < today.day:                      # already past, so next month
            month, year = (1, year + 1) if month == 12 else (month + 1, year)
        try:
            return _eod(dt.date(year, month, day), tz)
        except ValueError:
            return None
    return None


SOLICITATION_CLOSE_PATTERNS = [
    r"(?:quotes?|offers?|responses?|bids?)\s+(?:are\s+)?due\s+(?:by\s+|on\s+|no later than\s+)?(?P<date>[^\n.;]{4,40})",
    # DIBBS field labels, which show up verbatim in forwarded solicitation mail.
    r"(?:return by|offer due date|quote due date)[:\s]+(?P<date>[^\n.;]{4,40})",
    r"close[sd]?\s+(?:on\s+|at\s+)?(?P<date>\d{1,2}/\d{1,2}(?:/\d{2,4})?)",
    r"(?:closing|response)\s+date[:\s]+(?P<date>[^\n.;]{4,40})",
    r"no later than\s+(?P<date>[^\n.;]{4,40})",
]


# What the other party is asking of us, as opposed to when a solicitation closes. Both can
# appear in one message and they drive different clocks: a close date sets the vendor chase
# cadence, a requested date sets what we owe and when.
# The preposition stays inside the captured group on purpose. `resolve_deadline` uses
# "by" / "before" to tell a date from a number, so consuming it here would either drop
# "by the 28th" or force the date parser to accept a bare ordinal, and a bare ordinal makes
# "the 3rd party" parse as a deadline.
REQUESTED_DEADLINE_PATTERNS = [
    r"\b(?:we|i)\s+(?:need|require|want)\s+(?:it|this|that|them|the\s+\w+|pricing|a\s+quote|the\s+quote|quotes?)?\s*(?P<date>(?:by|before|no later than)\s+[^\n.;,]{3,40})",
    r"\b(?:need|require|want)s?\s+(?:to be\s+)?(?:it|this|that|them|pricing|quotes?|the\s+\w+)?\s*(?P<date>(?:by|before|no later than)\s+[^\n.;,]{3,40})",
    r"\b(?:required|needed|wanted|expected)\s+(?:back\s+)?(?P<date>(?:by|before|no later than)\s+[^\n.;,]{3,40})",
    r"\bmust\s+be\s+(?:received|submitted|delivered|returned|completed)\s+(?P<date>(?:by|before|no later than)\s+[^\n.;,]{3,40})",
    r"\b(?:deadline|cut ?off)\s+(?:is|:)\s*(?P<date>[^\n.;,]{3,40})",
    r"\b(?:in hand|on site|on our dock)\s+(?P<date>(?:by|before|no later than)\s+[^\n.;,]{3,40})",
    r"\b(?:please|kindly)\s+(?:send|provide|confirm|advise|quote|respond|return)[^\n.;]{0,60}?(?P<date>(?:by|before|no later than)\s+[^\n.;,]{3,40})",
]


def find_requested_deadline(text: str, sent_at: dt.datetime) -> Deadline | None:
    """A date the other party is asking us to hit.

    Without this, an inbound "we need pricing by the 28th" falls back to a generic SLA
    clock, which is both less accurate and less persuasive: a due date a customer wrote
    themselves is not arguable, and a computed one always is.
    """
    for pattern in REQUESTED_DEADLINE_PATTERNS:
        match = re.search(pattern, text or "", re.I)
        if not match:
            continue
        found = resolve_deadline(match.group("date"), sent_at)
        if found:
            return Deadline(text=match.group(0).strip()[:80], at=found.at,
                            precision=found.precision)
    return None


def find_solicitation_close(text: str, sent_at: dt.datetime) -> Deadline | None:
    """The external clock a vendor chase should hang off, not a fixed interval."""
    for pattern in SOLICITATION_CLOSE_PATTERNS:
        match = re.search(pattern, text or "", re.I)
        if not match:
            continue
        found = resolve_deadline(match.group("date"), sent_at)
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# Commitments. The Broken Promise Report reads these.
# ---------------------------------------------------------------------------

# (pattern, base confidence). Ordered strongest first; the strongest match wins.
COMMITMENT_PATTERNS: list[tuple[str, float]] = [
    # Contractions carry no space before the modal, so \s* not \s+. Missing this drops
    # "I'll send the quote Friday", which is the single most common real promise shape.
    (r"\b(?:i|we)\s*(?:will|'ll|’ll|shall)\s+(?:send|get|have|provide|forward|submit|issue|ship|call|email|follow up|circle back|confirm|check|review|update|price|quote|pull|put together)\b", 0.80),
    (r"\b(?:i|we)\s*(?:am|'m|’m|are|'re|’re)\s+(?:going to|about to)\s+\w+", 0.70),
    (r"\byou(?:'ll| will)\s+(?:have|receive|get|see)\b", 0.75),
    (r"\b(?:sending|sending over|getting)\s+(?:it|that|this|the \w+)\s+(?:to you\s+)?(?:today|tomorrow|shortly|this week)\b", 0.70),
    (r"\b(?:quote|pricing|proposal|invoice|paperwork|documents?|cert(?:ificate)?s?|drawings?)\s+(?:is|are|will be|coming|to follow)\b.{0,30}\b(?:today|tomorrow|shortly|this week|by)\b", 0.70),
    (r"\b(?:let me)\s+(?:get|send|pull|price|check on)\b", 0.65),
    (r"\b(?:i\s*'?’?ll|i will)\s+let you know\b", 0.65),
    (r"\bwe\s*(?:'ll|’ll| will)\s+(?:have|get)\s+(?:that|this|it)\s+(?:to you|over|out)\b", 0.75),
    (r"\bon (?:it|my list)\b.{0,20}\b(?:today|tomorrow|this week)\b", 0.55),
    (r"\b(?:will|i\s*'?’?ll)\s+(?:chase|ping|nudge|reach out to)\b", 0.60),
    (r"\b(?:i|we)\s*(?:'ll|’ll|will)\s+\w+", 0.45),
]

# Language that weakens a commitment. Present tense of intent, not of obligation.
HEDGES = [
    (r"\b(?:try|trying|attempt|hope|hopefully|aim|should be able|if (?:i|we) can|might|may|probably|likely)\b", -0.20),
    (r"\b(?:pending|subject to|assuming|once (?:i|we) (?:hear|get))\b", -0.15),
    (r"\?\s*$", -0.10),
]

# Language that strengthens it. An explicit named deliverable and date is a real promise.
BOOSTS = [
    (r"\b(?:no later than|by end of day|by eod|by cob|committed|confirmed|guarantee)\b", 0.10),
    (r"\b(?:quote|invoice|po|purchase order|packing slip|cert|dd250|drawing)\b", 0.05),
]

SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")

# A regex has no idea what a sentence means, so rules confidence is capped below
# guardrails.confidence.min_to_act (0.85). Only the model layer or a human raises a
# finding above the line where the system is allowed to act on it.
RULES_CONFIDENCE_CEILING = 0.80


@dataclass
class Commitment:
    text: str
    due: Deadline | None
    confidence: float
    detector: str


def find_commitments(text: str, sent_at: dt.datetime) -> list[Commitment]:
    """Commitment sentences in outbound text, scored.

    Runs on the author's new text only, never on quoted history, or a single promise gets
    rediscovered on every reply in the thread.
    """
    found: list[Commitment] = []
    for sentence in SENTENCE_SPLIT.split(text or ""):
        clean = sentence.strip()
        if not 8 <= len(clean) <= 400:
            continue
        lowered = clean.lower()

        best = 0.0
        for pattern, score in COMMITMENT_PATTERNS:
            if re.search(pattern, lowered) and score > best:
                best = score
        if best == 0.0:
            continue

        for pattern, delta in HEDGES:
            if re.search(pattern, lowered):
                best += delta
        for pattern, delta in BOOSTS:
            if re.search(pattern, lowered):
                best += delta

        due = resolve_deadline(clean, sent_at)
        if due:
            best += 0.10
        # A commitment with no date is real but unmeasurable, so it is reported
        # separately rather than scored as if it had a deadline.
        found.append(
            Commitment(
                text=clean,
                due=due,
                confidence=round(max(0.05, min(RULES_CONFIDENCE_CEILING, best)), 2),
                detector="rules.commitment",
            )
        )
    return found


# Evidence that a promise was kept: the follow-up actually happened.
FOLLOWTHROUGH_PATTERNS = [
    r"\b(?:attached|enclosed|please find|here (?:it|they) (?:is|are)|as promised|per my (?:note|email))\b",
    r"\b(?:sent|submitted|issued|uploaded|shipped|filed)\b",
]


def looks_like_followthrough(text: str, has_attachments: bool) -> bool:
    if has_attachments:
        return True
    lowered = (text or "").lower()
    return any(re.search(p, lowered) for p in FOLLOWTHROUGH_PATTERNS)


# ---------------------------------------------------------------------------
# Internal handoffs. The Dropped Handoff Report reads these.
# ---------------------------------------------------------------------------

HANDOFF_TEMPLATES = [
    (r"\b{name}\b[,:]?\s*(?:can|could|will|would)\s+you\b", 0.80),
    (r"\b{name}\b[,:]?\s*(?:please|pls)\b", 0.80),
    (r"\b(?:assigning|handing|passing|sending|routing|over)\s+(?:this|it|that)?\s*(?:to|over to)\s+{name}\b", 0.85),
    (r"\b{name}\s+(?:will|is going to|can)\s+(?:take|handle|own|pick up|run with)\b", 0.80),
    (r"\b(?:@){name}\b", 0.70),
    (r"\b(?:ask|check with|loop(?:ing)? in|cc'?(?:ing)?)\s+{name}\b", 0.55),
    (r"\b{name}\b[,:]?\s*(?:any update|status|where are we)\b", 0.65),
]


@dataclass
class Handoff:
    to_person: str
    text: str
    confidence: float
    detector: str


def find_handoffs(cfg: Config, text: str) -> list[Handoff]:
    """Work passed to a named person.

    Matches on first names because that is how internal mail actually reads. Names are
    taken from people.yaml, so a customer called Jason does not become a handoff to our
    Jason unless the thread is internal, which the caller checks.
    """
    results: dict[str, Handoff] = {}
    for pid, person in (cfg.people.get("people", {}) or {}).items():
        first = str(person.get("name", pid)).split()[0]
        escaped = re.escape(first)
        for template, score in HANDOFF_TEMPLATES:
            pattern = template.format(name=escaped)
            match = re.search(pattern, text or "", re.I)
            if not match:
                continue
            start = max(0, match.start() - 40)
            snippet = (text or "")[start : match.end() + 120].strip()
            existing = results.get(pid)
            if existing is None or score > existing.confidence:
                results[pid] = Handoff(
                    to_person=pid, text=snippet, confidence=score, detector="rules.handoff"
                )
    return list(results.values())


# ---------------------------------------------------------------------------
# Asks. Used to tell an unanswered thread from a thread that just ended.
# ---------------------------------------------------------------------------

ASK_PATTERNS = [
    r"\?",
    r"\b(?:please|kindly)\s+(?:send|provide|confirm|advise|review|sign|quote|approve)\b",
    r"\b(?:can|could|would)\s+you\b",
    r"\b(?:need|require|requesting|request)\s+(?:your|a|an|the)\b",
    r"\b(?:awaiting|waiting (?:on|for))\b",
    r"\b(?:let (?:me|us) know|advise|confirm receipt)\b",
    r"\b(?:quote|pricing|lead ?time|availability|eta)\b.{0,40}\?",
]

# A closing message does not need a reply, so counting it as unanswered is noise.
CLOSER_PATTERNS = [
    r"\b(?:thanks|thank you|thx|much appreciated|appreciate it|got it|received|will do|sounds good|perfect|no (?:problem|worries))\b\W*$",
    r"\b(?:no (?:action|response) (?:needed|required)|fyi only|for your records)\b",
]


def contains_ask(text: str) -> bool:
    lowered = (text or "").lower()
    return any(re.search(p, lowered) for p in ASK_PATTERNS)


def looks_like_closer(text: str) -> bool:
    stripped = (text or "").strip().lower()
    if len(stripped) <= 60 and any(re.search(p, stripped) for p in CLOSER_PATTERNS):
        return True
    return any(re.search(p, stripped) for p in CLOSER_PATTERNS[1:])


def _deadline_dict(found: "Deadline | None") -> dict[str, Any] | None:
    if found is None:
        return None
    return {"text": found.text, "at": found.at.isoformat() if found.at else None}


def summarize(cfg: Config, text: str, sent_at: dt.datetime,
              direction: str, is_internal: bool) -> dict[str, Any]:
    """Everything the rules layer can say about one message, in one pass."""
    refs = find_references(cfg, text)
    return {
        "references": refs.as_dict(),
        "contract_ref": refs.primary,
        "commitments": [
            {
                "text": c.text,
                "due_text": c.due.text if c.due else None,
                "due_at": c.due.at.isoformat() if c.due and c.due.at else None,
                "confidence": c.confidence,
            }
            for c in (find_commitments(text, sent_at) if direction != "inbound" else [])
        ],
        "handoffs": [
            {"to_person": h.to_person, "text": h.text, "confidence": h.confidence}
            for h in (find_handoffs(cfg, text) if is_internal else [])
        ],
        "solicitation_close": _deadline_dict(find_solicitation_close(text, sent_at)),
        # Only meaningful on inbound mail: a date we asked someone else to hit is a chase,
        # not something we owe, and the commitment detector already covers our own promises.
        "requested_deadline": (
            _deadline_dict(find_requested_deadline(text, sent_at))
            if direction == "inbound" else None
        ),
        "contains_ask": contains_ask(text),
        "looks_like_closer": looks_like_closer(text),
    }
