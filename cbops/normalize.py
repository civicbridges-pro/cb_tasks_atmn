"""Raw transport into one normalized message shape.

Every source (IMAP, Gmail API, Telegram export, portal paste) produces the same dict, so
nothing downstream knows or cares where a message came from. That is what makes the mail
path decision reversible: swapping Namecheap IMAP for Google Workspace changes one module
in cbops/ingest/ and nothing here.

Classification happens *before* the body is stored, not after, because guardrail 5 says
CUI and export-controlled content gets quarantined before ingest. A quarantined message
keeps its envelope, so the Unanswered Thread Report still counts it, and drops its body
and attachment payload.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import re
from email.header import decode_header, make_header
from email.message import Message
from typing import Any

from .config import Config

SNIPPET_LEN = 400

# Quoted-reply and signature boundaries. Everything below the first hit is prior context,
# and counting it as new text is how a promise detector finds the same promise nine times.
QUOTE_MARKERS = [
    re.compile(r"^\s*On .{0,80}\bwrote:\s*$", re.M),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.M | re.I),
    re.compile(r"^\s*From:\s.+$", re.M),
    re.compile(r"^\s*_{10,}\s*$", re.M),
    re.compile(r"^\s*Sent from my \w+", re.M),
    re.compile(r"^-- \s*$", re.M),
]

DISCLAIMER_MARKERS = [
    re.compile(r"this (e-?mail|message) (and any attachments )?(is|are) (intended|confidential)", re.I),
    re.compile(r"if you (are not|have received) th(e|is) (intended recipient|message in error)", re.I),
    re.compile(r"CAUTION: This email originated (from )?outside", re.I),
]


def decode(value: str | None) -> str:
    """Header decoding that never raises. A weird header is not a reason to drop mail."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def addr_of(value: str | None) -> str:
    return (email.utils.parseaddr(value or "")[1] or "").strip().lower()


def name_of(value: str | None) -> str:
    return decode(email.utils.parseaddr(value or "")[0] or "")


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


def addr_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [a.lower() for _, a in email.utils.getaddresses([value]) if a]


def parse_date(value: str | None) -> dt.datetime:
    """Header date, normalized to UTC. Undated mail sorts to epoch, never to now.

    Sorting an undated message to now would put it at the top of every report, which is
    exactly the wrong place for a message we know least about.
    """
    if value:
        try:
            stamp = email.utils.parsedate_to_datetime(value)
            if stamp is not None:
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=dt.timezone.utc)
                return stamp.astimezone(dt.timezone.utc)
        except (TypeError, ValueError):
            pass
    return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def body_text(msg: Message) -> tuple[str, list[str]]:
    """Plain text body plus attachment filenames. HTML is a fallback, never preferred."""
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[str] = []

    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        disposition = str(part.get("Content-Disposition") or "")
        if filename or "attachment" in disposition.lower():
            attachments.append(decode(filename) or "(unnamed)")
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
        except (LookupError, ValueError):
            continue
        if part.get_content_type() == "text/plain":
            text_parts.append(decoded)
        elif part.get_content_type() == "text/html":
            html_parts.append(decoded)

    if text_parts:
        return "\n".join(text_parts), attachments
    return strip_html("\n".join(html_parts)), attachments


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def new_text(body: str) -> str:
    """Just what this author wrote, with quoted history and boilerplate removed."""
    cut = len(body)
    for marker in QUOTE_MARKERS:
        found = marker.search(body)
        if found and found.start() < cut:
            cut = found.start()
    head = body[:cut]
    lines = [ln for ln in head.splitlines() if not ln.lstrip().startswith(">")]
    text = "\n".join(lines)
    for marker in DISCLAIMER_MARKERS:
        found = marker.search(text)
        if found:
            text = text[: found.start()]
    return text.strip()


SUBJECT_PREFIX_RE = re.compile(r"^\s*((re|fw|fwd|aw|sv|antw|rv)\s*(\[\d+\])?\s*:\s*)+", re.I)


def normalize_subject(subject: str) -> str:
    text = SUBJECT_PREFIX_RE.sub("", subject or "")
    return re.sub(r"\s+", " ", text).strip().lower()


def thread_key(msg_id: str, in_reply_to: str, references: str, subject: str,
               participants: list[str]) -> str:
    """Stable thread identity.

    Resolution order:

    1.  The root of the References chain, which every mail client honors.
    2.  The message's own Message-ID, when it starts a thread. This is what makes a root
        and its replies agree: the replies name it as their root, so it must name itself.
        Skipping this step silently splits every two-message thread in half, and each half
        then looks unanswered.
    3.  Normalized subject plus participants, for mail with no usable identifiers at all.

    A reply whose client dropped References still lands in its own thread here. That case
    is caught afterward by `reconcile_threads`, which merges on subject and counterparty.
    """
    root = ""
    if references:
        parts = references.split()
        if parts:
            root = parts[0].strip("<>")
    if not root and in_reply_to:
        root = in_reply_to.strip("<>")
    if not root and msg_id.strip():
        root = msg_id.strip().strip("<>")
    if root:
        return "mid:" + root

    external = sorted({p for p in participants if p})
    digest = hashlib.sha1(
        (normalize_subject(subject) + "|" + ",".join(external)).encode()
    ).hexdigest()[:16]
    return "subj:" + digest


