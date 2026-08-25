"""Command line entry point.

Scheduled runs are headless: cron calls `./cb` with a subcommand, or `claude -p` with a
prompt file. Nobody sits in a session, so every command exits non-zero on failure and says
what broke on stderr.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

from . import config as config_mod
from . import pipeline
from .config import REPO_ROOT, Config, ConfigError
from .ingest import get_source
from .ingest import telegram as telegram_ingest
from .ingest import portals
from .reports import PHASE0_REPORTS
from .reports.render import to_markdown
from .store import DEFAULT_DB, Store

REPORT_DIR = REPO_ROOT / "var" / "reports"


def _load_config(strict: bool = True) -> Config:
    return config_mod.load() if strict else config_mod.load_unvalidated()


def _open_store(args: argparse.Namespace) -> Store:
    store = Store(args.db)
    store.migrate()
    return store


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    """Everything wrong with this checkout, in one pass, before it matters."""
    print("CivicBridges ops doctor\n")
    failed = False

    try:
        cfg = _load_config(strict=False)
    except ConfigError as exc:
        print(f"config could not be loaded: {exc}", file=sys.stderr)
        return 2

    errors = [p for p in cfg.problems if p.level == "error"]
    warns = [p for p in cfg.problems if p.level == "warn"]
    todos = [p for p in cfg.problems if p.level == "todo"]

    print(f"config: {len(errors)} error, {len(warns)} warn, {len(todos)} unconfirmed")
    for problem in errors + warns:
        print(f"  {problem}")
    failed = failed or bool(errors)

    # Routed types must be storable, or the first real obligation fails at the INSERT.
    schema = (REPO_ROOT / "ledger" / "schema.sql").read_text()
    match = re.search(r"type\s+TEXT NOT NULL CHECK \(type IN\s*\((.*?)\)\)", schema, re.S)
    allowed = set(re.findall(r"'([a-z_]+)'", match.group(1))) if match else set()
    routed = {r.get("type") for r in cfg.routing_rules if r.get("type")}
    missing = routed - allowed
    print(f"\nledger schema: {len(allowed)} obligation types accepted")
    if missing:
        print(f"  ERROR routed types the ledger would reject: {sorted(missing)}")
        failed = True

    print("\nphase and guardrails:")
    print(f"  phase {cfg.phase}, outbound {'ENABLED' if cfg.outbound_enabled else 'disabled'}")
    print(f"  kill switch {'ENGAGED' if cfg.killswitch_engaged else 'clear'}")
    allowed_send, reason = cfg.may_autosend("vendor_chase", "oem")
    print(f"  autosend vendor_chase to an OEM: {'yes' if allowed_send else 'no'} ({reason})")
    # Checked against the rule itself, not against may_autosend, because in phase 0 the
    # phase check would mask a missing gov entry and the guardrail would look intact.
    outbound_cfg = cfg.guardrails.get("outbound", {}) or {}
    if "gov" not in set(outbound_cfg.get("never_autosend_to_classes", []) or []):
        print("  ERROR guardrail 1 is broken: 'gov' is not in never_autosend_to_classes")
        failed = True
    else:
        print("  guardrail 1 intact: gov is never auto-sent, at any phase")
    for required in ("stop_work", "contract_action", "award"):
        if required not in set(
            (cfg.guardrails.get("confidence", {}) or {}).get("always_human_review", []) or []
        ):
            print(f"  ERROR {required} is not in confidence.always_human_review")
            failed = True

    print("\ncoverage hours:")
    unknown = [
        pid for pid in sorted(cfg.person_ids)
        if str(cfg.person(pid).get("coverage_hours")) == config_mod.TODO
    ]
    if unknown:
        print(f"  {len(unknown)} unconfirmed: {', '.join(unknown)}")
        print("  clocks for these people fall back to wall clock and are marked degraded")
    else:
        print("  all confirmed")

    print("\nextraction:")
    from .extract import claude_extractor
    print(f"  claude CLI: {'found' if claude_extractor.cli_available() else 'NOT FOUND'}")
    for name in ("triage-extract", "promise-scan"):
        path = REPO_ROOT / "prompts" / f"{name}.md"
        print(f"  prompt {name}: {'ok' if path.exists() else 'MISSING'}")
        failed = failed or not path.exists()

    print("\nstore:")
    try:
        with Store(args.db) as store:
            store.migrate()
            counts = {
                "messages": store.query("SELECT COUNT(*) AS n FROM messages")[0]["n"],
                "threads": store.query("SELECT COUNT(*) AS n FROM threads")[0]["n"],
                "obligations": store.query("SELECT COUNT(*) AS n FROM obligations")[0]["n"],
            }
            print(f"  {args.db}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
            for row in store.last_sync():
                print(f"  sync {row['target']}: last success {row['last_success'] or 'never'}")
            unclaimed = store.unclaimed_mailboxes()
            if unclaimed:
                print(f"  {len(unclaimed)} mailbox(es) with no named reader:")
                for row in unclaimed:
                    print(f"    {row['address']} ({row['kind']})")
                print("    an obligation from a mailbox nobody is recorded as reading "
                      "cannot satisfy the single-owner rule")
    except Exception as exc:
        print(f"  ERROR {type(exc).__name__}: {exc}")
        failed = True

    if todos:
        print(f"\n{len(todos)} unconfirmed placeholder(s). These block Phase 1, not Phase 0:")
        for problem in todos[: args.todo_limit]:
            print(f"  {problem.where}")
        if len(todos) > args.todo_limit:
            print(f"  ... and {len(todos) - args.todo_limit} more (--todo-limit to see them)")

    print("\n" + ("FAILED" if failed else "OK"))
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = _load_config()
    since = None
    if args.since:
        since = dt.datetime.fromisoformat(args.since).replace(tzinfo=dt.timezone.utc)

    with _open_store(args) as store:
        if args.source in ("mbox", "eml", "maildir"):
            source = get_source("mbox", paths=args.path, mailbox_address=args.mailbox)
            results = pipeline.ingest_email(cfg, store, source, since)
        elif args.source == "imap":
            mailboxes = json.loads(Path(args.mailbox_file).read_text())
            store.register_mailboxes(mailboxes)
            source = get_source("imap", mailboxes=mailboxes)
            results = pipeline.ingest_email(cfg, store, source, since)
        elif args.source == "telegram":
            identity = json.loads(Path(args.identity_map).read_text()) if args.identity_map else {}
            messages = telegram_ingest.normalize_export(cfg, Path(args.path[0]), identity)
            results = [pipeline.ingest_normalized(
                cfg, store, messages, "telegram", args.path[0]
            )]
        else:
            print(f"unknown source {args.source}", file=sys.stderr)
            return 2

        merged = store.reconcile_threads()
        if merged:
            print(f"reconciled {merged} split thread(s) into their conversation")

        failed = False
        for result in results:
            print(result.summary())
            for note in result.notes[:10]:
                print(f"    {note}")
            failed = failed or not result.ok
    return 1 if failed else 0


def cmd_portal(args: argparse.Namespace) -> int:
    """Paste bridge. A human pastes, the system parses and then owns the follow-up."""
    cfg = _load_config()
    text = Path(args.file).read_text() if args.file else sys.stdin.read()
    parsed = portals.parse(args.kind, text, cfg)
    print(json.dumps({"kind": parsed.kind, "fields": parsed.fields,
                      "missing": parsed.missing}, indent=2))
    if parsed.needs_human:
        print(
            "\nThis paste is incomplete. The missing fields above are not guessed, "
            "on purpose: a wrong close date would silently break the whole backward "
            "planned chase cadence that depends on it.",
            file=sys.stderr,
        )
        return 1
    return 0


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------


def cmd_report(args: argparse.Namespace) -> int:
    cfg = _load_config()
    if args.name not in PHASE0_REPORTS:
        print(f"unknown report {args.name}; known: {', '.join(PHASE0_REPORTS)}", file=sys.stderr)
        return 2
    with _open_store(args) as store:
        report = PHASE0_REPORTS[args.name].run(cfg, store)
        text = to_markdown(report)
        path = _write_report(store, report, text, args.out)
        print(text if not args.quiet else f"{report.key}: {report.row_count} rows -> {path}")
    return 0


def cmd_phase0(args: argparse.Namespace) -> int:
    """The whole observatory in one run. This is what cron calls."""
    cfg = _load_config()
    if cfg.phase != 0:
        print(f"note: guardrails.yaml says phase {cfg.phase}; running the phase 0 reports anyway")

    with _open_store(args) as store:
        # Observations are recomputed from scratch every run. Incremental observation
        # state is how a report quietly stops finding things.
        store.clear_observations()
        _replay_observations(cfg, store)

        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        out_dir = Path(args.out or (REPORT_DIR / stamp))
        out_dir.mkdir(parents=True, exist_ok=True)

        stale_any = False
        for name, module in PHASE0_REPORTS.items():
            report = module.run(cfg, store)
            text = to_markdown(report)
            path = out_dir / f"{name}.md"
            path.write_text(text)
            store.record_report_run(name, report.row_count, {"phase0": True}, str(path))
            stale_any = stale_any or report.stale
            print(f"{name:15} {report.row_count:5} finding(s) -> {path}")

        if stale_any:
            print(
                "\nAt least one report flagged stale data. Every count above is a floor, "
                "not a total. Check `./cb health` before circulating these.",
                file=sys.stderr,
            )
    return 0


def _replay_observations(cfg: Config, store: Store) -> None:
    """Re-run the deterministic extractors over stored messages.

    Phase 0 reports must be reproducible from the message store alone, so a detector
    improvement changes the reports on the next run without a re-sync.
    """
    from .extract import rules
    from .store import parse_ts

    for message in store.query(
        "SELECT * FROM messages WHERE quarantined = 0 ORDER BY sent_at"
    ):
        sent_at = parse_ts(message["sent_at"])
        if sent_at is None:
            continue
        summary = rules.summarize(
            cfg, message["body_text"] or "", sent_at, message["direction"],
            message["counterparty_class"] == "internal",
        )
        for commitment in summary["commitments"]:
            store.record_promise(
                message_id=message["id"], thread_key=message["thread_key"],
                promised_by=message["from_addr"], promised_to=message["to_addrs"],
                text=commitment["text"], due_text=commitment["due_text"],
                due_at=commitment["due_at"], confidence=commitment["confidence"],
                detector="rules.commitment", status="unknown",
            )
        for handoff in summary["handoffs"]:
            store.record_handoff(
                message_id=message["id"], thread_key=message["thread_key"],
                from_person=message["from_addr"], to_person=handoff["to_person"],
                text=handoff["text"], confidence=handoff["confidence"],
                detector="rules.handoff",
                crosses_offset_boundary=1 if pipeline._is_offset(cfg, handoff["to_person"]) else 0,
            )


def _write_report(store: Store, report, text: str, out: str | None) -> str:
    if not out:
        store.record_report_run(report.key, report.row_count, {}, None)
        return "(stdout)"
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    store.record_report_run(report.key, report.row_count, {}, str(path))
    return str(path)


# ---------------------------------------------------------------------------
# health and kill switch
# ---------------------------------------------------------------------------


def cmd_health(args: argparse.Namespace) -> int:
    """A stalled mail sync is failure mode number one. This is how it gets noticed."""
    cfg = _load_config(strict=False)
    health = cfg.guardrails.get("health", {}) or {}
    limit = int(health.get("mailbox_stale_after_minutes", 60))
    now = dt.datetime.now(dt.timezone.utc)
    from .store import parse_ts

    failed = False
    with _open_store(args) as store:
        rows = store.last_sync()
        if not rows:
            print("no sync has ever run")
            return 1
        for row in rows:
            last_success = parse_ts(row["last_success"])
            if last_success is None:
                print(f"FAIL {row['target']}: never synced successfully")
                failed = True
                continue
            age = (now - last_success).total_seconds() / 60
            state = "OK  " if age <= limit else "FAIL"
            failed = failed or age > limit
            print(f"{state} {row['target']}: {int(age)} min since last success (limit {limit})")

        recent = store.query(
            "SELECT target, error FROM sync_health WHERE ok = 0 AND error IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 5"
        )
        for row in recent:
            print(f"  recent failure on {row['target']}: {row['error']}")
    return 1 if failed else 0


def cmd_killswitch(args: argparse.Namespace) -> int:
    """One command halts all outbound. Everyone on the team knows it."""
    cfg = _load_config(strict=False)
    path = cfg.killswitch_path
    if args.state == "on":
        path.write_text(
            f"engaged at {dt.datetime.now(dt.timezone.utc).isoformat()}\n"
            "All outbound is halted while this file exists. Delete it, or run "
            "`./cb killswitch off`, to resume.\n"
        )
        print(f"KILL SWITCH ENGAGED. All outbound halted. ({path})")
    elif args.state == "off":
        if path.exists():
            path.unlink()
        print("kill switch cleared. Outbound follows guardrails.yaml again.")
    else:
        print("ENGAGED" if path.exists() else "clear")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    with _open_store(args) as store:
        for label, sql in (
            ("messages", "SELECT COUNT(*) AS n FROM messages"),
            ("  inbound", "SELECT COUNT(*) AS n FROM messages WHERE direction='inbound'"),
            ("  outbound", "SELECT COUNT(*) AS n FROM messages WHERE direction='outbound'"),
            ("  internal", "SELECT COUNT(*) AS n FROM messages WHERE direction='internal'"),
            ("  quarantined", "SELECT COUNT(*) AS n FROM messages WHERE quarantined=1"),
            ("threads", "SELECT COUNT(*) AS n FROM threads"),
            ("promises detected", "SELECT COUNT(*) AS n FROM promises"),
            ("handoffs detected", "SELECT COUNT(*) AS n FROM handoffs"),
            ("obligations", "SELECT COUNT(*) AS n FROM obligations"),
        ):
            print(f"{label:20} {store.query(sql)[0]['n']}")
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cb", description="CivicBridges ops automation")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="ledger database path")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="validate config, schema, and environment")
    doctor.add_argument("--todo-limit", type=int, default=10)
    doctor.set_defaults(func=cmd_doctor)

    ingest = sub.add_parser("ingest", help="capture messages into the store")
    ingest.add_argument("source", choices=["mbox", "eml", "maildir", "imap", "telegram"])
    ingest.add_argument("path", nargs="*", help="files or directories, for file sources")
    ingest.add_argument("--mailbox", default="fixtures@local")
    ingest.add_argument("--mailbox-file", help="JSON list of mailboxes, for imap")
    ingest.add_argument("--identity-map", help="JSON map of Telegram sender to person id")
    ingest.add_argument("--since", help="ISO date; the backend overlaps this window")
    ingest.set_defaults(func=cmd_ingest)

    portal = sub.add_parser("portal", help="parse a pasted portal record")
    portal.add_argument("kind", choices=sorted(portals.PARSERS))
    portal.add_argument("--file", help="read from a file instead of stdin")
    portal.set_defaults(func=cmd_portal)

    report = sub.add_parser("report", help="run one report")
    report.add_argument("name", choices=sorted(PHASE0_REPORTS))
    report.add_argument("--out", help="write markdown here instead of stdout")
    report.add_argument("--quiet", action="store_true")
    report.set_defaults(func=cmd_report)

    phase0 = sub.add_parser("phase0", help="run every Phase 0 report")
    phase0.add_argument("--out", help="output directory")
    phase0.set_defaults(func=cmd_phase0)

    health = sub.add_parser("health", help="check sync freshness")
    health.set_defaults(func=cmd_health)

    kill = sub.add_parser("killswitch", help="halt or resume all outbound")
    kill.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")
    kill.set_defaults(func=cmd_killswitch)

    stats = sub.add_parser("stats", help="what is in the store")
    stats.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"config error:\n{exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"missing file: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
