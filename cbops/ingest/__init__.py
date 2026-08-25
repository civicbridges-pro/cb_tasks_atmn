"""Capture layer, L1.

Every source produces the same normalized message shape, so the mail path decision is
reversible: swapping Namecheap IMAP for the Google Workspace API replaces one module here
and touches nothing downstream. See docs/mail-path-decision.md.
"""

from .base import MailSource, SyncResult

__all__ = ["MailSource", "SyncResult", "get_source"]


def get_source(kind: str, **kwargs: object) -> MailSource:
    """Resolve a source by name. The one place that knows which backends exist."""
    if kind == "imap":
        from .imap_source import ImapSource
        return ImapSource(**kwargs)          # type: ignore[arg-type]
    if kind == "gmail":
        from .gmail_source import GmailSource
        return GmailSource(**kwargs)         # type: ignore[arg-type]
    if kind in ("mbox", "maildir", "eml"):
        from .mbox_source import FileSource
        return FileSource(**kwargs)          # type: ignore[arg-type]
    raise ValueError(f"unknown mail source: {kind!r}")
