# Decision: the mail layer

**Status: OPEN. This blocks Phase 1.**

Nothing else can be sequenced until this is decided. Phase 0 runs either way, which is why
the observatory is being built now rather than waiting.

## The constraint

CivicBridges runs on Namecheap Private Email. That is IMAP and SMTP with a plain password
or an app-specific password, and nothing else. Namecheap has said it has no plans to add
OAuth to Private Email, and the Namecheap API covers domains and DNS, not mailboxes.

In practice:

| Capability | Namecheap Private Email |
| --- | --- |
| Push notification on new mail | none, poll or hold IMAP IDLE per mailbox |
| Server-side search | none, sync bodies locally and index them yourself |
| Shared or delegated mailboxes | none, every mailbox is a separate credential |
| Admin API | none, add or suspend or audit a mailbox by hand in the panel |
| Retention, legal hold, eDiscovery | none |
| SPF, DKIM, DMARC control | limited |

## Path A: build the IMAP layer

One to two weeks. Per-mailbox IMAP sync into a local message store, SMTP for send,
credentials in a secrets manager. It works. `cbops/ingest/imap_source.py` is that layer,
already written against this interface.

It is more brittle than an API, it will occasionally stall silently, and it needs its own
health monitoring, which is why `sync_health` and `./cb health` exist regardless of path.

**The cost that is easy to miss.** A shared alias with three readers has no delegation model
and no way to record who acted. The ledger's first invariant is that every obligation has
exactly one named human owner. On Path A that invariant is not enforceable for anything
arriving at a shared alias: the system can ask a human who took it, but it cannot know. So
Path A does not merely make the plumbing more fragile, it weakens the core design.

For 70+ federal contracts, the absence of retention and eDiscovery is the other real gap.
If a contract or claim turns adversarial, "we have no way to produce the mail record" is a
bad position. That is a legal exposure, not an inconvenience.

## Path B: migrate mail to Google Workspace first

The cost is small relative to this build. It buys:

1. A real API with push notifications, so the ledger is never stale between polls.
2. Proper server-side search, so we stop maintaining a local index.
3. Shared and delegated mailboxes, which makes the single-owner invariant enforceable.
4. Retention, legal hold, and admin audit logs.
5. Better deliverability control over SPF, DKIM, and DMARC, which Phase 2 outbound depends on.

It also means Claude connects through the existing Gmail connector instead of custom code,
so the ingest layer largely disappears rather than being ported.

Migration is far easier at current headcount than it will be in two years.

## Recommendation

**Path B, executed during the Phase 0 window**, while the system is still read-only and
nothing depends on it. Phase 0 needs a historical export, not a live connection, so the
observatory can run against exported mail while the migration happens.

If the answer is Path A for now, the ingest layer is already behind a clean interface
(`cbops/ingest/base.py`), so a later migration swaps one module rather than rewriting the
pipeline. Do not let Path A leak upward: nothing outside `cbops/ingest/` may know what a
mailbox is made of.

## What is needed to decide

Open question 2 in `open-questions.md`: which mailboxes exist, who reads each one today, and
which are shared aliases versus individual inboxes. The count of shared aliases is the
number that decides this, because each one is a hole in the ownership model on Path A.

`./cb doctor` lists every mailbox the system has seen with no recorded reader.
