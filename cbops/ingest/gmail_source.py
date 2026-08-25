"""Path B: Google Workspace.

Not implemented, on purpose, and this file is not a placeholder for its own sake: it fixes
the seam so the migration is a one-module change rather than a pipeline rewrite.

If Path B is chosen (the recommendation, see docs/mail-path-decision.md), most of this file
never gets written. Claude Code reaches Gmail through the existing connector, so the
capture layer largely disappears rather than being ported. What survives is this interface
and everything above it.

What Path B buys, in the order it matters here:

1.  Push notifications, so the ledger is never stale between polls.
2.  Real server-side search, so we stop maintaining a local index.
3.  Shared and delegated mailboxes, which is what makes the single-owner invariant
    enforceable on a shared alias. On Path A it is not enforceable at all.
4.  Retention, legal hold, and admin audit logs. For 70+ federal contracts this is the gap
    that matters most, and it is not a convenience issue.
5.  SPF, DKIM, and DMARC control, so outbound in Phase 2 actually lands.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterator

from .base import MailSource, RawMessage

NOT_IMPLEMENTED = (
    "Gmail capture is not implemented. The mail path decision is still open, see "
    "docs/mail-path-decision.md. If Path B is chosen, prefer the existing Gmail connector "
    "over writing an API client here."
)


class GmailSource:
    name = "gmail"

    def __init__(self, mailboxes: list[dict[str, Any]] | None = None, **_: object):
        self.mailboxes = mailboxes or []

    def targets(self) -> list[str]:
        return [box["address"] for box in self.mailboxes if box.get("enabled", True)]

    def fetch(self, target: str, since: dt.datetime | None) -> Iterator[RawMessage]:
        raise NotImplementedError(NOT_IMPLEMENTED)
        yield  # pragma: no cover - keeps the generator signature honest

    def healthcheck(self) -> dict[str, Any]:
        return {"backend": self.name, "ok": False, "error": NOT_IMPLEMENTED}


_ = MailSource
