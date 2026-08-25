# Broken promise scan

You are auditing CivicBridges outbound mail for commitments the company made and did not
keep. This is the Phase 0 Broken Promise Report. It is read-only: you are measuring, not
fixing, and nothing you output sends mail.

This report matters more than the others because it is what customers and contracting
officers actually feel. A missed internal task is invisible to them. A missed "I'll have
that to you Friday" is not.

## What counts as a promise

A commitment by someone at CivicBridges to do a specific thing. It counts whether or not a
date was given. "I'll send the quote Friday" and "I'll get you pricing" are both promises;
the second is simply unmeasurable, which is itself worth reporting.

These are not promises:

- Asking the other party to do something.
- "Let me know if you need anything else."
- Restating a fact ("the shipment left Tuesday").
- Quoted text from an earlier message. Only judge text this author newly wrote.

## Judging follow-through

You get the promise and every later message in the thread. Decide:

- `kept`: a later message from our side delivers the thing, or attaches it, or the other
  party acknowledges receiving it.
- `broken`: the due date passed with nothing delivered in the thread.
- `open`: the due date has not passed yet.
- `unknown`: the promise may have been kept outside this thread, by phone, in Telegram, or
  through a portal. Say so rather than calling it broken. **A false "broken" destroys trust
  in this report faster than a missed one.**

## Rules

1. Do not count the same promise twice because it was requoted in a reply.
2. A promise with no date is `unknown` unless a later message clearly delivers it.
3. When the thread is ambiguous, say `unknown` and explain in `note`.
4. Never name a person as having broken a promise unless the message they wrote is in your
   input. Attribute to the sending address, not to a guess.

## Output

A single JSON object, nothing else.

```json
{
  "promises": [
    {
      "text": "the promise as written",
      "promised_by": "sender address",
      "due_text": "Friday",
      "due_at": "2026-08-28T17:00:00Z",
      "status": "broken",
      "resolved_by_message_id": null,
      "confidence": 0.82,
      "note": "why this status, one sentence"
    }
  ]
}
```
