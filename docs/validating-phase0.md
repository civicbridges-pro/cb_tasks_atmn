# How to check whether Phase 0 works

Phase 0 currently passes 182 tests against 14 synthetic fixtures. That proves the code runs.
It proves nothing about whether the reports are right about **your** mail, because I wrote
the fixtures, and a detector tested only on examples written by its author is a detector
tested on its own assumptions.

Here is the ladder, cheapest first. Each rung is worth doing before the next one.

---

## Rung 1: does it run on your mail at all (30 minutes)

Get a real export in. You do not need IMAP credentials or the mail-path decision for this.

**Where the export comes from**, easiest first:

- **Thunderbird**: add the mailbox, let it sync, then ImportExportTools NG on the folder,
  "Export all messages in the folder" as EML or mbox. Do the **Sent** folder too, see below.
- **Outlook**: drag messages to a folder in Explorer to get .msg, which this does not read.
  Better: connect Thunderbird to the same account and export from there.
- **Apple Mail**: Mailbox menu, Export Mailbox, produces an .mbox.
- **Live IMAP**, if you have an app password already:
  `CB_IMAP_PASSWORD_<MAILBOX>=... ./cb ingest imap --mailbox-file config/mailboxes.json`

Start with one or two mailboxes and about three months. Enough to be real, small enough to
review.

```bash
./cb ingest mbox /path/to/export --mailbox quotes@civicbridges.com
./cb selfcheck
```

`selfcheck` needs no human labels. It looks for the failures that make a report
*structurally* wrong rather than merely imprecise, and it exits non-zero on any of them.

**The check that matters most is `outbound captured`.** Without the Sent folder there are no
commitments to audit, so the Broken Promise report finds nothing and looks healthy doing it.
That is the worst possible outcome for the most valuable of the four reports: a clean bill
of health that means "I was not looking."

The others in that class: threading (a split thread makes each half look unanswered, which
inflates the headline count), date parsing (an unreadable Date sorts to 1970 and drops out
of every aged report), and multi-day capture gaps (companies do not go quiet for three
business days in a row; that is a sync hole).

Fix every FAIL before reading a single report.

## Rung 2: does it find things you recognize (1 hour)

```bash
./cb phase0
```

Open `var/reports/<today>/unanswered.md` and read the top ten rows with someone who knows
the accounts.

You are asking one question: **do I recognize these?** Not "is this exhaustive." If the top
of the list is threads you already knew were stuck, the ranking is working. If it is full of
mailing lists and noise, the counterparty config needs populating before anything else.

Then check the two places the report deliberately made a judgment call:

- The Unanswered report lists the threads it **excluded** as acknowledgments. Read those.
  If it is wrong there, real obligations are being hidden, and hidden is worse than noisy.
- The Broken Promise report separates `broken` from `unknown`. Every `unknown` is a promise
  it refused to call broken without evidence. Check a few: were they actually kept?

## Rung 3: measure it (2 hours, and this is the real answer)

Recognition is not measurement. For numbers you can defend, you need human verdicts on a
sample that includes items the report **did not** flag, because a sample of findings can
only ever measure precision, and the failure that matters is the one nobody sees.

```bash
./cb sample --report promises -n 40
```

That writes a CSV: half rows the detector flagged, half it did not, shuffled together, with
the system's verdict in one column and a blank `human_says` column next to it. One keystroke
per row: `y`, `n`, or `?`.

Rules that make the result mean something:

1. **Form your answer before reading `system_says`.** Otherwise the review agrees with
   itself and measures nothing.
2. **`?` is a real answer.** If an experienced person cannot tell, an agent certainly
   cannot, and that item belongs in human triage by design. A pile of `?` on one report is a
   finding about the report's question, not about the labeler.
3. **Two people, same seed, is worth the extra hour** on the promise report:
   `./cb sample --report promises --seed 20260825` twice. Where two humans disagree with
   each other, no detector will do better, and that is the ceiling.

Then:

```bash
./cb score var/reports/sample-promises.csv
```

Precision is measured directly. Recall is **estimated**, by weighting each stratum back to
population size: the unflagged pool is sampled far more thinly than the flagged one, so the
raw ratio would flatter recall badly. The report also estimates how many findings were
missed across the whole corpus, which is usually the number that starts the conversation.

### What good looks like

| Report | Precision | Recall | Why the asymmetry |
| --- | --- | --- | --- |
| unanswered | 85%+ | 90%+ | a missed unanswered gov thread is the whole problem; a false one costs a glance |
| promises | 70%+ | 80%+ | recall matters far more, see below |
| handoffs | 70%+ | 70%+ | lowest stakes of the four |
| vendor-silence | 80%+ | 85%+ | a missed one can lose a bid |

**Weight recall over precision, deliberately.** A false positive costs somebody two seconds
in triage. A missed promise is a commitment to a contracting officer that nobody knows was
made. Recall at 80% with precision at 70% is a better system than the reverse, and it is
worth saying out loud because every instinct pulls the other way.

Do not chase these numbers into the nineties. Past a point, the honest fix is not a better
regex, it is the model layer grading the candidates, and after that it is human triage,
which is where guardrail 8 puts the residue anyway.

## Rung 4: make it stay fixed (ongoing)

Every disagreement is a fixture waiting to be written:

```bash
./cb fixture mid:cb-100@civicbridges.com
```

That exports the thread as anonymized `.eml` files into `tests/fixtures/mail/`, replacing
addresses, domains, names, contract and item numbers, money, and phone numbers with stable
fakes, and reconstructing the message from the store so no original headers, tracking
metadata, or attachment payloads come with it.

**It cannot anonymize prose, and it says so.** A sentence can identify a person by role or a
customer by circumstance. Read every exported file before committing it. The tool does the
mechanical part; the judgment is yours.

Then fix the detector, add the assertion, and run `make test`. Now that mistake cannot come
back, which is the only definition of "working" that survives contact with a second month of
mail.

---

## The order these actually break

From most to least likely, in my estimation, and worth checking in this order:

1. **The Sent folder is missing or partial.** Silently zeroes the most valuable report.
2. **Counterparty classes are empty**, so the Unanswered report cannot rank by importance
   and everything sorts as equally urgent. Populating them from Zoho is the single
   highest-leverage config change available during Phase 0.
3. **The promise detector does not match your house phrasing.** Every team writes
   commitments differently. `selfcheck` reports the yield per 100 sent messages; near zero
   means it is blind to how your people write, not that nobody promises anything.
4. **Threading breaks on a client that drops References.** `reconcile_threads` catches the
   common case; the self-check reports the singleton-thread share so you can see if it did.
5. **Coverage hours are unconfirmed**, so every clock runs on wall time. Harmless in Phase 0
   because nothing is aged against a person, and a real problem the moment digests start.

## What this cannot tell you

Whether the obligations exist at all. If a commitment was made on the phone, in Telegram, or
inside a portal, no amount of mail analysis will find it, and the Broken Promise report will
score it `unknown` forever. That is the honest limit of a mail-only observatory, and it is
one more argument for ingesting Telegram (open question 3) and for the paste bridge on the
portals.
