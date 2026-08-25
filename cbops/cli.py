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

from . import compliance as compliance_mod
from . import config as config_mod
from . import fixtures as fixtures_mod
from . import ledger_view, pipeline
from .agents import auditor, chaser, router, triage
from .config import REPO_ROOT, Config, ConfigError
from .digests import exec_rollup, personal
from .ingest import get_source
from .ingest import telegram as telegram_ingest
from .ingest import portals
from .reports import PHASE0_REPORTS
from .validate import sample as sample_mod
from .validate import selfcheck
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
    available = claude_extractor.cli_available()
    print(f"  claude CLI: {'found' if available else 'NOT FOUND'}")
    print("  model grading: "
          + ("available via `./cb triage --model`" if available
             else "unavailable; triage runs deterministic only, capped below the act threshold"))
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
# validating phase 0
# ---------------------------------------------------------------------------


def cmd_selfcheck(args: argparse.Namespace) -> int:
    """Did the pipeline handle this mail correctly? No human labels needed."""
    cfg = _load_config()
    with _open_store(args) as store:
        report = selfcheck.run(cfg, store)
        text = to_markdown(report)
        path = _write_report(store, report, text, args.out)
        print(text if not args.quiet else
              f"selfcheck: {report.metrics['verdict']} "
              f"({report.metrics['failures']} fail, {report.metrics['warnings']} warn) -> {path}")
    return 1 if report.metrics.get("failures") else 0


