"""Paste bridge for portals with no usable API: DIBBS, WAWF, SAM, MySBA.

Design around the paste. These portals will not be scraped reliably, and pretending
otherwise produces a system that breaks the first time a login page changes and then
quietly stops recording awards.

So the pattern is explicit: a human pastes or uploads, the system parses what it can, and
then the system owns the follow-up. The human does one action. The machine does the
remembering, which is the part humans are bad at.

Anything the parser cannot read becomes a field for the human to fill, never a guess. A
misparsed solicitation close date would silently break the entire backward-planned chase
cadence that depends on it.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..extract import rules


@dataclass
class ParsedPaste:
    kind: str
    fields: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def needs_human(self) -> bool:
        return bool(self.missing)


DIBBS_PATTERNS = {
    "solicitation_ref": r"\b([A-Z0-9]{6}-\d{2}-[QRTUVWX]-\d{4,5})\b",
    "nsn": r"\bNSN[:\s]*([0-9]{4}-?[0-9]{2}-?[0-9]{3}-?[0-9]{4})\b",
    "nomenclature": r"(?:Item Name|Nomenclature)[:\s]+([^\n]{3,60})",
    "quantity": r"(?:Quantity|QTY)[:\s]+([0-9,]+)",
    "unit": r"(?:Unit|UI)[:\s]+([A-Z]{2})\b",
    "close_date": r"(?:Return By|Quote(?:s)? Due|Close Date|Closing)[:\s]+([^\n]{4,40})",
    "buyer": r"(?:Buyer|Contract Specialist|POC)[:\s]+([^\n]{3,60})",
    "delivery_days": r"(?:Delivery|Del)\s*(?:Days|Time)[:\s]+(\d{1,4})",
}

DIBBS_REQUIRED = ["solicitation_ref", "nsn", "close_date"]

WAWF_PATTERNS = {
    "contract_ref": r"\b([A-Z0-9]{6}-\d{2}-[ACDEFGHJKLMNPSZ]-\d{4,5})\b",
    "delivery_order": r"(?:Delivery Order|DO)[:\s#]*([0-9]{4,6})",
    "invoice_number": r"(?:Invoice(?: No| Number)?)[:\s#]*([A-Z0-9\-]{3,20})",
    "shipment_number": r"(?:Shipment(?: No| Number)?)[:\s#]*([A-Z0-9]{3,10})",
    "amount": r"(?:Amount|Total)[:\s$]*([0-9,]+\.?[0-9]{0,2})",
    "status": r"(?:Status|Document Status)[:\s]+([A-Za-z ]{3,30})",
    "acceptance_date": r"(?:Acceptance|Accepted)(?: Date)?[:\s]+([^\n]{4,30})",
}

WAWF_REQUIRED = ["contract_ref", "invoice_number"]

SAM_PATTERNS = {
    "uei": r"\b(?:UEI)[:\s]*([A-Z0-9]{12})\b",
    "cage": r"\b(?:CAGE(?: Code)?)[:\s]*([A-Z0-9]{5})\b",
    "registration_status": r"(?:Registration Status|Status)[:\s]+([A-Za-z ]{3,30})",
    "expiration_date": r"(?:Expiration(?: Date)?|Expires)[:\s]+([^\n]{4,30})",
    "purpose_of_registration": r"(?:Purpose of Registration)[:\s]+([^\n]{3,60})",
}

SAM_REQUIRED = ["expiration_date"]

PARSERS = {
    "dibbs": (DIBBS_PATTERNS, DIBBS_REQUIRED),
    "wawf": (WAWF_PATTERNS, WAWF_REQUIRED),
    "sam": (SAM_PATTERNS, SAM_REQUIRED),
}


def parse(kind: str, text: str, cfg: Config | None = None,
          now: dt.datetime | None = None) -> ParsedPaste:
    """Parse a pasted portal record. Unreadable fields are listed, never invented."""
    if kind not in PARSERS:
        raise ValueError(f"no paste parser for {kind!r}; known: {sorted(PARSERS)}")
    patterns, required = PARSERS[kind]
    result = ParsedPaste(kind=kind, raw=text)

    for name, pattern in patterns.items():
        match = re.search(pattern, text or "", re.I)
        if match:
            result.fields[name] = match.group(1).strip()

    # Resolve any date-like field to a real timestamp, or say it could not be resolved.
    reference = now or dt.datetime.now(dt.timezone.utc)
    for name in ("close_date", "expiration_date", "acceptance_date"):
        raw_value = result.fields.get(name)
        if not raw_value:
            continue
        resolved = _resolve_date(raw_value, reference)
        if resolved:
            result.fields[name + "_at"] = resolved.isoformat()
        else:
            result.missing.append(name + "_at")

    for name in required:
        if not result.fields.get(name):
            result.missing.append(name)

    return result


ABSOLUTE_DATE_PATTERNS = [
    (r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", ("y", "m", "d")),
    (r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", ("m", "d", "y")),
    (r"\b(\d{1,2})/(\d{1,2})/(\d{2})\b", ("m", "d", "yy")),
    (r"\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})\b", ("d", "mon", "y")),
    (r"\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})\b", ("mon", "d", "y")),
]


def _resolve_date(text: str, reference: dt.datetime) -> dt.datetime | None:
    """Absolute dates first. A portal record should carry one, and a relative phrase in
    pasted portal text usually means the paste captured the wrong line."""
    for pattern, order in ABSOLUTE_DATE_PATTERNS:
        match = re.search(pattern, text)
        if not match:
            continue
        parts = dict(zip(order, match.groups()))
        try:
            year = int(parts.get("y") or (2000 + int(parts["yy"])))
            month = (
                int(parts["m"]) if "m" in parts
                else rules.MONTHS[parts["mon"].lower()[:3]]
            )
            return dt.datetime(year, month, int(parts["d"]), 17, 0, tzinfo=dt.timezone.utc)
        except (KeyError, ValueError):
            continue
    found = rules.resolve_deadline(text, reference)
    return found.at if found else None
