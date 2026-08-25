"""The interface every capture backend implements.

This exists for one reason. Namecheap Private Email gives us IMAP with an app password and
nothing else: no push, no server-side search, no delegation, no admin API, no retention or
legal hold. A migration to Google Workspace is the recommended path, and it must cost one
module, not a rewrite. So nothing above this line knows what a mailbox is made of.

Every backend must honor three things:

*   **Idempotency.** Re-fetching an overlapping window must not duplicate messages. The
    store enforces this on (source, message_id), and backends should overlap deliberately
    rather than trusting a cursor.
*   **Honest health reporting.** A sync that fetched nothing because the connection stalled
    and a sync that fetched nothing because there was no new mail are different events. A
    quietly stalled mailbox is failure mode number one for this whole system.
*   **Classify before store.** Backends hand raw messages to `cbops.normalize`, which
    quarantines CUI and export-controlled content before the body is written anywhere.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol


@dataclass
class RawMessage:
    """One message as the transport delivered it, plus where it came from."""

    mailbox: str
    raw_bytes: bytes
    raw_ref: str | None = None


@dataclass
class SyncResult:
    target: str
    seen: int = 0
    new: int = 0
    quarantined: int = 0
    skipped: int = 0
    ok: bool = True
    error: str | None = None
    # A backend that cannot tell whether it saw everything says so here. The dashboards
    # surface it, because "0 new messages" and "we could not tell" must never look alike.
    partial: bool = False
    finished_at: dt.datetime | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        state = "ok" if self.ok else f"FAILED: {self.error}"
        flags = " (partial)" if self.partial else ""
        return (
            f"{self.target}: {self.new} new / {self.seen} seen, "
            f"{self.quarantined} quarantined, {self.skipped} skipped [{state}]{flags}"
        )


class MailSource(Protocol):
    """A capture backend."""

    name: str

    def targets(self) -> list[str]:
        """Mailboxes or channels this backend will read."""

    def fetch(self, target: str, since: dt.datetime | None) -> Iterator[RawMessage]:
        """Yield raw messages for one target.

        Implementations should overlap the requested window rather than trusting a stored
        cursor, and must raise rather than yield nothing when the connection fails. A
        backend that swallows a connection error and returns an empty iterator turns a
        loud failure into a silent one, which is exactly the outcome this design exists to
        prevent.
        """

    def healthcheck(self) -> dict[str, Any]:
        """Whether this backend can reach its transport right now."""
