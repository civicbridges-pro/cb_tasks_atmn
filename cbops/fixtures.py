"""Turn a real thread in the store into an anonymized fixture.

Extraction quality is entirely a function of how many real messy examples the detectors have
seen, so every disagreement found during validation should end up here as a regression test.
This is the tool that makes that cheap enough to actually happen.

**What this does and does not do.** It mechanically replaces external addresses, names,
domains, and identifiers with stable fakes, and redacts money and phone numbers. It cannot
know that the third paragraph names a customer's program, or that a sentence identifies a
person by role. So it prints a warning and requires a human to read the output before it is
committed. A tool that claimed to anonymize prose would be worse than one that admits it
cannot: the false confidence is the danger.

The output is reconstructed from the store rather than copied from the original mail, so no
hidden headers, tracking metadata, or attachment payloads survive. Only the envelope fields
and the visible text the pipeline actually reasons about.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from .config import Config
from .store import Store, parse_ts

# Formats preserved, digits replaced, so the detectors are exercised identically while no
# real contract, item, or order number leaves the building.
PIID_RE = re.compile(r"\b[A-Z0-9]{6}-\d{2}-[A-Z]-\d{4,5}\b")
NSN_RE = re.compile(r"\b\d{4}-?\d{2}-?\d{3}-?\d{4}\b")
# The lookahead requires a digit in the captured group. Without it "signed PO today" has
# "today" rewritten into a fake order number, which silently changes what the sentence says
# and corrupts the very fixture that was meant to test that phrasing.
PO_RE = re.compile(
    r"\b(?:PO|P\.O\.|RFQ)\s*(?:no\.?|number|num|#)?\s*((?=[A-Z0-9\-]*[0-9])[A-Z0-9\-]{3,20})\b",
    re.I,
)
MONEY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?")
PHONE_RE = re.compile(r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b")

# Deliberately obvious. A fixture that looks like real mail invites someone to treat it as
# real mail.
FAKE_DOMAIN_SUFFIX = ".example"


@dataclass
class Anonymizer:
    """Stable, reversible-looking-but-not-reversible substitutions within one export."""

    our_domains: set[str]
    domains: dict[str, str] = field(default_factory=dict)
    addresses: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)

    def domain(self, domain: str) -> str:
        domain = domain.lower()
        if not domain:
            return domain
        # Our own domain stays: it is already public in this repo, and direction
        # classification depends on it.
        if domain in self.our_domains:
            return domain
        if domain not in self.domains:
            if domain.endswith((".mil", ".gov")):
                # Keep the suffix so gov classification still fires, drop the agency.
                suffix = domain.rsplit(".", 1)[1]
                self.domains[domain] = f"agency{len(self.domains) + 1}.{suffix}"
            elif ".k12." in domain or domain.endswith(".us"):
                self.domains[domain] = f"district{len(self.domains) + 1}.k12.id.us"
            else:
                self.domains[domain] = f"counterparty{len(self.domains) + 1}{FAKE_DOMAIN_SUFFIX}"
        return self.domains[domain]

    def address(self, address: str) -> str:
        address = (address or "").lower().strip()
        if "@" not in address:
            return address
        if address in self.addresses:
            return self.addresses[address]
        local, domain = address.rsplit("@", 1)
        if domain.lower() in self.our_domains:
            self.addresses[address] = address          # internal people are already in config
            return address
        # Keep role accounts recognizable: a shared alias behaves differently from a person.
        role = local.lower() if local.lower() in {
            "sales", "orders", "quotes", "info", "support", "purchasing", "contracts",
            "procurement", "accounting", "billing", "admin", "help", "service", "ap", "ar"
        } else f"contact{len(self.addresses) + 1}"
        self.addresses[address] = f"{role}@{self.domain(domain)}"
        return self.addresses[address]

    def name(self, name: str, address: str) -> str:
        clean = (name or "").strip()
        if not clean:
            return ""
        if address.rsplit("@", 1)[-1].lower() in self.our_domains:
            return clean
        if clean not in self.names:
            label = f"Contact {len({v for v in self.names.values()}) + 1}"
            self.names[clean] = label
            # Register each name part too. Mail signs off with a first name far more often
            # than a full one, and a sign-off that survives anonymization is the single most
            # likely way a real person's name ends up committed to this repo.
            for part in clean.split():
                token = part.strip(".,;:()<>\"'")
                if len(token) >= 3 and token.isalpha() and token not in self.names:
                    self.names[token] = label
        return self.names[clean]

    def ref(self, value: str, kind: str) -> str:
        key = f"{kind}:{value.upper()}"
        if key not in self.refs:
            digest = int(hashlib.sha1(key.encode()).hexdigest()[:8], 16)
            if kind == "piid":
                letter = value.split("-")[2] if value.count("-") >= 2 else "D"
                self.refs[key] = f"SPE{digest % 900 + 100}A6-{value.split('-')[1]}-{letter}-{digest % 9000 + 1000}"
            elif kind == "nsn":
                self.refs[key] = f"{digest % 9000 + 1000}-{digest % 90 + 10}-{digest % 900 + 100}-{digest % 9000 + 1000}"
            else:
                self.refs[key] = f"CB-{digest % 9000 + 1000}"
        return self.refs[key]

    def text(self, body: str) -> str:
        """Substitute everything mechanically findable. Prose is the human's job."""
        out = body or ""
        for original, replacement in sorted(
            self.addresses.items(), key=lambda kv: -len(kv[0])
        ):
            out = re.sub(re.escape(original), replacement, out, flags=re.I)
        for original, replacement in sorted(self.domains.items(), key=lambda kv: -len(kv[0])):
            out = re.sub(r"\b" + re.escape(original) + r"\b", replacement, out, flags=re.I)
        for original, replacement in sorted(self.names.items(), key=lambda kv: -len(kv[0])):
            out = re.sub(r"\b" + re.escape(original) + r"\b", replacement, out)

        out = PIID_RE.sub(lambda m: self.ref(m.group(0), "piid"), out)
        out = NSN_RE.sub(lambda m: self.ref(m.group(0), "nsn"), out)
        out = PO_RE.sub(lambda m: m.group(0).replace(m.group(1), self.ref(m.group(1), "po")), out)
        out = MONEY_RE.sub("$REDACTED", out)
        out = PHONE_RE.sub("555-555-0100", out)
        return out


