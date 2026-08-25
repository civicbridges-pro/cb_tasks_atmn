"""Judgment layer: headless Claude Code invocations over the rules layer's candidates.

Scheduled runs are headless by design. Nobody sits in a session, so this shells out to
`claude -p` with a prompt file from prompts/ and requires JSON back.

Three properties this module guarantees, because guardrails depend on them:

*   **It never invents a verdict.** If the CLI is missing, times out, or returns unusable
    output, the result is `available=False` and the caller falls back to the rules layer.
    A silent default of "no obligation here" would be the worst possible failure.
*   **Quarantined content never reaches a prompt.** Guardrail 5 is enforced at ingest, and
    re-checked here, because a prompt is an egress path.
*   **Prompt and output are logged.** Guardrail 6. If this is audited, the log is the
    defense.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT, Config

PROMPT_DIR = REPO_ROOT / "prompts"
DEFAULT_TIMEOUT = int(os.environ.get("CB_CLAUDE_TIMEOUT", "120"))
CLAUDE_BIN = os.environ.get("CB_CLAUDE_BIN", "claude")


@dataclass
class ExtractionResult:
    available: bool
    payload: dict[str, Any]
    raw: str = ""
    error: str = ""

    @property
    def confidence(self) -> float:
        try:
            return float(self.payload.get("confidence", 0.0))
        except (TypeError, ValueError):
            return 0.0


def cli_available() -> bool:
    return shutil.which(CLAUDE_BIN) is not None


def load_prompt(name: str) -> str:
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"missing prompt file: {path}")
    return path.read_text()


def _extract_json(text: str) -> dict[str, Any] | None:
    """Find the JSON object in model output.

    Tolerant on purpose: a fenced block, a bare object, or an object with prose around it
    all parse. Intolerant about the result being a dict, because a list or a string here
    means the prompt drifted and the caller must not treat it as a verdict.
    """
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def run(prompt_name: str, context: dict[str, Any], *, timeout: int = DEFAULT_TIMEOUT,
        store: Any = None, actor: str = "extractor") -> ExtractionResult:
    """One headless extraction. Never raises on model or CLI failure."""
    if not cli_available():
        return ExtractionResult(False, {}, error=f"{CLAUDE_BIN} not on PATH")

    prompt = load_prompt(prompt_name) + "\n\n## Input\n\n```json\n" + json.dumps(
        context, indent=2, default=str
    ) + "\n```\n"

    try:
        completed = subprocess.run(
            [CLAUDE_BIN, "-p", prompt],
            capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT),
        )
    except subprocess.TimeoutExpired:
        result = ExtractionResult(False, {}, error=f"timeout after {timeout}s")
    except OSError as exc:
        result = ExtractionResult(False, {}, error=f"cli error: {exc}")
    else:
        raw = completed.stdout.strip()
        if completed.returncode != 0:
            result = ExtractionResult(
                False, {}, raw=raw,
                error=f"exit {completed.returncode}: {completed.stderr.strip()[:400]}",
            )
        else:
            parsed = _extract_json(raw)
            result = (
                ExtractionResult(True, parsed, raw=raw)
                if parsed is not None
                else ExtractionResult(False, {}, raw=raw, error="no JSON object in output")
            )

    if store is not None:
        store.log_action(
            actor=actor, action=f"extract:{prompt_name}",
            entity="message", entity_id=str(context.get("message_id", "")),
            prompt=prompt, output=result.raw or result.error,
            detail={"available": result.available, "error": result.error},
        )
    return result


def message_context(cfg: Config, message: Any, rules_summary: dict[str, Any]) -> dict[str, Any]:
    """Build the model's input for one message.

    Sends the rules layer's candidates along with the text so the model is grading
    findings rather than starting cold, and so its output can be compared against a
    deterministic baseline.
    """
    if message["quarantined"]:
        raise ValueError(
            "refusing to build a prompt from a quarantined message; "
            "guardrail 5 blocks CUI and export-controlled content from leaving the store"
        )
    return {
        "message_id": message["id"],
        "direction": message["direction"],
        "sent_at": message["sent_at"],
        "subject": message["subject"],
        "from": message["from_addr"],
        "to": message["to_addrs"],
        "counterparty_class": message["counterparty_class"],
        "body": (message["body_text"] or "")[:6000],
        "rules_candidates": rules_summary,
        "known_people": sorted(cfg.person_ids),
        "obligation_types": sorted(
            {r.get("type") for r in cfg.routing_rules if r.get("type")}
        ),
    }