def classify_counterparty(cfg: Config, addresses: list[str]) -> tuple[str, str]:
    """(counterparty domain, class). The highest-importance non-internal party wins.

    A thread with a contracting officer and a distributor on it is a gov thread. Grading
    down to the distributor would put a .mil deadline on a distributor's clock.
    """
    classes = cfg.counterparties.get("classes", {}) or {}
    best: tuple[int, str, str] = (-1, "", "unknown")

    for address in addresses:
        domain = domain_of(address)
        if not domain:
            continue
        if domain in cfg.our_domains:
            candidate = "internal"
        else:
            candidate = _class_for_domain(classes, domain)
        importance = int(classes.get(candidate, {}).get("importance", 50))
        if importance > best[0]:
            best = (importance, domain, candidate)

    return best[1], best[2]


def _class_for_domain(classes: dict[str, Any], domain: str) -> str:
    for name, spec in classes.items():
        for listed in spec.get("domains", []) or []:
            if domain == str(listed).lower() or domain.endswith("." + str(listed).lower()):
                return name
    for name, spec in classes.items():
        for suffix in spec.get("domain_suffixes", []) or []:
            if domain.endswith(str(suffix).lower()):
                return name
    for name, spec in classes.items():
        for pattern in spec.get("domain_patterns", []) or []:
            if re.search(pattern, domain, re.I):
                return name
    return "unknown"


def classify_sensitivity(cfg: Config, subject: str, body: str,
                         attachments: list[str]) -> tuple[bool, str]:
    """Guardrail 5. Quarantine before ingest, and when in doubt, quarantine."""
    rules = cfg.guardrails.get("classification", {}) or {}
    if not rules.get("quarantine_on_match", True):
        return False, ""

    haystack = f"{subject}\n{body}".upper()
    for marker in rules.get("cui_markers", []) or []:
        if str(marker).upper() in haystack:
            return True, f"marker: {marker}"

    blocked = {str(e).lower() for e in rules.get("quarantine_attachment_extensions", []) or []}
    for filename in attachments:
        lowered = filename.lower()
        for extension in blocked:
            if lowered.endswith(extension):
                return True, f"attachment type: {extension} ({filename})"
    return False, ""


def is_noise(cfg: Config, from_addr: str, subject: str) -> bool:
    """Auto-replies and bounces. Excluded so the leak counts stay honest."""
    for prefix in cfg.counterparties.get("ignore_senders", []) or []:
        if from_addr.startswith(str(prefix).lower()):
            return True
    for pattern in cfg.counterparties.get("ignore_subject_patterns", []) or []:
        if re.search(pattern, subject or "", re.I):
            return True
    return False


def direction_of(cfg: Config, from_addr: str, recipients: list[str]) -> str:
    """inbound, outbound, or internal. Internal is both ends inside our domains."""
    from_us = domain_of(from_addr) in cfg.our_domains
    to_domains = {domain_of(a) for a in recipients if a}
    to_all_us = bool(to_domains) and to_domains <= cfg.our_domains
    if from_us and to_all_us:
        return "internal"
    return "outbound" if from_us else "inbound"


def from_email_message(cfg: Config, msg: Message, mailbox: str,
                       raw_ref: str | None = None) -> dict[str, Any] | None:
    """One parsed email into the normalized shape. None means deliberately skipped."""
    from_addr = addr_of(msg.get("From"))
    subject = decode(msg.get("Subject"))
    if is_noise(cfg, from_addr, subject):
        return None

    to_addrs = addr_list(msg.get("To"))
    cc_addrs = addr_list(msg.get("Cc"))
    recipients = to_addrs + cc_addrs
    body, attachments = body_text(msg)
    quarantined, reason = classify_sensitivity(cfg, subject, body, attachments)

    participants = [a for a in [from_addr, *recipients] if domain_of(a) not in cfg.our_domains]
    counterparty, cclass = classify_counterparty(cfg, [from_addr, *recipients])
    visible = new_text(body)

    return {
        "source": "email",
        "mailbox": mailbox,
        "message_id": (msg.get("Message-ID") or "").strip().strip("<>") or None,
        "thread_key": thread_key(
            msg.get("Message-ID") or "", msg.get("In-Reply-To") or "",
            msg.get("References") or "", subject, participants or recipients,
        ),
        "in_reply_to": (msg.get("In-Reply-To") or "").strip().strip("<>") or None,
        "refs": (msg.get("References") or "").strip() or None,
        "from_addr": from_addr,
        "from_name": name_of(msg.get("From")),
        "to_addrs": to_addrs,
        "cc_addrs": cc_addrs,
        "subject": subject,
        "sent_at": parse_date(msg.get("Date")).isoformat(),
        "direction": direction_of(cfg, from_addr, recipients),
        # A quarantined message keeps its envelope so threads stay countable, and loses
        # its body so nothing sensitive lands in the store or in a prompt.
        "body_text": None if quarantined else visible,
        "snippet": "(quarantined)" if quarantined else visible[:SNIPPET_LEN],
        "has_attachments": 1 if attachments else 0,
        "attachment_names": [] if quarantined else attachments,
        "counterparty": counterparty,
        "counterparty_class": cclass,
        "contract_ref": None,   # filled by cbops.extract.rules
        "quarantined": 1 if quarantined else 0,
        "quarantine_reason": reason or None,
        "raw_ref": raw_ref,
    }