WARNING = """
  A HUMAN MUST READ THESE FILES BEFORE COMMITTING THEM.

  Addresses, domains, names, contract and item numbers, money, and phone numbers were
  replaced mechanically. Prose was not. A sentence can identify a person by role, a program
  by description, or a customer by circumstance, and no tool catches that.

  Read every line. Then commit.
"""


def export_thread(cfg: Config, store: Store, thread_key: str, out_dir: Path,
                  prefix: str | None = None) -> list[Path]:
    """Write one thread as anonymized .eml files, oldest first."""
    messages = store.messages_in_thread(thread_key)
    if not messages:
        raise ValueError(f"no messages in thread {thread_key!r}")

    anonymizer = Anonymizer(our_domains=set(cfg.our_domains))
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = prefix or _slug(thread_key)
    written: list[Path] = []

    # First pass builds the substitution map from the envelopes, so body text can be
    # rewritten with addresses the headers already established.
    for message in messages:
        anonymizer.address(message["from_addr"] or "")
        anonymizer.name(message["from_name"] or "", message["from_addr"] or "")
        for address in _addresses(message["to_addrs"]) + _addresses(message["cc_addrs"]):
            anonymizer.address(address)

    for index, message in enumerate(messages, start=1):
        built = EmailMessage()
        sender = anonymizer.address(message["from_addr"] or "")
        display = anonymizer.name(message["from_name"] or "", message["from_addr"] or "")
        built["Message-ID"] = f"<{slug}-{index}@fixture.example>"
        built["From"] = f"{display} <{sender}>" if display else sender
        built["To"] = ", ".join(
            anonymizer.address(a) for a in _addresses(message["to_addrs"])) or sender
        if _addresses(message["cc_addrs"]):
            built["Cc"] = ", ".join(anonymizer.address(a) for a in _addresses(message["cc_addrs"]))
        built["Subject"] = anonymizer.text(message["subject"] or "")
        sent_at = parse_ts(message["sent_at"])
        if sent_at:
            built["Date"] = format_datetime(sent_at)
        if index > 1:
            built["In-Reply-To"] = f"<{slug}-{index - 1}@fixture.example>"
            built["References"] = " ".join(
                f"<{slug}-{i}@fixture.example>" for i in range(1, index))

        body = (
            "(quarantined at ingest; body was never stored)"
            if message["quarantined"] else anonymizer.text(message["body_text"] or "")
        )
        built.set_content(body or "(no text)")

        for name in _attachment_names(message["attachment_names"]):
            suffix = Path(name).suffix or ".bin"
            # Filename only: no payload is ever copied out of the store.
            built.add_attachment(b"fixture placeholder", maintype="application",
                                 subtype="octet-stream",
                                 filename=f"attachment-{index}{suffix}")

        path = out_dir / f"{slug}-{index:02d}.eml"
        path.write_bytes(built.as_bytes())
        written.append(path)

    return written


def _addresses(value: str | None) -> list[str]:
    import json
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return [value]
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _attachment_names(value: str | None) -> list[str]:
    return _addresses(value)


def _slug(thread_key: str) -> str:
    digest = hashlib.sha1(thread_key.encode()).hexdigest()[:8]
    return f"thread-{digest}"
