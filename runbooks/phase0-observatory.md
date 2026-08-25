# Runbook: Phase 0, the Observatory

Two weeks. Read-only. Zero risk. Nothing sends, nothing writes to Zoho.

The output of this phase is not the reports. It is the argument they make. Expect the
unanswered count to be uncomfortable.

## Before you start

```bash
./cb doctor
```

Fix anything reported as ERROR. Unconfirmed `TODO_CONFIRM` placeholders do **not** block
Phase 0: they block Phase 1, and doctor lists them so they are visible the whole time.

Everything that matters here is decided in `config/`, not in code. Read
`config/routing.yaml` and `config/sla.yaml` with the team before circulating any report,
because a report built on a wrong routing matrix will be argued with rather than acted on.

## Getting mail in

Phase 0 needs a historical export, not a live connection. That is deliberate: it lets the
mail migration decision (`docs/mail-path-decision.md`) happen in parallel.

**From exported files**, which is the recommended way to start:

```bash
./cb ingest mbox /path/to/export --mailbox quotes@civicbridges.com
```

**From live IMAP**, Path A only. Credentials come from the environment, one per mailbox, and
never from a file in this repo:

```bash
export CB_IMAP_PASSWORD_QUOTES_CIVICBRIDGES_COM='app-specific-password'
./cb ingest imap --mailbox-file config/mailboxes.json --since 2026-06-01
```

`config/mailboxes.json` is not committed. It declares each mailbox's `kind`
(`individual`, `alias`, or `shared`), its `readers`, and its `owner_person`. Declare shared
aliases honestly: the ownership model depends on it.

**Telegram**, to answer open question 3 with data:

```bash
./cb ingest telegram /path/to/result.json --identity-map config/telegram-people.json
```

Then read the promise and handoff counts. If they are near zero, Telegram is chatter and can
be left out. If they are not, it has to be ingested continuously in Phase 1.

## Running the reports

```bash
./cb phase0                     # all four plus baseline, into var/reports/YYYY-MM-DD/
./cb report unanswered          # one report to stdout
```

Reports are recomputed from the message store on every run, never incrementally. A detector
improvement therefore changes the reports on the next run with no re-sync.

## Reading the reports

**Data freshness first, every time.** If the header carries a staleness warning, every count
below it is a floor, not a total. Do not circulate a stale report; run `./cb health` and fix
the sync first.

| Report | What it measures | What to look at first |
| --- | --- | --- |
| `unanswered` | external threads where the last message is inbound and older than 48h | the `gov` rows. Target is zero, not low |
| `promises` | commitments in outbound mail with no follow-through | `broken` rows, then the undated count |
| `handoffs` | internal work passed to a named person with nothing after | the overseas and procurement-to-logistics seams |
| `vendor-silence` | vendor and OEM requests with no reply, aged against the real deadline | anything with `closes_in` under 48 hours |
| `baseline` | median response, unowned threads, turnaround times | what reads `unmeasurable`, and why |

Two habits that keep this honest:

- **Spot-check the exclusions.** The unanswered report lists the threads it dropped as
  acknowledgments. If the classifier is wrong there, real obligations are being hidden. This
  is the highest-value five minutes in the whole phase.
- **Add every interesting thread to `tests/fixtures/mail/`**, anonymized. Extraction quality
  is entirely a function of how many real messy examples the detectors have seen. This is the
  single highest-leverage contribution anyone can make during Phase 0.

## Daily during the two weeks

```bash
./cb health && ./cb phase0
```

A `cron` entry that does the same and mails the output is fine. `./cb health` exits non-zero
when a mailbox has stopped producing, which is what makes it usable as an alert.

## The exit test

Phase 0 is done when all four reports run clean on a full mail history, the numbers are
believed by the people in them, and the mail path decision is made.

Phase 1 starts when you can answer "what does this company owe, to whom, by when" without
asking a person. Phase 0 cannot answer that. It can only show you how far from it you are,
which is exactly its job.

## If something looks wrong

- A report shows zero findings across the board: check `./cb stats`. Zero messages means
  ingest never ran, not that nothing is leaking.
- A thread appears twice: the References chain was broken by a mail client. `./cb ingest`
  runs `reconcile_threads` automatically; if a split survives, the subject or counterparty
  differs between halves.
- A promise is reported broken that was actually kept: it was kept off-thread. That is the
  `unknown` bucket's job, so tighten `looks_like_followthrough` in
  `cbops/extract/rules.py` and add the thread as a fixture.
- Everything is `unknown`: the promise detector found commitments but no evidence either way.
  This is common on Path A, where a portal submission or a phone call is invisible.
