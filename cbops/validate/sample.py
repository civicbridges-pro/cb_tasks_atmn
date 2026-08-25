"""Measuring whether the findings are true, on your own mail.

A sample drawn only from what a report flagged measures precision and nothing else. It
cannot see a missed promise, and a missed promise is the failure nobody notices. So every
worksheet is stratified: half from what the report flagged, half from comparable items it
did not, and the human labels both without being told which is which.

That makes recall measurable. Because the two strata are sampled at different rates, the
scorer weights them back to population size rather than reporting the raw ratio, which
would flatter recall badly.

The worksheet is a CSV on purpose. It opens in Excel, it can be split across three people,
and it comes back scoreable.
"""

from __future__ import annotations

import csv
import datetime as dt
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..extract import rules
from ..reports.render import Report
from ..store import Store, parse_ts

# One keystroke per row. Anything else and a 40-row worksheet does not get finished.
YES = {"y", "yes", "1", "true", "t"}
NO = {"n", "no", "0", "false", "f"}
UNSURE = {"?", "unsure", "idk", "maybe", ""}

QUESTIONS = {
    "unanswered": "Does this thread still need a reply from us? (y / n / ?)",
    "promises": "Did we commit to doing something specific in this message? (y / n / ?)",
    "handoffs": "Was work passed to a named person here, and did it need a response? (y / n / ?)",
    "vendor-silence": "Are we still waiting on this counterparty for something we need? (y / n / ?)",
}

COLUMNS = [
    "row_id", "report", "stratum", "stratum_population", "system_says", "human_says",
    "human_note", "date", "counterparty", "from", "subject", "evidence", "ref",
]


@dataclass
class Item:
    row_id: str
    report: str
    stratum: str            # "flagged" | "not_flagged"
    stratum_population: int
    system_says: str        # what the report concluded
    date: str = ""
    counterparty: str = ""
    sender: str = ""
    subject: str = ""
    evidence: str = ""      # the text a human needs to judge, quoted from the message
    ref: str = ""

    def as_csv_row(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id, "report": self.report, "stratum": self.stratum,
            "stratum_population": self.stratum_population, "system_says": self.system_says,
            "human_says": "", "human_note": "", "date": self.date,
            "counterparty": self.counterparty, "from": self.sender,
            "subject": self.subject[:120], "evidence": self.evidence[:400], "ref": self.ref,
        }


@dataclass
class Worksheet:
    report: str
    question: str
    items: list[Item] = field(default_factory=list)
    seed: int = 0
    populations: dict[str, int] = field(default_factory=dict)

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            for item in self.items:
                writer.writerow(item.as_csv_row())
        return path


# ---------------------------------------------------------------------------
# Drawing a sample
# ---------------------------------------------------------------------------


