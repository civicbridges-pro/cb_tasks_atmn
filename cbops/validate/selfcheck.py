"""Data-quality checks over whatever has been captured. No human labels required.

The failure this file exists to catch: a report that is empty or clean because its input was
missing, not because nothing is wrong. "Zero broken promises" reads like good news and is
indistinguishable from "the Sent folder was never synced", and only one of those is worth
celebrating.

Every check states a threshold, what the number means, and what to do about it. A check that
reports a number without saying what a bad number looks like is a check nobody acts on.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..reports.render import Report, freshness_lines
from ..store import Store, parse_ts

# Below this share of outbound mail, the Broken Promise report is structurally blind rather
# than merely quiet. Real mixed mailboxes run far higher.
MIN_OUTBOUND_SHARE = 0.10

# A real corpus threads. If nearly every thread is a single message, either the Sent folder
# is missing or References are being dropped and reconciliation is not catching it.
MAX_SINGLETON_THREAD_SHARE = 0.85

# Quarantine is meant to be broad, not universal.
MAX_QUARANTINE_SHARE = 0.15

# Promises per 100 outbound messages. Zero means the detector never fires on how this team
# actually writes; a very high rate means it is firing on courtesies.
MIN_PROMISE_YIELD = 0.5
MAX_PROMISE_YIELD = 40.0


@dataclass
class Check:
    name: str
    status: str          # "ok" | "warn" | "fail" | "info"
    value: str
    means: str
    action: str = ""

    def as_row(self) -> dict[str, Any]:
        return {"status": self.status.upper(), "check": self.name, "value": self.value,
                "what it means": self.means, "what to do": self.action}


@dataclass
class Context:
    cfg: Config
    store: Store
    now: dt.datetime
    total: int = 0
    counts: dict[str, int] = field(default_factory=dict)


def _share(part: int, whole: int) -> float:
    return (part / whole) if whole else 0.0


def _pct(part: int, whole: int) -> str:
    return f"{part} of {whole} ({100 * _share(part, whole):.0f}%)" if whole else f"{part} (no data)"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_anything_captured(ctx: Context) -> list[Check]:
    if ctx.total:
        first = ctx.store.query("SELECT MIN(sent_at) AS a, MAX(sent_at) AS b FROM messages")[0]
        span = "unknown"
        start, end = parse_ts(first["a"]), parse_ts(first["b"])
        if start and end:
            span = f"{start.date()} to {end.date()} ({(end - start).days} days)"
        return [Check("capture", "ok", f"{ctx.total} messages, {span}",
                      "the store has mail to reason about")]
    return [Check(
        "capture", "fail", "0 messages",
        "every report will be empty, and empty will look like good news",
        "run `./cb ingest` before reading any report",
    )]


def check_outbound_present(ctx: Context) -> list[Check]:
    """The single most important check in this file.

    Without Sent mail there are no commitments to audit, so the Broken Promise report, the
    most valuable of the four, returns nothing and looks healthy doing it.
    """
    outbound = ctx.counts.get("outbound", 0)
    share = _share(outbound, ctx.total)
    if not ctx.total:
        return []
    if outbound == 0:
        return [Check(
            "outbound captured", "fail", "0 outbound messages",
            "the Sent folder was not captured. The Broken Promise report cannot find a "
            "single commitment, and it will report zero rather than report that it is blind",
            "add the Sent folder to the ingest. On IMAP it is named 'Sent', 'INBOX.Sent', or "
            "'Sent Items' depending on the client that created it",
        )]
    if share < MIN_OUTBOUND_SHARE:
        return [Check(
            "outbound captured", "warn", _pct(outbound, ctx.total),
            f"below {MIN_OUTBOUND_SHARE:.0%} outbound suggests the Sent folder is only "
            "partially synced, so promise coverage is patchy in a way the report cannot see",
            "check that every mailbox's Sent folder is included, not just the first one",
        )]
    return [Check("outbound captured", "ok", _pct(outbound, ctx.total),
                  "there is sent mail to audit for commitments")]


def check_internal_present(ctx: Context) -> list[Check]:
    internal = ctx.counts.get("internal", 0)
    if not ctx.total:
        return []
    if internal == 0:
        return [Check(
            "internal mail captured", "warn", "0 internal messages",
            "the Dropped Handoff report has nothing to work with. Either internal work "
            "happens somewhere other than mail, which is worth knowing, or those mailboxes "
            "were not included",
            "if handoffs happen in Telegram, ingest an export and compare the counts. That "
            "answers open question 3 with data instead of opinion",
        )]
    return [Check("internal mail captured", "ok", _pct(internal, ctx.total),
                  "internal handoffs are visible")]


def check_threading(ctx: Context) -> list[Check]:
    """A split thread makes each half look unanswered, so this inflates the headline count."""
    threads = ctx.store.query("SELECT COUNT(*) AS n FROM threads")[0]["n"]
    if not threads:
        return []
    singletons = ctx.store.query(
        "SELECT COUNT(*) AS n FROM threads WHERE message_count = 1")[0]["n"]
    share = _share(singletons, threads)
    orphan_ids = ctx.store.query(
        "SELECT COUNT(*) AS n FROM messages WHERE message_id IS NULL")[0]["n"]

    checks = []
    if share > MAX_SINGLETON_THREAD_SHARE:
        checks.append(Check(
            "threading", "fail", f"{_pct(singletons, threads)} threads are a single message",
            "conversations are being split, and each half looks unanswered. The Unanswered "
            "Thread count is inflated and the Broken Promise report cannot see a "
            "follow-through that landed in the other half",
            "confirm the Sent folder is captured. If it is, a client is dropping References: "
            "check whether `reconcile_threads` merged anything on the last ingest",
        ))
    else:
        checks.append(Check("threading", "ok",
                            f"{_pct(singletons, threads)} threads are a single message",
                            "conversations are grouping"))
    if orphan_ids:
        checks.append(Check(
            "message ids", "warn", _pct(orphan_ids, ctx.total),
            "messages with no Message-ID fall back to subject-and-participant threading, "
            "which is weaker",
            "usually a client quirk and tolerable. Worth checking these are not all from one "
            "mailbox",
        ))
    return checks


def check_dates(ctx: Context) -> list[Check]:
    """An unparsed date sorts to 1970 and silently drops out of every aged report."""
    undated = ctx.store.query(
        "SELECT COUNT(*) AS n FROM messages WHERE sent_at LIKE '1970-%'")[0]["n"]
    if not ctx.total:
        return []
    if undated:
        return [Check(
            "date parsing", "fail" if _share(undated, ctx.total) > 0.02 else "warn",
            _pct(undated, ctx.total),
            "these messages have an unreadable Date header. They sort to 1970 and never age "
            "into any report, so they are invisible rather than wrong",
            "check the source. A whole mailbox landing here usually means an export wrote "
            "headers the parser does not recognize",
        )]
    return [Check("date parsing", "ok", "every message has a readable date",
                  "aged reports are working from real timestamps")]


def check_bodies(ctx: Context) -> list[Check]:
    """No body means no commitment, no ask, and no reference can be extracted."""
    if not ctx.total:
        return []
    empty = ctx.store.query(
        "SELECT COUNT(*) AS n FROM messages WHERE quarantined = 0 "
        "AND (body_text IS NULL OR TRIM(body_text) = '')")[0]["n"]
    share = _share(empty, ctx.total)
    if share > 0.15:
        return [Check(
            "body extraction", "fail", _pct(empty, ctx.total),
            "these messages yielded no text, so no promise, ask, or contract reference could "
            "be extracted from them. They are counted but not understood",
            "usually HTML-only mail with an unusual encoding, or an export that dropped "
            "bodies. Check a few by hand before trusting the extraction counts",
        )]
    if empty:
        return [Check("body extraction", "warn", _pct(empty, ctx.total),
                      "a small number of messages yielded no text",
                      "normal for calendar invites and bare-attachment mail")]
    return [Check("body extraction", "ok", "every message yielded text",
                  "extraction had something to work with")]


def check_counterparties(ctx: Context) -> list[Check]:
    """The Unanswered report sorts by importance, so unknown classes flatten the ranking."""
    if not ctx.total:
        return []
    rows = ctx.store.query(
        "SELECT counterparty_class AS c, COUNT(*) AS n FROM messages GROUP BY 1 ORDER BY n DESC")
    breakdown = ", ".join(f"{row['c'] or 'null'}={row['n']}" for row in rows)
    unknown = sum(row["n"] for row in rows if row["c"] in (None, "unknown"))
    share = _share(unknown, ctx.total)

    checks = [Check("counterparty mix", "info", breakdown,
                    "how the corpus splits by counterparty class")]
    if share > 0.40:
        checks.append(Check(
            "counterparty classification", "warn", _pct(unknown, ctx.total),
            "most counterparties are unclassified, so the Unanswered report cannot rank by "
            "importance and everything sorts as if equally urgent. Government still ranks "
            "correctly because it is matched by domain suffix",
            "populate the oem, distributor, and customer domain lists in "
            "config/counterparties.yaml from Zoho. This is the highest-leverage config "
            "change available during Phase 0",
        ))
    gov = sum(row["n"] for row in rows if row["c"] == "gov")
    if gov == 0:
        checks.append(Check(
            "government mail", "warn", "0 messages classified gov",
            "for a company with federal contracts this is surprising. Either the .mil and "
            ".gov traffic is in a mailbox that was not captured, or it arrives via a portal",
            "confirm which mailbox receives contracting officer mail and include it",
        ))
    return checks


def check_quarantine(ctx: Context) -> list[Check]:
    if not ctx.total:
        return []
    quarantined = ctx.counts.get("quarantined", 0)
    share = _share(quarantined, ctx.total)
    if share > MAX_QUARANTINE_SHARE:
        return [Check(
            "quarantine rate", "warn", _pct(quarantined, ctx.total),
            "a large share of mail is being held back from extraction. The envelopes still "
            "count in the Unanswered report, but nothing in them can be read",
            "review guardrails.classification. If a routine footer contains a marker, the "
            "whole corpus quarantines. Do not loosen the CUI markers without a decision on "
            "open question 6",
        )]
    return [Check("quarantine rate", "ok", _pct(quarantined, ctx.total),
                  "classification is holding back a plausible share of mail")]


def check_noise_filter(ctx: Context) -> list[Check]:
    """Auto-replies inflate every count. Any that got through means the filter has a hole."""
    leaked = ctx.store.query(
        "SELECT COUNT(*) AS n FROM messages WHERE "
        "LOWER(subject) LIKE 'automatic reply%' OR LOWER(subject) LIKE 'out of office%' "
        "OR LOWER(subject) LIKE 'undeliverable%' OR from_addr LIKE 'noreply@%'")[0]["n"]
    if leaked:
        return [Check(
            "noise filter", "warn", f"{leaked} auto-reply or bounce message(s) stored",
            "these inflate thread counts and can make a thread look answered when a robot "
            "answered it",
            "add the pattern to ignore_senders or ignore_subject_patterns in "
            "config/counterparties.yaml, then re-ingest",
        )]
    return [Check("noise filter", "ok", "no auto-replies or bounces stored",
                  "the counts are not padded by robots")]


def check_detector_yield(ctx: Context) -> list[Check]:
    """Both a silent detector and a trigger-happy one produce a useless report."""
    outbound = ctx.counts.get("outbound", 0)
    promises = ctx.store.query("SELECT COUNT(*) AS n FROM promises")[0]["n"]
    checks: list[Check] = []

    if outbound:
        yield_rate = 100 * promises / outbound
        value = f"{promises} promises from {outbound} outbound ({yield_rate:.1f} per 100)"
        if yield_rate < MIN_PROMISE_YIELD:
            checks.append(Check(
                "promise detector", "fail", value,
                "the detector is not firing on how this team actually writes. The Broken "
                "Promise report will be near empty and that will look like good news",
                "read ten sent messages and find the commitment phrasing they use, then add "
                "it to COMMITMENT_PATTERNS in cbops/extract/rules.py and a fixture for each",
            ))
        elif yield_rate > MAX_PROMISE_YIELD:
            checks.append(Check(
                "promise detector", "warn", value,
                "the detector is firing on more than a third of sent mail, which usually "
                "means it is catching courtesies rather than commitments",
                "spot-check twenty findings with `./cb sample --report promises`. Tighten the "
                "patterns that produced the false ones",
            ))
        else:
            checks.append(Check("promise detector", "ok", value,
                                "the detector is firing at a plausible rate"))

    inbound = ctx.counts.get("inbound", 0)
    if inbound:
        undated = ctx.store.query(
            "SELECT COUNT(*) AS n FROM promises WHERE due_at IS NULL")[0]["n"]
        if promises:
            checks.append(Check(
                "undated commitments", "info", _pct(undated, promises),
                "commitments made with no date. This is a real finding about how the company "
                "communicates, not a detector problem: an undated promise cannot be tracked",
            ))

    handoffs = ctx.store.query("SELECT COUNT(*) AS n FROM handoffs")[0]["n"]
    internal = ctx.counts.get("internal", 0)
    if internal and handoffs == 0:
        checks.append(Check(
            "handoff detector", "warn", f"0 handoffs from {internal} internal messages",
            "internal mail was captured but no work-passing language was recognized",
            "check that the first names in config/people.yaml match how the team addresses "
            "each other in mail",
        ))
    return checks


def check_per_mailbox(ctx: Context) -> list[Check]:
    """A mailbox far quieter than its peers is usually half synced, not quiet."""
    rows = ctx.store.query(
        "SELECT mailbox, COUNT(*) AS n, MIN(sent_at) AS first, MAX(sent_at) AS last "
        "FROM messages WHERE mailbox IS NOT NULL GROUP BY mailbox ORDER BY n DESC")
    if len(rows) < 2:
        return [Check("mailbox coverage", "info",
                      f"{len(rows)} mailbox(es) captured",
                      "a single mailbox is a partial view of the company")]
    checks = [Check("mailbox coverage", "info",
                    ", ".join(f"{row['mailbox']}={row['n']}" for row in rows),
                    "message count per mailbox")]
    biggest = rows[0]["n"]
    thin = [row["mailbox"] for row in rows if row["n"] < biggest * 0.05]
    if thin:
        checks.append(Check(
            "thin mailboxes", "warn", ", ".join(thin),
            "these hold under 5% of the busiest mailbox's volume, which usually means a "
            "partial sync rather than a quiet inbox",
            "check the folder list and the date window for these mailboxes",
        ))
    return checks


def check_coverage_gaps(ctx: Context) -> list[Check]:
    """A run of business days with no mail at all is a sync hole, not a quiet week."""
    rows = ctx.store.query(
        "SELECT DATE(sent_at) AS day, COUNT(*) AS n FROM messages "
        "WHERE sent_at NOT LIKE '1970-%' GROUP BY 1 ORDER BY 1")
    if len(rows) < 7:
        return []
    days = {row["day"]: row["n"] for row in rows}
    start = dt.date.fromisoformat(rows[0]["day"])
    end = dt.date.fromisoformat(rows[-1]["day"])

    gaps: list[str] = []
    run: list[dt.date] = []
    day = start
    while day <= end:
        if day.weekday() < 5 and days.get(day.isoformat(), 0) == 0:
            run.append(day)
        else:
            if len(run) >= 3:
                gaps.append(f"{run[0]} to {run[-1]}")
            run = []
        day += dt.timedelta(days=1)
    if len(run) >= 3:
        gaps.append(f"{run[0]} to {run[-1]}")

    if gaps:
        return [Check(
            "capture gaps", "warn", "; ".join(gaps[:5]),
            "three or more consecutive business days with no mail at all. Companies do not "
            "go that quiet, so this is a sync hole",
            "re-ingest those windows with `--since`. The store dedupes, so overlapping is free",
        )]
    return [Check("capture gaps", "ok", "no multi-day business gaps",
                  "capture looks continuous across the window")]


CHECKS: list[Callable[[Context], list[Check]]] = [
    check_anything_captured,
    check_outbound_present,
    check_internal_present,
    check_threading,
    check_dates,
    check_bodies,
    check_counterparties,
    check_quarantine,
    check_noise_filter,
    check_detector_yield,
    check_per_mailbox,
    check_coverage_gaps,
]

STATUS_ORDER = {"fail": 0, "warn": 1, "ok": 2, "info": 3}


def run(cfg: Config, store: Store, now: dt.datetime | None = None) -> Report:
    """Every data-quality check, worst first."""
    now = now or dt.datetime.now(dt.timezone.utc)
    lines, stale = freshness_lines(cfg, store, now)

    total = store.query("SELECT COUNT(*) AS n FROM messages")[0]["n"]
    counts = {
        row["direction"]: row["n"]
        for row in store.query(
            "SELECT direction, COUNT(*) AS n FROM messages GROUP BY direction")
    }
    counts["quarantined"] = store.query(
        "SELECT COUNT(*) AS n FROM messages WHERE quarantined = 1")[0]["n"]

    ctx = Context(cfg=cfg, store=store, now=now, total=total, counts=counts)

    report = Report(
        key="selfcheck",
        title="Phase 0 self-check",
        subtitle=(
            "Whether the pipeline handled this mail correctly. No human labels needed. This "
            "does not measure whether the findings are true: for that, run `./cb sample`."
        ),
        columns=["status", "check", "value", "what it means", "what to do"],
        freshness=lines, stale=stale, generated_at=now,
    )

    for check in CHECKS:
        try:
            found = check(ctx)
        except Exception as exc:
            found = [Check(check.__name__, "fail", f"{type(exc).__name__}: {exc}",
                           "this check could not run", "report it as a bug")]
        report.rows.extend(item.as_row() for item in found)

    report.rows.sort(key=lambda row: STATUS_ORDER.get(str(row["status"]).lower(), 4))

    failures = sum(1 for row in report.rows if row["status"] == "FAIL")
    warnings = sum(1 for row in report.rows if row["status"] == "WARN")
    report.metrics = {
        "failures": failures,
        "warnings": warnings,
        "messages": total,
        "verdict": (
            "NOT READY, the reports would mislead" if failures
            else "usable, with caveats above" if warnings
            else "clean"
        ),
    }
    report.notes = [
        "A FAIL means a report would be confidently wrong rather than merely imprecise. "
        "Fix those before circulating anything.",
        "The check that matters most is `outbound captured`. Without Sent mail the Broken "
        "Promise report finds nothing and looks healthy doing it, which is the worst possible "
        "outcome for the most valuable of the four reports.",
        "This file measures plumbing. Whether a finding is *true* needs a human verdict: "
        "`./cb sample --report unanswered` draws a worksheet, `./cb score` grades it.",
    ]
    return report
