"""Files on disk: .eml, .mbox, or a directory of either.

This is how the pipeline is developed and tested without touching a live mailbox, and how
`tests/fixtures/mail/` runs in CI. It is also the honest answer for a historical backfill:
export once, ingest once, no credentials involved.
"""

from __future__ import annotations

import datetime as dt
import mailbox
from pathlib import Path
from typing import Any, Iterator

from .base import MailSource, RawMessage


class FileSource:
    name = "file"

    def __init__(self, paths: list[str | Path], mailbox_address: str = "fixtures@local"):
        self.paths = [Path(p) for p in paths]
        self.mailbox_address = mailbox_address

    def targets(self) -> list[str]:
        return [self.mailbox_address]

    def fetch(self, target: str, since: dt.datetime | None) -> Iterator[RawMessage]:
        for path in self.paths:
            if path.is_dir():
                for child in sorted(path.rglob("*")):
                    if child.suffix.lower() in (".eml", ".txt") and child.is_file():
                        yield RawMessage(target, child.read_bytes(), f"file://{child}")
                    elif child.suffix.lower() == ".mbox":
                        yield from self._from_mbox(target, child)
            elif path.suffix.lower() == ".mbox":
                yield from self._from_mbox(target, path)
            elif path.is_file():
                yield RawMessage(target, path.read_bytes(), f"file://{path}")

    @staticmethod
    def _from_mbox(target: str, path: Path) -> Iterator[RawMessage]:
        box = mailbox.mbox(str(path))
        try:
            for index, message in enumerate(box):
                yield RawMessage(target, message.as_bytes(), f"mbox://{path}#{index}")
        finally:
            box.close()

    def healthcheck(self) -> dict[str, Any]:
        missing = [str(p) for p in self.paths if not p.exists()]
        return {"backend": self.name, "ok": not missing, "missing": missing}


_ = MailSource
