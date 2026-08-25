# civicbridges-ops

The automation program for CivicBridges. Phase 0 is built and live: a read-only observatory
that measures where commitments leak today. Phase 1, the obligation ledger itself, is built
and gated behind a preview mode until Phase 0 has run on real mail.

**Read `CLAUDE.md` first.** It holds the company context, the source of truth map, the
guardrails, and the voice rules, and every agent in this repo inherits them.

## What this is for

CivicBridges has support, AP, and accounting, but the actual obligations of the business
(who owes what to whom by when) live inside individual inboxes and Telegram threads. When a
message goes unanswered, nothing anywhere in the company notices.

> Every inbound signal becomes a tracked obligation with an owner, a due date, and an
> escalation path. Nothing closes silently. Nothing dies in an inbox.

Not an email bot. Automating replies without that ledger underneath just produces faster
fragmentation. See `docs/architecture.md`.

## Quick start

```bash
./cb doctor                                          # validate config and environment
./cb ingest mbox tests/fixtures/mail --mailbox quotes@civicbridges.com
./cb phase0                                          # the four leak reports plus baseline
./cb phase1                                          # preview the ledger, writing nothing
./cb owed                                            # what we owe, to whom, by when
make test                                            # 182 tests
```

Reports are written to `var/reports/YYYY-MM-DD/`. The fixture corpus is synthetic and safe
to run against, and it exercises every failure mode the reports look for.

## Commands

| Command | What it does |
| --- | --- |
| `./cb doctor` | validates config, ledger schema, guardrails, prompts, and store in one pass |
| `./cb ingest mbox <path>` | capture from exported files, the recommended way to start |
| `./cb ingest imap --mailbox-file <f>` | capture from live Namecheap IMAP, Path A |
| `./cb ingest telegram <export.json>` | capture a Telegram Desktop export |
| `./cb portal dibbs --file <f>` | parse a pasted DIBBS, WAWF, or SAM record |
| `./cb report <name>` | run one report to stdout |
| `./cb phase0` | run every Phase 0 report into a dated directory |
| `./cb triage` | classify messages into owned obligations with clocks (preview) |
| `./cb chase` | follow-ups and escalations due now (preview) |
| `./cb audit` | thirteen ledger consistency checks, non-zero exit on a critical |
| `./cb digest` | daily personal digests and the exec rollup |
| `./cb owed` | what this company owes, to whom, by when |
| `./cb phase1` | the whole ledger loop; `--commit` requires phase 1 declared |
| `./cb health` | sync freshness, exits non-zero when a mailbox has stalled |
| `./cb killswitch on` | halt all outbound |
| `./cb stats` | what is in the store |

Anything that writes to the ledger previews by default and persists nothing. `--commit`
refuses unless `config/guardrails.yaml` declares phase 1, and the committed config stays at
phase 0 until a human raises it. A preview runs the full loop against a throwaway copy, so
the digests show what they would really contain.

## What is built, and what is not

Phase discipline is real here. Nothing from a later phase is half-built.

**Built and live (Phase 0):**

- `cbops/ingest/` capture behind one interface: IMAP, files, Telegram export, portal paste
- `cbops/normalize.py` one message shape, with CUI and export-control quarantine at ingest
- `cbops/extract/` deterministic detectors plus a headless Claude judgment layer
- `cbops/clock.py` coverage-aware SLA math, federal holidays, backward-planned deadlines
- `ledger/schema.sql` and `cbops/store.py` the ledger, with its invariants enforced in code
- `cbops/reports/` the four leak reports plus baseline metrics
- `config/` the routing matrix, SLA clocks, guardrails, naming, counterparty classes
- `tests/` 182 tests, and a fixture corpus that is the real asset

**Built, gated behind preview (Phase 1):**

- `cbops/agents/triage.py` a message becomes an obligation, or becomes a question for a human
- `cbops/agents/router.py` one named owner, one clock, one escalation path
- `cbops/agents/chaser.py` the cadence engine, planned backward from real deadlines
- `cbops/agents/auditor.py` thirteen consistency checks; reports, never repairs
- `cbops/digests/` daily personal digests and the exec rollup
- `cbops/ledger_view.py` `./cb owed`, which is the Phase 1 exit test as one command

**Not built, by design:** the drafter (Phase 2), Zoho and Projects writeback (Phase 2), the
contract-lifecycle chain and health dashboard (Phase 3), and any autonomous send (Phase 4).
Writeback goes through the existing connectors rather than a custom API client.

## The four Phase 0 reports

One per failure mode, all four confirmed active by the business.

1. **Unanswered Thread Report.** External threads where the last message is inbound and older
   than 48 hours. Sorted by counterparty importance, never by date: a contracting officer
   waiting six hours outranks a distributor waiting six days.
