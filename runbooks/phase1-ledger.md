# Runbook: Phase 1, the Ledger

Weeks 3 to 6. The ledger goes live, obligations get owners and clocks, digests start
landing. **Still no outbound.** Nothing in this phase sends a message.

The exit test is one sentence:

> At any moment you can answer "what does this company owe, to whom, by when" without
> asking a person.

`./cb owed` is that answer.

## Do not start this phase yet if

- Phase 0 has not run against real mail history. Everything below is calibrated on synthetic
  fixtures until it has.
- The mail path decision is still open. See `docs/mail-path-decision.md`.
- Nobody besides Doug owns this program. `docs/open-questions.md` question 4. A digest that
  nobody is accountable for reading is a digest that stops being read in week two.

None of those block *previewing* Phase 1, which is the point of the next section.

## Preview before going live

Every ledger-writing command previews by default and persists nothing:

```bash
./cb triage             # what the ledger would contain
./cb chase              # what would be chased or escalated right now
./cb phase1             # the whole loop: triage, cadence, audit, digests
```

Read the preview against real mail and check four things, in this order:

1. **Are the lanes right?** Every row names an owner. Would that person agree the item is
   theirs? A wrong lane is a config fix in `config/routing.yaml`, not a code change: adjust
   the `match` terms, or add a `prefer_when` / `avoid_when` clause.
2. **Is the triage queue small?** Rows there mean the system would not guess an owner. Some
   is correct and healthy. A flood means the term lists do not match how your team actually
   writes, or the counterparty classes in `config/counterparties.yaml` are still empty.
3. **Are the clocks believable?** Anything marked `coverage unknown` is running on wall time
   because that person's `coverage_hours` are still `TODO_CONFIRM`. Fix those before going
   live or the first week of digests will cry wolf.
4. **Do the `review` rows make sense?** Every stop-work, contract action, and award appears
   here by design, whatever the confidence. Anything else here is low confidence.

Iterate on config until the preview reads correctly. This is the cheapest moment to be
wrong.

## Going live

```yaml
# config/guardrails.yaml
phase: 1
```

Then, and only then:

```bash
./cb doctor                  # must pass with the new phase
./cb phase1 --commit
```

Outbound stays disabled. `guardrails.outbound.enabled` is false and Phase 1 does not change
that; approve-and-send drafts arrive in Phase 2.

## The daily loop

What cron runs once the ledger is live:

```bash
./cb health || echo "capture is stale, fix this before trusting anything below"
./cb ingest imap --mailbox-file config/mailboxes.json
./cb phase1 --commit --out var/reports/$(date +%F)
```

`./cb phase1 --commit` exits non-zero when the audit finds a critical, which makes it usable
as an alert rather than something to read later.

Then circulate:

- `digest-<person>.md` to each owner
- `digest-exec.md` to Doug and Anna
- `digest-triage_queue.md` to whoever is assigning that day

## Reading a personal digest

Buckets are printed in the order a person should act:

| Bucket | Meaning |
| --- | --- |
| `OVERDUE` | past its due date |
| `RESPOND` | past the response clock, not yet past the due date |
| `REVIEW` | the system is not confident, or the type always needs a human. Confirm or correct |
| `ESCALATE` | the chase cadence is spent. Another follow-up will not move it |
| `CHASE` | a follow-up is due within a day. In Phase 1 the human sends it |
| `TODAY` | due within a day |
| `WAITING` | waiting on somebody external. Still has a clock, will come back as a chase |
| `UPCOMING` | everything else |

`ESCALATE` is the one to watch. It means chasing has run out and somebody has to decide
something, which is precisely the moment this system exists to surface.

## The audit report

`./cb audit` runs thirteen checks and exits non-zero on any critical. A critical means an
invariant the ledger claims to hold is not holding. The store refuses those on the paths it
controls, so a critical points at a manual edit, a sync, or a config change.

Nothing is ever repaired automatically. Guardrail 3: no auto-close. The auditor reports and
a human decides.

`stale_capture` outranks everything else in practice. While capture is stale, every other
number in every report is a floor rather than a total.

## What to watch in week one

- **Triage queue size, daily.** Should trend to near zero. If it does not, the term lists or
  the counterparty classes need work, not the people.
- **`no_external_deadline` findings.** Each one is a chase running on a generic interval
  instead of a real clock. Capturing close dates at intake fixes them permanently.
- **Digest volume per person.** Someone receiving thirty lines will read none of them. That
  is a routing problem or a real workload problem, and both are worth knowing.
- **Anything a person disputes.** A digest line someone says is not theirs is worth ten
  minutes of config, because that is how trust is either built or lost in this phase.

## Exit test

Phase 1 is done when `./cb owed` is correct, believed, and used to answer questions that
previously required asking a person. Not when the code runs.
