"""Telegram capture from a Desktop JSON export.

Open question 3 in the brief is whether Telegram carries real commitments or mostly
chatter. That question is answerable with data rather than opinion, and this module is how:
ingest an export, run the same promise and handoff detectors used on mail, and read the
counts. If commitments live here, Telegram has to be ingested continuously. If they do not,
leaving it out simplifies the build considerably.

An export, not a bot: a bot added to a group cannot read history, and history is exactly
what Phase 0 needs. Continuous capture is a Phase 1 decision that depends on the answer.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from ..config import Config


def _text_of(entry: dict[str, Any]) -> str:
    """Telegram text is a string, or a list of strings and entity dicts."""
    raw = entry.get("text", "")
    if isinstance(raw, str):
        return raw
    parts: list[str] = []
    for piece in raw if isinstance(raw, list) else []:
        if isinstance(piece, str):
            parts.append(piece)
        elif isinstance(piece, dict):
            parts.append(str(piece.get("text", "")))
    return "".join(parts)


def _sent_at(entry: dict[str, Any]) -> str:
    unix = entry.get("date_unixtime")
    if unix:
        return dt.datetime.fromtimestamp(int(unix), dt.timezone.utc).isoformat()
    raw = str(entry.get("date", ""))
    if raw:
        try:
            return dt.datetime.fromisoformat(raw).replace(tzinfo=dt.timezone.utc).isoformat()
        except ValueError:
            pass
    return dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc).isoformat()


def normalize_export(cfg: Config, path: Path,
                     identity_map: dict[str, str] | None = None) -> Iterator[dict[str, Any]]:
    """Yield normalized messages from a Telegram Desktop export.

    `identity_map` maps a Telegram display name or user id to a person id in people.yaml.
    Without it, a Telegram message has no owner the ledger can use, which is the same
    delegation gap a shared mailbox has. Unmapped senders are kept and marked, never
    dropped, so the volume shows up in the Phase 0 counts even when attribution does not.
    """
    identity = {k.lower(): v for k, v in (identity_map or {}).items()}
    data = json.loads(path.read_text())
    chats = data.get("chats", {}).get("list") if "chats" in data else [data]

    for chat in chats or []:
        chat_name = str(chat.get("name") or chat.get("id") or "unknown")
        thread_key = "tg:" + hashlib.sha1(chat_name.encode()).hexdigest()[:16]
        for entry in chat.get("messages", []) or []:
            if entry.get("type") != "message":
                continue
            text = _text_of(entry).strip()
            if not text:
                continue
            sender = str(entry.get("from") or entry.get("actor") or "unknown")
            person = identity.get(sender.lower())
            attachments = [
                str(entry[key]) for key in ("file", "photo", "media_type") if entry.get(key)
            ]
            yield {
                "source": "telegram",
                "mailbox": f"telegram:{chat_name}",
                "message_id": f"tg:{chat.get('id')}:{entry.get('id')}",
                "thread_key": thread_key,
                "in_reply_to": (
                    f"tg:{chat.get('id')}:{entry['reply_to_message_id']}"
                    if entry.get("reply_to_message_id") else None
                ),
                "refs": None,
                "from_addr": f"{person or sender}@telegram.local".lower().replace(" ", "."),
                "from_name": sender,
                "to_addrs": [],
                "cc_addrs": [],
                "subject": f"Telegram: {chat_name}",
                "sent_at": _sent_at(entry),
                # Telegram groups are internal in practice. A vendor in a group chat is an
                # exception a human must map, not something to infer from a display name.
                "direction": "internal",
                "body_text": text,
                "snippet": text[:400],
                "has_attachments": 1 if attachments else 0,
                "attachment_names": attachments,
                "counterparty": chat_name,
                "counterparty_class": "internal",
                "contract_ref": None,
                "quarantined": 0,
                "quarantine_reason": None,
                "raw_ref": f"telegram://{chat.get('id')}/{entry.get('id')}",
                "unmapped_sender": None if person else sender,
            }