2. **Broken Promise Report.** Commitments made in outbound mail with no follow-through. The
   hardest to extract and the most valuable, because it is the one customers and contracting
   officers actually feel. A promise whose thread went quiet is reported `unknown`, never
   `broken`: a false accusation destroys trust in the report faster than a miss does.
3. **Dropped Handoff Report.** Internal work passed to a named person with nothing after,
   aged against the receiver's own coverage window so an offset-hours colleague is not
   reported late for being asleep.
4. **Vendor Silence Report.** Vendor and OEM requests with no reply, aged against the
   solicitation or delivery clock rather than a fixed interval. Sorted by time remaining, not
   time waited. This is the Marotta pattern made visible.

Plus **baseline metrics**: median first response by mailbox, threads with no owner, quote
turnaround, award to PO. Where mail alone cannot produce a number it says `unmeasurable`
rather than reporting zero, because zero and unmeasurable look identical on a dashboard and
mean opposite things.

## Phase 1: the ledger

Four agents, none of which send anything.

**Triage** classifies a message into a lane using the `match` terms in
`config/routing.yaml`. Lanes that legitimately share vocabulary (an inbound federal RFQ and
an outbound vendor RFQ are both "RFQ") are separated by `prefer_when` and `avoid_when`
clauses evaluated against facts the message carries, so the tie-break lives in config where
the team can argue with it. Confidence measures how clearly one lane owns the message, not
how many words matched: two lanes each holding a substantive term is ambiguous whatever the
arithmetic says, and it drops below the routing threshold so a human decides.

**Router** assigns exactly one named human, computes the response and completion clocks
against that person's coverage hours, and records the escalation path. Low confidence goes to
the triage queue rather than to a person, because an unclear obligation on the wrong
person's digest is how a team learns to stop reading the digest. `they_owe_us` opens as
`waiting_external` with a clock, since waiting on a vendor is not done.

**Chaser** advances the cadence and escalates when it is spent. A known external deadline
always beats the fixed interval. `exhausted` is not a bug: it means chasing is over and
somebody has to decide something.

**Auditor** runs thirteen checks over the ledger and its config. `critical` is reserved for
integrity, so a failing audit means "do not trust these numbers", not "somebody is behind".
Nothing is ever repaired: guardrail 3 forbids auto-close.

The visible output is a daily digest per owner (ordered by what breaks first, not by what
arrived first), an exec rollup for Doug and Anna, and a digest for the triage queue itself,
because an unowned obligation nobody is shown is the exact failure this program exists to
end.

The exit test is one sentence, so it is one command:

```bash
./cb owed --owner jason
./cb owed --contract SPE4A6-24-D-0123
```

## Guardrails

Full list in `CLAUDE.md`, machine-readable in `config/guardrails.yaml`, and asserted in
`tests/test_config.py`. The ones that shaped the code:

- **No autonomous outbound to a government contracting officer or agency. Ever.** No
  confidence threshold unlocks it.
- **Stop-work, contract actions, and awards always reach a human**, whatever the confidence.
- **CUI and export-controlled content is quarantined before ingest, not after.** A quarantined
  message keeps its envelope so the thread still counts, and loses its body so nothing
  sensitive reaches the store or a prompt.
- **Deterministic detector confidence is capped below the action threshold**, so a regex can
  never authorize an action on its own.
- **One command halts all outbound**: `./cb killswitch on`.
- **Writing to the ledger requires the phase to be declared**, so Phase 1 goes live on a
  preview somebody read, not on faith.

## Open decisions

`docs/open-questions.md`. The blocking one is the mail path: Namecheap IMAP as it stands, or
migrate to Google Workspace first. See `docs/mail-path-decision.md`. Recommendation is the
migration, during the Phase 0 window, while the system is still read-only and nothing depends
on it.

Four things the brief does not settle are tracked there too: stop-work routing versus
stop-work paging, two escalation paths that terminate at their own backup, who owns an
inbound customer quote request, and whether a given counterparty is a supplier or a
customer. The last one is the largest source of triage-queue traffic, and populating the
counterparty classes from Zoho removes it.

## Layout

```
CLAUDE.md              company context, guardrails, voice rules
cb                     CLI entry point; cron calls this, nobody sits in a session
cbops/                 the pipeline
  ingest/              capture backends behind one interface
  extract/             rules detector + headless Claude judgment layer
  reports/             the four leak reports plus baseline
  agents/              triage, router, chaser, auditor
  digests/             personal digests and the exec rollup
config/                everything a human negotiates lives here, not in code
ledger/                schema and migrations
prompts/               prompt files for headless runs
docs/                  architecture and open decisions
runbooks/              how to actually run a phase
tests/fixtures/        anonymized real threads; this is the gold
var/                   local store and generated reports, not committed
```

## Contributing during Phase 0

The highest-value contribution is fixtures. Add every interesting real thread to
`tests/fixtures/mail/` with names, addresses, and dollar figures anonymized. Extraction
quality is entirely a function of how many real messy examples the detectors have seen.
