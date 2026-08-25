# Open questions

Answer these into the repo before Phase 1. Questions 1 through 6 are from the build brief.
7 and 8 came out of building the config and are conflicts in the brief itself.

Phase 0 does not need any of these answered. It needs mail, read-only.

---

## 1. Path A or Path B on mail

**Blocking.** Nothing else can be sequenced until this is decided.
See `mail-path-decision.md`. Recommendation is Path B, during the Phase 0 window.

## 2. Which mailboxes exist, who reads each one, and which are shared aliases

More consequential here than on most platforms. A shared alias with three readers has no
delegation model and no way to know who acted, so obligations arriving there cannot satisfy
the single-owner invariant without a human claiming them by hand.

The count of shared aliases is also the number that decides question 1.

Record them in a JSON file and pass it to `./cb ingest imap --mailbox-file`. Until then,
`./cb doctor` lists every mailbox seen with no recorded reader.

## 3. Does Telegram carry real commitments, or is it mostly chatter?

If it carries commitments it has to be ingested. If not, leaving it out simplifies the
build considerably.

**This one is answerable with data rather than opinion.** Export a representative window,
run `./cb ingest telegram <export.json>`, then read the promise and handoff counts in the
Phase 0 reports. Do that before deciding.

## 4. Who owns this internally besides Doug?

The highest-risk question on the list, and it is not technical. An automation program with a
single owner who is also the VP of BD stalls the first busy week.

Needed **before Phase 1**, not before Phase 2. Phase 1 is when the system starts producing
daily digests that people are expected to act on, and that requires someone whose job it is
to notice when they stop.

## 5. Which categories of decision may a machine make without Doug, and which must he always see?

Encoded in `config/guardrails.yaml` under `outbound.autonomy_candidates`. The current list is
the brief's Phase 4 candidates and nothing else. Every one of them has the property that the
worst case is a slightly redundant email. Adding anything without that property changes the
risk profile of the whole program.

## 6. Do any contracts flow down NIST 800-171 or CMMC requirements?

Changes what can be ingested and where it can be stored. Until answered, the classifier
quarantines aggressively: any CUI or distribution-statement marker, and any CAD attachment
by extension alone. See `guardrails.classification`.

If the answer is yes for any active contract, revisit before Phase 1, because it may
constrain where the message store itself can live.

---

## 7. Stop-work routing conflicts with stop-work paging

Brief §7 routes contracts, mods, awards, and stop-work to Taj. Brief §6 says stop-work and
mods are rare, high consequence, and should page a human immediately rather than enter a
queue. Taj works offset hours.

As written, the highest-consequence signal in the company has the lowest latency tolerance
and is assigned to the person most likely to be asleep when it arrives.

**Interim resolution in `config/routing.yaml`:** stop-work keeps Taj as primary owner but
carries `page_immediately`, `ignore_coverage_hours`, and `also_notify: [doug, anna]`, so the
page fans out instead of waiting for a coverage window. The config validator rejects any
`page_immediately` rule whose primary works offset hours and has no `also_notify` list.

Confirm or correct this with the team. It is a real decision, not a config detail.

## 9. Who owns an inbound customer quote request?

The brief's routing matrix has no lane for a customer asking us for a price. The closest
entries are "solicitations, bid decisions" (Usman) and "vendor and OEM quotes" (Jason), and
those describe the opposite direction of trade: a federal solicitation coming in, and our
request going out to a supplier.

Without a lane, a school district asking for a quote landed in whichever lane happened to
share a word with it.

**Interim resolution:** a `quote_request` lane in `config/routing.yaml`, primary Usman,
backup Joe, escalating to Doug, matching on customer-side phrasing ("quote request", "please
quote", "availability"). Chosen because it is the closest thing to a bid decision, but this
is a guess about how the company actually works.

Confirm the owner. If SLED quote requests are really Jason's or Morgan's, say so: it is a
one-line change and it decides whose digest they land on every morning.

## 10. Is a counterparty a supplier or a customer?

`config/counterparties.yaml` classifies by domain into `oem`, `distributor`,
`customer_sled`, and so on, and the lists are still empty pending Zoho. Until they are
populated, a distributor who buys from us and a distributor who sells to us look identical,
so an inbound "where is our quote" from either one is genuinely ambiguous and routes to the
triage queue.

That is the correct behavior, and it is also avoidable: populating the counterparty classes
from Zoho removes a whole category of triage-queue traffic. Worth doing before Phase 1 goes
live rather than after.

## 8. Two escalation paths terminate at the backup

Brief §7 lists these:

| Signal | Primary | Backup | Escalates to |
| --- | --- | --- | --- |
| AP and AR, invoices, WAWF | Analiza | Amanda | Amanda |
| Payroll, HR, insurance, benefits | Amanda | Anna | Anna |

An escalation that goes to the person already covering as backup is not an escalation. A
breach lands on someone who has already seen it.

**Interim resolution:** invoices escalate to Anna, HR escalates to Doug. The validator warns
whenever an escalation target equals the backup.

Confirm the intended chains.
