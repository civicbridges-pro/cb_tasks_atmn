# CivicBridges Operations Automation

This repository is the automation program for CivicBridges. Every agent, script, and
scheduled run in here inherits the rules in this file. Read it before doing anything.

## What this system is

CivicBridges has support, AP, and accounting, but the actual obligations of the business
(who owes what to whom by when) live inside individual inboxes and Telegram threads. When
a message goes unanswered, nothing anywhere in the company notices.

This system exists to fix exactly that, and nothing else:

> Every inbound signal becomes a tracked obligation with an owner, a due date, and an
> escalation path. Nothing closes silently. Nothing dies in an inbox.

This is not an email bot. Automating replies without the ledger underneath just produces
faster fragmentation.

## Source of truth map

Decided once. Enforce it in code. Never introduce a second place a human has to update.

| Domain | System of record | Notes |
| --- | --- | --- |
| Contracts, delivery orders, awards | Zoho CRM | contract # is the primary key across the company |
| Vendors, OEMs, distributors, channel status | Zoho CRM | includes authorized-channel and letter-of-supply state |
| Work in flight, tasks, phases | Zoho Projects | every obligation over ~3 days becomes a task |
| Money, AP, AR, invoices | QuickBooks | Cooper Norman / Bill.com reconcile against it |
| Documents, quotes, packing slips, certs | Google Drive | strict naming convention, machine-parseable (`config/naming.yaml`) |
| Commitments and follow-ups | Obligation Ledger | custom module inside Zoho CRM; the thing we do not have today |
| Communication | Email, Telegram | transport only, never truth |

**Contract number is the primary key across every system.**

Email and Telegram are transport. The ledger is truth. If code ever treats a mailbox as
authoritative state, that is a bug.

## Ledger invariants

These four rules are what make the ledger work, and they are the four most teams break:

1. **Owner is always exactly one named human.** Never "procurement." Never "the team."
   A group owner means no owner. Enforced in `cbops/config.py`.
2. **`waiting_external` still has a clock.** Waiting on a vendor is not done. It decays
   into an automatic chase.
3. **Nothing closes without evidence.** A quote is not sent until there is a link to what
   was sent. No evidence, no close.
4. **No autonomous close.** Obligations resolve with evidence or a human decision.

## Guardrails

Machine-readable in `config/guardrails.yaml`. Non-negotiable:

1. **No autonomous outbound to any government contracting officer or agency. Ever.**
   Human approval every time, no exceptions, no confidence threshold that unlocks it.
2. **No autonomous commitment to price, delivery date, or contract terms.** Drafts may
   propose. Humans commit.
3. **No auto-archive, auto-delete, or auto-close.**
4. **Approval threshold.** Any PO or commitment above the configured dollar figure
   requires Doug or Anna, in writing, logged.
5. **CUI and export-controlled content.** Some drawings, specs, and technical data cannot
   flow into arbitrary tools. Classify and quarantine *before* ingesting, not after. When
   in doubt, quarantine. A false quarantine costs a human a minute; a false ingest is an
   incident.
6. **Complete audit trail.** Every automated action logged with prompt, output, and actor.
   If this is ever audited, the log is the defense.
7. **Kill switch.** `./cb killswitch on` halts all outbound. Everyone on the team knows it.
8. **Low confidence goes to a human.** An agent that quietly mislabels a stop-work order
   as routine is worse than no agent. Never guess. Route to triage.

## Voice rules for anything drafted

- Direct and warm. Say the thing.
- No filler openers. Never "I hope this email finds you well," "Just following up,"
  "Reaching out to," "Per my last email."
- **No em dashes.** Use a comma, a period, or a colon.
- No exclamation points in external mail.
- Reference the contract number, solicitation number, or NSN in the first two lines when
  one applies. Government readers scan for it.
- Ask for one specific thing with one specific date. Not "at your earliest convenience."
- Signature block per the company standard.
- Never apologize for following up. State the ask and the date.

## Phase discipline

Current phase: **Phase 0, Observatory.** Read-only. Nothing sends. Nothing writes to Zoho.

The Phase 1 ledger is built but gated. `config/guardrails.yaml` declares the live phase, and
until a human raises it every ledger-writing command runs in preview: it computes exactly
what would be written and persists nothing. Preview the ledger against real mail first, then
raise the phase. Never raise it in code, and never as a side effect of another change.

| Phase | Window | What turns on |
| --- | --- | --- |
| 0 Observatory | weeks 1-2 | read-only capture, four leak reports, baseline metrics |
| 1 Ledger | weeks 3-6 | ledger live, triage + routing, daily digests. Still no outbound |
| 2 Drafting | weeks 7-12 | approve-and-send drafts, vendor chases, Zoho writeback |
| 3 Contract lifecycle | weeks 13-20 | full chain instrumented, contract health dashboard |
| 4 Narrow autonomy | week 20+ | send-without-approval for proven, reversible categories only |

Do not build Phase N+1 features while Phase N is unproven. Most of the value is at Phase 2
and 3. Resist jumping to autonomy.

## Working in this repo

- `./cb doctor` validates config and environment. Run it after any config change.
- `./cb phase0` runs the four Phase 0 reports plus baseline metrics.
- `./cb phase1` previews the whole ledger loop: triage, routing, cadence, audit, digests.
  Add `--commit` to persist, which requires phase 1 to be declared.
- `./cb owed` answers "what does this company owe, to whom, by when". That sentence is the
  Phase 1 exit test, so it is one command.
- `make test` runs the suite. `tests/fixtures/` is the gold. Extraction quality is entirely
  a function of how many real messy examples the classifier has seen. Add every
  interesting real thread (anonymized) as a fixture.
- Scheduled runs are headless: cron calls `./cb` or `claude -p` with a prompt file from
  `prompts/`. Nobody sits in a session.
- Zoho CRM, Zoho Projects, QuickBooks, Drive, and Gmail have connectors already available.
  Wire those before building any custom API client.
- Ingest lives behind `cbops/ingest/base.py`. A mail platform migration must swap one
  module, never touch the pipeline.

## Known open decisions

Tracked in `docs/open-questions.md`. The blocking one is the mail path (Namecheap IMAP vs
Google Workspace migration). Anything marked `TODO_CONFIRM` in `config/` is a placeholder
that a human must confirm before Phase 1. `./cb doctor` lists them.
