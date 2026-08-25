"""Report shape and markdown rendering.

One rule drives the layout: a report that does not say how fresh its data is cannot be
trusted, so freshness is the first thing on the page. The failure mode this guards against
is the mail sync stalling quietly on a Tuesday and every dashboard continuing to look calm.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..config import Config
from ..store import Store, parse_ts


@dataclass
class Report:
    key: str
    title: str
    subtitle: str = ""
    columns: Sequence[str] = ()
    rows: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    generated_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    freshness: list[str] = field(default_factory=list)
    stale: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)


def freshness_lines(cfg: Config, store: Store,
                    now: dt.datetime | None = None) -> tuple[list[str], bool]:
    """Per-target last-sync lines, plus whether anything is stale enough to distrust."""
    now = now or dt.datetime.now(dt.timezone.utc)
    limit = int((cfg.guardrails.get("health", {}) or {}).get("mailbox_stale_after_minutes", 60))
    lines: list[str] = []
    stale = False

    rows = store.last_sync()
    if not rows:
        return ["**No sync has ever run.** Every count below is zero because nothing has "
                "been captured, not because nothing is wrong."], True

    for row in rows:
        last_success = parse_ts(row["last_success"])
        if last_success is None:
            lines.append(f"- `{row['target']}`: **never synced successfully**")
            stale = True
            continue
        age = (now - last_success).total_seconds() / 60
        flag = ""
        if age > limit:
            flag = f" **STALE, {int(age)} min old, limit {limit}**"
            stale = True
        lines.append(f"- `{row['target']}`: last success {last_success.isoformat()}{flag}")
    return lines, stale


def to_markdown(report: Report) -> str:
    out: list[str] = [f"# {report.title}", ""]
    if report.subtitle:
        out += [report.subtitle, ""]
    out += [f"Generated {report.generated_at.isoformat()}", ""]

    if report.stale:
        out += [
            "> **Data freshness warning.** At least one source is stale or has never "
            "synced. Treat every count below as a floor, not a total.",
            "",
        ]
    if report.freshness:
        out += ["## Data freshness", "", *report.freshness, ""]

    if report.metrics:
        out += ["## Summary", ""]
        for key, value in report.metrics.items():
            out.append(f"- **{key}**: {value}")
        out.append("")

    out += [f"## Findings ({report.row_count})", ""]
    if not report.rows:
        out += ["Nothing found.", ""]
    else:
        columns = list(report.columns) or list(report.rows[0])
        out.append("| " + " | ".join(columns) + " |")
        out.append("| " + " | ".join("---" for _ in columns) + " |")
        for row in report.rows:
            cells = [_cell(row.get(c, "")) for c in columns]
            out.append("| " + " | ".join(cells) + " |")
        out.append("")

    if report.notes:
        out += ["## Notes", "", *[f"- {n}" for n in report.notes], ""]
    return "\n".join(out)


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ").strip()


def hours_between(earlier: dt.datetime, later: dt.datetime) -> float:
    return round((later - earlier).total_seconds() / 3600.0, 1)


def median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 1)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 1)