def draw(cfg: Config, store: Store, report_name: str, size: int = 40, seed: int = 20260825,
         now: dt.datetime | None = None) -> Worksheet:
    """A stratified worksheet for one report.

    `seed` is explicit so the same sample can be redrawn and handed to a second reviewer.
    Two people labeling the same rows is the only way to know whether the *question* is
    clear, which matters as much as the answers.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if report_name not in QUESTIONS:
        raise ValueError(f"no sampler for {report_name!r}; known: {sorted(QUESTIONS)}")

    flagged, not_flagged = _populations(cfg, store, report_name, now)
    rng = random.Random(seed)

    half = max(1, size // 2)
    picked_flagged = _pick(rng, flagged, half)
    # Spend any unused half on the other stratum rather than shrinking the worksheet.
    picked_other = _pick(rng, not_flagged, size - len(picked_flagged))

    worksheet = Worksheet(
        report=report_name, question=QUESTIONS[report_name], seed=seed,
        populations={"flagged": len(flagged), "not_flagged": len(not_flagged)},
    )
    for index, item in enumerate(picked_flagged + picked_other):
        item.row_id = f"{report_name}-{index + 1:03d}"
        item.stratum_population = worksheet.populations[item.stratum]
        worksheet.items.append(item)

    # Shuffle so a labeler cannot infer the answer from position in the file.
    rng.shuffle(worksheet.items)
    return worksheet


def _pick(rng: random.Random, pool: list[Item], count: int) -> list[Item]:
    if count <= 0 or not pool:
        return []
    if len(pool) <= count:
        return list(pool)
    return rng.sample(pool, count)


def _populations(cfg: Config, store: Store, report_name: str,
                 now: dt.datetime) -> tuple[list[Item], list[Item]]:
    if report_name == "unanswered":
        return _unanswered_population(cfg, store, now)
    if report_name == "promises":
        return _promise_population(cfg, store, now)
    if report_name == "handoffs":
        return _handoff_population(cfg, store, now)
    return _vendor_population(cfg, store, now)


def _unanswered_population(cfg: Config, store: Store,
                           now: dt.datetime) -> tuple[list[Item], list[Item]]:
    """Every external thread old enough to judge, split by whether the report flagged it.

    The unflagged half is where the interesting errors live: a thread the report dismissed
    as an acknowledgment, or one it thinks we answered when we did not.
    """
    threshold = float(cfg.phase0("unanswered_after_hours", 48))
    cutoff = (now - dt.timedelta(hours=threshold)).isoformat()

    flagged: list[Item] = []
    not_flagged: list[Item] = []

    for thread in store.query(
        "SELECT * FROM threads WHERE is_external = 1 AND last_at <= ? ORDER BY last_at",
        (cutoff,),
    ):
        last = store.query(
            "SELECT * FROM messages WHERE thread_key = ? ORDER BY sent_at DESC, id DESC LIMIT 1",
            (thread["thread_key"],),
        )
        if not last:
            continue
        message = last[0]
        body = message["body_text"] or message["snippet"] or ""
        is_inbound = thread["last_direction"] == "inbound"
        closer = rules.looks_like_closer(body) and not rules.contains_ask(body)
        would_flag = is_inbound and not closer

        item = Item(
            row_id="", report="unanswered",
            stratum="flagged" if would_flag else "not_flagged",
            stratum_population=0,
            system_says=(
                "needs a reply" if would_flag
                else "no reply needed: last message reads as an acknowledgment" if closer
                else "no reply needed: we sent the last message"
            ),
            date=(message["sent_at"] or "")[:10],
            counterparty=thread["counterparty"] or "",
            sender=message["from_addr"] or "",
            subject=thread["subject"] or "",
            evidence=body.strip().replace("\n", " ")[:400],
            ref=thread["thread_key"],
        )
        (flagged if would_flag else not_flagged).append(item)
    return flagged, not_flagged


def _promise_population(cfg: Config, store: Store,
                        now: dt.datetime) -> tuple[list[Item], list[Item]]:
    """Outbound messages, split by whether the detector found a commitment.

    This is the stratification that matters most. Sampling only detected promises can never
    reveal the ones the detector walked straight past.
    """
    detected = {
        row["message_id"]: row for row in store.query(
            "SELECT message_id, text, due_text, status FROM promises")
    }
    flagged: list[Item] = []
    not_flagged: list[Item] = []

    for message in store.query(
        "SELECT * FROM messages WHERE direction IN ('outbound','internal') "
        "AND quarantined = 0 AND body_text IS NOT NULL AND TRIM(body_text) <> ''"
    ):
        promise = detected.get(message["id"])
        item = Item(
            row_id="", report="promises",
            stratum="flagged" if promise else "not_flagged", stratum_population=0,
            system_says=(
                f"commitment found ({promise['status']}): {promise['text'][:120]}"
                if promise else "no commitment in this message"
            ),
            date=(message["sent_at"] or "")[:10],
            counterparty=message["counterparty"] or "",
            sender=message["from_addr"] or "",
            subject=message["subject"] or "",
            evidence=(message["body_text"] or "").strip().replace("\n", " ")[:400],
            ref=f"message {message['id']}",
        )
        (flagged if promise else not_flagged).append(item)
    return flagged, not_flagged


def _handoff_population(cfg: Config, store: Store,
                        now: dt.datetime) -> tuple[list[Item], list[Item]]:
    detected = {row["message_id"]: row for row in store.query(
        "SELECT message_id, to_person, text FROM handoffs")}
    flagged: list[Item] = []
    not_flagged: list[Item] = []

    for message in store.query(
        "SELECT * FROM messages WHERE counterparty_class = 'internal' AND quarantined = 0 "
        "AND body_text IS NOT NULL AND TRIM(body_text) <> ''"
    ):
        handoff = detected.get(message["id"])
        item = Item(
            row_id="", report="handoffs",
            stratum="flagged" if handoff else "not_flagged", stratum_population=0,
            system_says=(
                f"work passed to {handoff['to_person']}" if handoff
                else "no handoff in this message"
            ),
            date=(message["sent_at"] or "")[:10],
            counterparty=message["counterparty"] or "",
            sender=message["from_addr"] or "",
            subject=message["subject"] or "",
            evidence=(message["body_text"] or "").strip().replace("\n", " ")[:400],
            ref=f"message {message['id']}",
        )
        (flagged if handoff else not_flagged).append(item)
    return flagged, not_flagged


def _vendor_population(cfg: Config, store: Store,
                       now: dt.datetime) -> tuple[list[Item], list[Item]]:
    default_hours = float(cfg.phase0("vendor_silence_default_hours", 72))
    flagged: list[Item] = []
    not_flagged: list[Item] = []

    for thread in store.query(
        "SELECT * FROM threads WHERE is_external = 1 AND counterparty_class <> 'gov'"
    ):
        last = store.query(
            "SELECT * FROM messages WHERE thread_key = ? ORDER BY sent_at DESC, id DESC LIMIT 1",
            (thread["thread_key"],),
        )
        if not last:
            continue
        message = last[0]
        silent_for = 0.0
        sent_at = parse_ts(message["sent_at"])
        if sent_at:
            silent_for = (now - sent_at).total_seconds() / 3600
        has_deadline = thread["solicitation_close_at"] is not None
        would_flag = (
            thread["last_direction"] == "outbound"
            and (has_deadline or silent_for >= default_hours)
        )
        item = Item(
            row_id="", report="vendor-silence",
            stratum="flagged" if would_flag else "not_flagged", stratum_population=0,
            system_says=(
                f"waiting on them, silent {silent_for:.0f}h" if would_flag
                else "not waiting: they sent the last message" if
                thread["last_direction"] == "inbound"
                else f"not flagged: silent only {silent_for:.0f}h and no deadline known"
            ),
            date=(message["sent_at"] or "")[:10],
            counterparty=thread["counterparty"] or "",
            sender=message["from_addr"] or "",
            subject=thread["subject"] or "",
            evidence=(message["body_text"] or message["snippet"] or "").strip().replace("\n", " ")[:400],
            ref=thread["thread_key"],
        )
        (flagged if would_flag else not_flagged).append(item)
    return flagged, not_flagged


# ---------------------------------------------------------------------------
# Scoring a labeled worksheet
# ---------------------------------------------------------------------------


@dataclass
class Score:
    report: str
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0
    unsure: int = 0
    unlabeled: int = 0
    population_flagged: int = 0
    population_not_flagged: int = 0

    @property
    def precision(self) -> float | None:
        judged = self.true_positive + self.false_positive
        return (self.true_positive / judged) if judged else None

    @property
    def recall(self) -> float | None:
        """Weighted back to population size.

        The two strata are sampled at very different rates, so the raw ratio would flatter
        recall badly: negatives are drawn from a pool many times larger than the positives.
        """
        sampled_flagged = self.true_positive + self.false_positive
        sampled_other = self.false_negative + self.true_negative
        if not sampled_flagged or not sampled_other:
            return None
        found = self.population_flagged * (self.true_positive / sampled_flagged)
        missed = self.population_not_flagged * (self.false_negative / sampled_other)
        return (found / (found + missed)) if (found + missed) else None

    @property
    def f1(self) -> float | None:
        precision, recall = self.precision, self.recall
        if not precision or not recall or (precision + recall) == 0:
            return None
        return 2 * precision * recall / (precision + recall)

    @property
    def estimated_missed(self) -> float | None:
        sampled_other = self.false_negative + self.true_negative
        if not sampled_other:
            return None
        return self.population_not_flagged * (self.false_negative / sampled_other)


def read_labels(path: Path) -> tuple[dict[str, Score], list[dict[str, str]]]:
    """Score a returned worksheet. Also returns every disagreement, which is the point."""
    scores: dict[str, Score] = {}
    disagreements: list[dict[str, str]] = []

    with Path(path).open(newline="") as handle:
        for row in csv.DictReader(handle):
            report = (row.get("report") or "").strip()
            if not report:
                continue
            score = scores.setdefault(report, Score(report=report))
            stratum = (row.get("stratum") or "").strip()
            try:
                population = int(row.get("stratum_population") or 0)
            except ValueError:
                population = 0
            if stratum == "flagged":
                score.population_flagged = max(score.population_flagged, population)
            else:
                score.population_not_flagged = max(score.population_not_flagged, population)

            verdict = (row.get("human_says") or "").strip().lower()
            if verdict in UNSURE:
                if verdict == "":
                    score.unlabeled += 1
                else:
                    score.unsure += 1
                continue

            human_yes = verdict in YES
            if verdict not in YES and verdict not in NO:
                score.unsure += 1
                continue

            if stratum == "flagged":
                if human_yes:
                    score.true_positive += 1
                else:
                    score.false_positive += 1
                    disagreements.append(_disagreement(row, "false positive"))
            else:
                if human_yes:
                    score.false_negative += 1
                    disagreements.append(_disagreement(row, "missed finding"))
                else:
                    score.true_negative += 1
    return scores, disagreements


def _disagreement(row: dict[str, str], kind: str) -> dict[str, str]:
    return {
        "kind": kind, "row_id": row.get("row_id", ""), "report": row.get("report", ""),
        "system_says": (row.get("system_says") or "")[:100],
        "human_note": (row.get("human_note") or "")[:120],
        "subject": (row.get("subject") or "")[:60],
        "ref": row.get("ref", ""),
    }


def score_report(scores: dict[str, Score], disagreements: list[dict[str, str]],
                 now: dt.datetime | None = None) -> Report:
    now = now or dt.datetime.now(dt.timezone.utc)
    report = Report(
        key="score",
        title="Phase 0 accuracy against human labels",
        subtitle=(
            "Precision is measured directly. Recall is estimated by weighting each stratum "
            "back to population size, because the unflagged pool is sampled far more thinly "
            "than the flagged one."
        ),
        columns=["report", "precision", "recall (est)", "f1", "correct", "false positives",
                 "missed", "est. missed in full corpus", "unsure", "unlabeled"],
        generated_at=now,
    )

    for name in sorted(scores):
        score = scores[name]
        report.rows.append({
            "report": name,
            "precision": f"{score.precision:.0%}" if score.precision is not None else "n/a",
            "recall (est)": f"{score.recall:.0%}" if score.recall is not None else "n/a",
            "f1": f"{score.f1:.2f}" if score.f1 is not None else "n/a",
            "correct": score.true_positive + score.true_negative,
            "false positives": score.false_positive,
            "missed": score.false_negative,
            "est. missed in full corpus": (
                f"{score.estimated_missed:.0f}" if score.estimated_missed is not None else "n/a"
            ),
            "unsure": score.unsure,
            "unlabeled": score.unlabeled,
        })

    total_unlabeled = sum(s.unlabeled for s in scores.values())
    report.metrics = {
        "reports scored": len(scores),
        "rows labeled": sum(
            s.true_positive + s.false_positive + s.false_negative + s.true_negative
            for s in scores.values()),
        "rows left blank": total_unlabeled,
        "disagreements to look at": len(disagreements),
    }
    report.notes = [
        "A false positive costs a person two seconds in triage. A missed finding is a broken "
        "promise nobody knew about. Weight them accordingly: recall below 80% on the promise "
        "detector is a bigger problem than precision at 70%.",
        "Every disagreement below is a fixture waiting to be written. `./cb fixture add "
        "<ref>` exports the thread anonymized, and it becomes a regression test so the same "
        "mistake cannot come back.",
        "`unsure` rows are useful data about the question, not noise. If a reviewer cannot "
        "tell, an agent certainly cannot, and that item belongs in human triage by design.",
    ]
    if total_unlabeled:
        report.notes.insert(0,
            f"{total_unlabeled} row(s) came back blank and are excluded from every number "
            "above. Precision and recall on a half-labeled worksheet are not trustworthy.")

    if disagreements:
        report.notes.append("")
        report.notes.append("**Disagreements**")
        for item in disagreements[:40]:
            report.notes.append(
                f"`{item['row_id']}` {item['kind']}: system said "
                f"\"{item['system_says']}\"{' — ' + item['human_note'] if item['human_note'] else ''}"
                f" ({item['ref']})"
            )
    return report