def cmd_sample(args: argparse.Namespace) -> int:
    """Draw a stratified review worksheet. Half flagged, half not, shuffled."""
    cfg = _load_config()
    with _open_store(args) as store:
        worksheet = sample_mod.draw(cfg, store, args.report, size=args.n, seed=args.seed)
        if not worksheet.items:
            print(f"nothing to sample for {args.report}: the store has no qualifying messages",
                  file=sys.stderr)
            return 1
        path = Path(args.out or (REPORT_DIR / f"sample-{args.report}.csv"))
        worksheet.write(path)

        flagged = sum(1 for item in worksheet.items if item.stratum == "flagged")
        print(f"{len(worksheet.items)} rows -> {path}")
        print(f"  {flagged} the report flagged, {len(worksheet.items) - flagged} it did not, "
              "shuffled together")
        print(f"  drawn from {worksheet.populations['flagged']} flagged and "
              f"{worksheet.populations['not_flagged']} unflagged items (seed {worksheet.seed})")
        print(f"\n  Question for each row: {worksheet.question}")
        print("  Fill in the `human_says` column. Add a `human_note` when the answer is "
              "interesting or you disagree.")
        print(f"\n  Then: ./cb score {path}")
        print("\n  Label without looking at `system_says` first if you can. Reading the "
              "system's answer before forming your own is how a review agrees with itself.")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """Grade a returned worksheet: precision measured, recall estimated."""
    scores, disagreements = sample_mod.read_labels(Path(args.worksheet))
    if not scores:
        print(f"no scoreable rows in {args.worksheet}", file=sys.stderr)
        return 2
    report = sample_mod.score_report(scores, disagreements)
    text = to_markdown(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
        print(f"{report.row_count} report(s) scored -> {args.out}")
    else:
        print(text)
    return 0


def cmd_fixture(args: argparse.Namespace) -> int:
    """Export a real thread as an anonymized fixture, so a disagreement becomes a test."""
    cfg = _load_config()
    with _open_store(args) as store:
        try:
            written = fixtures_mod.export_thread(
                cfg, store, args.thread, Path(args.out), prefix=args.name)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    for path in written:
        print(path)
    print(fixtures_mod.WARNING, file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# phase 1: ledger
# ---------------------------------------------------------------------------


def _require_phase_1(cfg: Config, what: str) -> None:
    """Persisting to the ledger is Phase 1 behavior, so it needs Phase 1 to be declared.

    Preview mode needs no permission because it writes nothing. This is the guard that lets
    the whole Phase 1 ledger be inspected against real mail while the deployment is still
    the Phase 0 observatory, which is the opposite of building Phase N+1 on faith.
    """
    if cfg.phase < 1:
        raise ConfigError(
            f"{what} writes to the ledger, which is Phase 1 behavior, and "
            f"config/guardrails.yaml declares phase {cfg.phase}.\n\n"
            "  Run without --commit to preview exactly what would be written.\n"
            "  When the preview looks right on real mail, set `phase: 1` in "
            "config/guardrails.yaml.\n\n"
            "Phase 1 still sends nothing: outbound stays disabled until Phase 2."
        )


def cmd_triage(args: argparse.Namespace) -> int:
    """Classify messages into obligations and route each to one named human."""
    cfg = _load_config()
    if args.commit:
        _require_phase_1(cfg, "triage --commit")

    with _open_store(args) as store:
        candidates = triage.scan(cfg, store, since=args.since)

        model_notes: list[str] = []
        if args.model or args.model_all:
            candidates, model_notes = triage.apply_model(
                cfg, store, candidates, limit=args.model_limit, everything=args.model_all)

        routed = router.build(cfg, store, candidates, persist=args.commit)

        if not routed:
            print("no new obligations found")
            return 0

        report = _routed_report(cfg, routed, committed=args.commit,
                                graded_by_model=bool(model_notes))
        report.notes.extend(model_notes)
        text = to_markdown(report)
        path = _write_report(store, report, text, args.out)
        print(text if not args.quiet else
              f"{report.row_count} obligation(s) {'committed' if args.commit else 'previewed'}"
              f" -> {path}")

        if not args.commit:
            print(
                "\nPreview only. Nothing was written to the ledger. Re-run with --commit "
                "once phase 1 is declared in config/guardrails.yaml.",
                file=sys.stderr,
            )
    return 0


def _routed_report(cfg: Config, routed: list, committed: bool,
                   graded_by_model: bool = False):
    from .reports.render import Report

    report = Report(
        key="triage",
        title="Triage and routing" + ("" if committed else " (preview)"),
        subtitle=(
            "Every message that creates an obligation, with one named owner and a clock. "
            + ("Written to the ledger." if committed else
               "Nothing was written: this is what the ledger would contain.")
        ),
        columns=["type", "owner", "status", "direction", "counterparty", "what_is_owed",
                 "due_at", "respond_by", "next_chase", "confidence", "review", "page"],
        rows=[item.as_row() for item in routed],
    )
    unowned = sum(1 for item in routed if item.owner == config_mod.TRIAGE_QUEUE)
    review = sum(1 for item in routed if item.candidate.needs_human_review)
    paged = [p for item in routed for p in item.page_now]
    report.metrics = {
        "obligations": len(routed),
        "to the triage queue (no confident owner)": unowned,
        "needing human review": review,
        "waiting on someone external": sum(
            1 for item in routed if item.status == "waiting_external"),
        "pages": ", ".join(sorted(set(paged))) or "none",
    }
    report.notes = [
        "An obligation in the triage queue has no confident owner. Guardrail 8: the system "
        "routes to a human rather than guessing a person.",
        "`review` covers low confidence plus every stop-work, contract action, and award, "
        "which always reach a human whatever the confidence.",
    ]
    if not graded_by_model:
        report.notes.append(
            "Deterministic classification only. Run `--model` to grade the uncertain ones; "
            "a term list alone can never clear the action threshold."
        )
    return report


def cmd_chase(args: argparse.Namespace) -> int:
    """Advance the cadence on everything in waiting_external. Sends nothing."""
    cfg = _load_config()
    if args.commit:
        _require_phase_1(cfg, "chase --commit")

    with _open_store(args) as store:
        actions = chaser.plan(cfg, store)
        if args.commit:
            chaser.apply(cfg, store, actions)

        from .reports.render import Report
        report = Report(
            key="chase",
            title="Chase cadence" + ("" if args.commit else " (preview)"),
            subtitle=(
                "Follow-ups and escalations due now. Phase 1 records them and a human sends "
                "them: approve-and-send drafts arrive in Phase 2."
            ),
            columns=["action", "obligation", "owner", "escalate_to", "chase", "counterparty",
                     "what_is_owed", "next_chase", "reason"],
            rows=[action.as_row() for action in actions],
        )
        report.metrics = {
            "chases due": sum(1 for a in actions if a.action == "chase"),
            "escalations": sum(1 for a in actions if a.action == "escalate"),
            "escalation paths exhausted": sum(1 for a in actions if a.action == "exhausted"),
        }
        report.notes = [
            "A chase driven by an external deadline is planned backward from it. A fixed "
            "interval is the fallback, and the audit report lists where it is being used.",
            "`exhausted` means chasing is over. That obligation needs a decision, not "
            "another follow-up.",
        ]
        text = to_markdown(report)
        path = _write_report(store, report, text, args.out)
        print(text if not args.quiet else f"{len(actions)} action(s) -> {path}")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Nightly consistency checks. Exits non-zero on a critical finding."""
    cfg = _load_config(strict=False)
    with _open_store(args) as store:
        report = auditor.run(cfg, store)
        text = to_markdown(report)
        path = _write_report(store, report, text, args.out)
        print(text if not args.quiet else
              f"audit: {report.metrics['critical']} critical, {report.metrics['high']} high"
              f" -> {path}")
    return 1 if report.metrics.get("critical") else 0


def cmd_digest(args: argparse.Namespace) -> int:
    cfg = _load_config()
    with _open_store(args) as store:
        reports = []
        if args.person:
            reports.append(personal.run(cfg, store, args.person))
        elif args.exec_only:
            reports.append(exec_rollup.run(cfg, store))
        else:
            reports.extend(personal.everyone(cfg, store))
            reports.append(exec_rollup.run(cfg, store))

        if args.out:
            out_dir = Path(args.out)
            out_dir.mkdir(parents=True, exist_ok=True)
            for report in reports:
                path = out_dir / f"{report.key}.md"
                path.write_text(to_markdown(report))
                store.record_report_run(report.key, report.row_count, {}, str(path))
                print(f"{report.key:24} {report.row_count:4} line(s) -> {path}")
        else:
            for report in reports:
                if report.row_count or args.include_empty:
                    print(to_markdown(report))
                    print()
    return 0


def cmd_compliance(args: argparse.Namespace) -> int:
    """The compliance calendar. Needs no mail, no store, and no decisions."""
    cfg = _load_config()
    report = compliance_mod.run(cfg)
    text = to_markdown(report)

    if args.commit:
        _require_phase_1(cfg, "compliance --commit")
    with _open_store(args) as store:
        created = compliance_mod.sync(cfg, store, persist=args.commit)
        path = _write_report(store, report, text, args.out)
        print(text if not args.quiet else
              f"compliance: {report.metrics['expired (target zero)']} expired, "
              f"{report.metrics['date not recorded (target zero)']} undated -> {path}")
        if created:
            print(
                f"\n{len(created)} compliance obligation(s) "
                f"{'created' if args.commit else 'would be created'}",
                file=sys.stderr,
            )
    unknown = report.metrics["date not recorded (target zero)"]
    return 1 if (report.metrics["expired (target zero)"] or unknown) else 0


def cmd_owed(args: argparse.Namespace) -> int:
    """The Phase 1 success test: what does this company owe, to whom, by when."""
    cfg = _load_config()
    with _open_store(args) as store:
        report = ledger_view.run(cfg, store, owner=args.owner,
                                 counterparty=args.counterparty, contract=args.contract)
        text = to_markdown(report)
        path = _write_report(store, report, text, args.out)
        print(text if not args.quiet else f"{report.row_count} open obligation(s) -> {path}")
    return 0


def cmd_phase1(args: argparse.Namespace) -> int:
    """The whole Phase 1 loop. This is what cron calls once the ledger is live."""
    cfg = _load_config()
    if args.commit:
        _require_phase_1(cfg, "phase1 --commit")

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    out_dir = Path(args.out or (REPORT_DIR / stamp))
    out_dir.mkdir(parents=True, exist_ok=True)

    with _open_store(args) as store:
        # In preview the whole loop runs against a throwaway copy, so the digests and the
        # owed view show what they would actually contain. A preview that renders empty
        # digests tells a reader nothing about the thing they are being asked to approve.
        work = store if args.commit else store.snapshot()

        candidates = triage.scan(cfg, work)
        routed = router.build(cfg, work, candidates, persist=True)
        triage_report = _routed_report(cfg, routed, committed=args.commit)

        # The calendar is not driven by mail, so it is synced here rather than triaged.
        compliance_mod.sync(cfg, work, persist=True)

        actions = chaser.plan(cfg, work)
        chaser.apply(cfg, work, actions)

        reports = [triage_report, compliance_mod.run(cfg), auditor.run(cfg, work),
                   ledger_view.run(cfg, work)]
        reports.extend(personal.everyone(cfg, work))
        reports.append(exec_rollup.run(cfg, work))

        for report in reports:
            path = out_dir / f"{report.key}.md"
            path.write_text(to_markdown(report))
            store.record_report_run(report.key, report.row_count, {"phase1": True}, str(path))
            print(f"{report.key:24} {report.row_count:4} line(s) -> {path}")

        print(f"\n{len(actions)} chase action(s) {'applied' if args.commit else 'previewed'}")
        audit = next(r for r in reports if r.key == "audit")
        if audit.metrics.get("critical"):
            print(f"{audit.metrics['critical']} CRITICAL audit finding(s). "
                  "Read audit.md before circulating anything else.", file=sys.stderr)
            return 1
        if not args.commit:
            work.close()
            print(
                "\nPreview only. Nothing was written to the ledger: the reports above come "
                "from a throwaway copy. Set `phase: 1` in config/guardrails.yaml and re-run "
                "with --commit when they read correctly.",
                file=sys.stderr,
            )
    return 0


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

    check = sub.add_parser(
        "selfcheck", help="data-quality checks on captured mail; no human labels needed")
    check.add_argument("--out")
    check.add_argument("--quiet", action="store_true")
    check.set_defaults(func=cmd_selfcheck)

    sample_cmd = sub.add_parser(
        "sample", help="draw a stratified review worksheet for a report")
    sample_cmd.add_argument("--report", required=True, choices=sorted(sample_mod.QUESTIONS))
    sample_cmd.add_argument("-n", type=int, default=40, help="rows in the worksheet")
    sample_cmd.add_argument("--seed", type=int, default=20260825,
                            help="redraw the same sample for a second reviewer")
    sample_cmd.add_argument("--out", help="CSV path")
    sample_cmd.set_defaults(func=cmd_sample)

    score_cmd = sub.add_parser("score", help="grade a labeled worksheet")
    score_cmd.add_argument("worksheet")
    score_cmd.add_argument("--out")
    score_cmd.set_defaults(func=cmd_score)

    fixture = sub.add_parser(
        "fixture", help="export a real thread as an anonymized test fixture")
    fixture.add_argument("thread", help="thread key, from the `thread` column of any report")
    fixture.add_argument("--out", default="tests/fixtures/mail")
    fixture.add_argument("--name", help="filename prefix; defaults to a hash of the thread")
    fixture.set_defaults(func=cmd_fixture)

    triage_cmd = sub.add_parser(
        "triage", help="classify messages into owned obligations (preview by default)")
    triage_cmd.add_argument("--commit", action="store_true",
                            help="write to the ledger; requires phase 1")
    triage_cmd.add_argument("--since", help="only messages sent on or after this ISO date")
    triage_cmd.add_argument(
        "--model", action="store_true",
        help="grade uncertain candidates with the headless Claude layer")
    triage_cmd.add_argument(
        "--model-all", action="store_true",
        help="grade every candidate, not only the uncertain ones")
    triage_cmd.add_argument(
        "--model-limit", type=int, default=25,
        help="cap model calls in one run (default 25); each is a subprocess")
    triage_cmd.add_argument("--out")
    triage_cmd.add_argument("--quiet", action="store_true")
    triage_cmd.set_defaults(func=cmd_triage)

    chase_cmd = sub.add_parser(
        "chase", help="follow-ups and escalations due now (preview by default)")
    chase_cmd.add_argument("--commit", action="store_true",
                           help="record the advance; requires phase 1")
    chase_cmd.add_argument("--out")
    chase_cmd.add_argument("--quiet", action="store_true")
    chase_cmd.set_defaults(func=cmd_chase)

    audit_cmd = sub.add_parser("audit", help="ledger consistency checks")
    audit_cmd.add_argument("--out")
    audit_cmd.add_argument("--quiet", action="store_true")
    audit_cmd.set_defaults(func=cmd_audit)

    digest_cmd = sub.add_parser("digest", help="daily personal digests and the exec rollup")
    digest_cmd.add_argument("--person", help="one person id from people.yaml")
    digest_cmd.add_argument("--exec", dest="exec_only", action="store_true",
                            help="the exec rollup only")
    digest_cmd.add_argument("--out", help="write one file per digest into this directory")
    digest_cmd.add_argument("--include-empty", action="store_true",
                            help="print digests with nothing on them")
    digest_cmd.set_defaults(func=cmd_digest)

    comply = sub.add_parser(
        "compliance", help="the compliance calendar; non-zero exit on expired or undated")
    comply.add_argument("--commit", action="store_true",
                        help="create ledger obligations; requires phase 1")
    comply.add_argument("--out")
    comply.add_argument("--quiet", action="store_true")
    comply.set_defaults(func=cmd_compliance)

    owed = sub.add_parser(
        "owed", help="what this company owes, to whom, by when: the Phase 1 success test")
    owed.add_argument("--owner")
    owed.add_argument("--counterparty")
    owed.add_argument("--contract")
    owed.add_argument("--out")
    owed.add_argument("--quiet", action="store_true")
    owed.set_defaults(func=cmd_owed)

    phase1 = sub.add_parser("phase1", help="the whole Phase 1 loop (preview by default)")
    phase1.add_argument("--commit", action="store_true", help="requires phase 1")
    phase1.add_argument("--out", help="output directory")
    phase1.set_defaults(func=cmd_phase1)

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
