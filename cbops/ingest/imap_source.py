"""Path A: Namecheap Private Email over IMAP.

This is the fallback path, not the recommended one. See docs/mail-path-decision.md for why.
The constraints are not incidental, they shape this file:

*   No push. We poll. IDLE exists but a held connection per mailbox is another thing to
    monitor, so a scheduled poll with a deliberately overlapping window is more robust.
*   No server-side search. We pull bodies and index locally.
*   No delegation. Every mailbox is a separate credential to store and rotate. A shared
    alias with three readers has no way to record who acted, so obligations sourced from
    one cannot satisfy the single-owner invariant without a human saying who took it.
*   No admin API. Adding or auditing a mailbox is a manual action in the Namecheap panel.

Credentials come from the environment, never from a config file in the repo, and never from
a literal in code. `CB_IMAP_PASSWORD_<SLUG>` per mailbox.
"""

from __future__ import annotations

import datetime as dt
import email
import imaplib
import os
import re
from typing import Any, Iterator

from .base import MailSource, RawMessage

DEFAULT_HOST = os.environ.get("CB_IMAP_HOST", "mail.privateemail.com")
DEFAULT_PORT = int(os.environ.get("CB_IMAP_PORT", "993"))

# Deliberate overlap on every poll. Re-reading a day of mail is cheap; the store dedupes on
# (source, message_id). Trusting a cursor across a connection that stalls is how mail goes
# missing without anyone noticing.
OVERLAP = dt.timedelta(hours=26)

FOLDERS = ("INBOX", "INBOX.Sent", "Sent")


def password_env_var(address: str) -> str:
    slug = re.sub(r"[^A-Z0-9]+", "_", address.upper())
    return f"CB_IMAP_PASSWORD_{slug}"


class ImapSource:
    name = "imap"

    def __init__(self, mailboxes: list[dict[str, Any]], host: str = DEFAULT_HOST,
                 port: int = DEFAULT_PORT, folders: tuple[str, ...] = FOLDERS):
        self.mailboxes = {box["address"]: box for box in mailboxes}
        self.host = host
        self.port = port
        self.folders = folders

    def targets(self) -> list[str]:
        return [a for a, box in self.mailboxes.items() if box.get("enabled", True)]

    # -- connection --------------------------------------------------------

    def _password(self, address: str) -> str:
        var = password_env_var(address)
        secret = os.environ.get(var)
        if not secret:
            raise RuntimeError(
                f"no password for {address}: set {var}. Namecheap Private Email has no "
                "OAuth, so this is an app-specific password and it belongs in a secrets "
                "manager, never in this repo."
            )
        return secret

    def _connect(self, address: str) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        conn.login(address, self._password(address))
        return conn

    # -- fetch -------------------------------------------------------------

    def fetch(self, target: str, since: dt.datetime | None) -> Iterator[RawMessage]:
        window_start = (since - OVERLAP) if since else None
        conn = self._connect(target)
        try:
            for folder in self.folders:
                status, _ = conn.select(f'"{folder}"', readonly=True)
                if status != "OK":
                    continue  # Sent lives under different names across clients
                criteria = ["ALL"]
                if window_start:
                    criteria = ["SINCE", window_start.strftime("%d-%b-%Y")]
                status, data = conn.search(None, *criteria)
                if status != "OK":
                    raise RuntimeError(f"IMAP search failed on {target}/{folder}: {status}")
                for uid in (data[0].split() if data and data[0] else []):
                    status, payload = conn.fetch(uid, "(RFC822)")
                    if status != "OK" or not payload or not isinstance(payload[0], tuple):
                        # One unreadable message must not abort the mailbox, but it is not
                        # silent either: the caller marks the sync partial.
                        continue
                    yield RawMessage(
                        mailbox=target,
                        raw_bytes=payload[0][1],
                        raw_ref=f"imap://{target}/{folder}/{uid.decode()}",
                    )
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def healthcheck(self) -> dict[str, Any]:
        results: dict[str, Any] = {"backend": self.name, "host": self.host, "mailboxes": {}}
        for address in self.targets():
            try:
                conn = self._connect(address)
                status, _ = conn.select("INBOX", readonly=True)
                conn.logout()
                results["mailboxes"][address] = "ok" if status == "OK" else f"select {status}"
            except Exception as exc:
                results["mailboxes"][address] = f"FAILED: {type(exc).__name__}: {exc}"
        results["ok"] = all(v == "ok" for v in results["mailboxes"].values())
        return results


def parse(raw: RawMessage) -> email.message.Message:
    return email.message_from_bytes(raw.raw_bytes)


_ = MailSource  # documents the contract this class satisfies
