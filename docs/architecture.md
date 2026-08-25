# Architecture

## The reframe

The stated problem is "too much email, too many follow-ups, contracts slipping." The real
problem is that CivicBridges has no system of record for commitments.

Zoho CRM records things: accounts, deals, contracts. Zoho Projects records tasks somebody
remembered to create. But the obligations of the business, meaning who owes what to whom by
when, live inside individual inboxes and Telegram threads. When a message goes unanswered,
nothing anywhere in the company notices. The departments exist; the connective tissue is
human memory.

So the goal is not an email bot:

> Every inbound signal becomes a tracked obligation with an owner, a due date, and an
> escalation path. Nothing closes silently. Nothing dies in an inbox.

Automating replies without that ledger underneath produces faster fragmentation.

## Five layers, built in order

```
                    transport                    truth
                    (never truth)                (the ledger)

  Namecheap IMAP  ┐
  Google Gmail    ├─► L1 Capture ─► L2 Extraction ─► L3 Routing ─► L4 Drafting ─► L5 Autonomy
  Telegram export │   normalized     structured        owner +       human           narrow,
  Portal paste    ┘   message store  JSON, scored      clock         approves        reversible
                                          │
                                          └─► human triage queue (low confidence, always)
```

| Layer | What it does | Where it lives | Phase |
| --- | --- | --- | --- |
| L1 Capture | every mailbox and channel into one normalized message store, read-only | `cbops/ingest/`, `cbops/normalize.py` | 0 |
| L2 Extraction | is there an obligation here, whose, by when, tied to which contract | `cbops/extract/` | 0 and 1 |
| L3 Routing and SLA | one named owner, one clock, escalation on breach, daily digests | `config/routing.yaml`, `cbops/clock.py` | 1 |
| L4 Drafting | outbound drafts in the correct voice with attachments pre-attached | not built | 2 |
| L5 Narrow autonomy | send without approval, reversible low-stakes loops only | not built | 4 |

Most of the value is at L3 and L4. Resist jumping to L5.

## Extraction is two detectors, deliberately

`cbops/extract/rules.py` is a deterministic recall net: regex over commitment phrasing,
handoff phrasing, PIID and NSN references, and relative dates. It over-fires on purpose. Its
confidence is capped below `guardrails.confidence.min_to_act`, so a regex can never
authorize an action by itself.

`cbops/extract/claude_extractor.py` is the judgment layer. It runs headless (`claude -p`
with a prompt file from `prompts/`) and grades the rules layer's candidates.

Both exist because a missed promise is a false negative nobody ever sees. A model-only
pipeline that misses a commitment produces a clean-looking report and a broken promise. A
rules-only pipeline cannot tell a commitment from a courtesy. Rules find the phrasing, the
model decides what it means, and anything either one is unsure about goes to a human.

## The ledger

One central object. Everything else feeds it or renders it. Schema in `ledger/schema.sql`.

Four invariants, enforced in `cbops/store.py` rather than documented and hoped for:

1. **Owner is exactly one named human.** `create_obligation` rejects a list, a group word
   like "procurement", and any name not in `people.yaml`.
2. **`waiting_external` still has a clock.** Enforced by a table CHECK constraint.
3. **Nothing closes without evidence.** `set_status(..., "done")` raises unless an evidence
   row exists.
4. **No autonomous close or drop.** `set_status(..., "dropped", actor="system")` raises.

The system of record for obligations is a custom module inside **Zoho CRM**, not this
database. Zoho is already trusted as truth, and a second place people have to update is a
place people stop updating. From Phase 1 the local store is the message index plus a mirror,
so reports run without hammering the Zoho API.

**Contract number is the primary key across every system.**

## Source of truth map

See the table in `CLAUDE.md`. The short version: Zoho CRM owns contracts and vendors, Zoho
Projects owns work in flight, QuickBooks owns money, Drive owns documents, the ledger owns
commitments, and email and Telegram own nothing.

## Portals with no usable API

DIBBS, WAWF, SAM, and MySBA get a manual-bridge pattern: a human pastes or uploads, the
system parses, records, and then owns the follow-up. These will not be scraped reliably, and
a scraper that breaks quietly stops recording awards. Design around the paste.

`cbops/ingest/portals.py`. An unreadable field is reported, never guessed: a misparsed
solicitation close date would silently break the entire backward-planned chase cadence that
depends on it.

## Health is part of the design, not an add-on

The three things most likely to kill this program:

1. **A brittle mail layer.** If ingest stalls quietly on a Tuesday, the ledger goes stale and
   nobody notices until something is missed. Every sync writes to `sync_health`, every report
   prints its own data freshness at the top, and `./cb health` exits non-zero when a mailbox
   has stopped producing. "Zero new messages" and "we could not tell" must never look alike.
2. **Silent misclassification.** An agent that mislabels a stop-work order as routine is
   worse than no agent. Confidence scoring from day one, low confidence to human triage, and
   stop-work, contract actions, and awards always to a human regardless of confidence.
3. **Automating a broken process.** Some of these workflows are fragmented because the
   process was never defined, not because nobody automated it. Phase 0 exposes which. Fix
   those on paper before pointing code at them.
