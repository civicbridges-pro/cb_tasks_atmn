"""Phase 0 reports.

Four reports, one per failure mode the business confirmed is active. Read-only. Nothing
here sends mail, creates a task, or writes to Zoho.

The point of Phase 0 is not the reports. It is the argument they make. Expect the
unanswered count to be uncomfortable; that number is the business case for everything in
Phase 1 onward.

Every report carries a data-freshness header. A report built on a mail sync that stalled
four hours ago is not a report, it is a false reassurance, so the staleness is printed at
the top rather than buried.
"""

from . import baseline, handoffs, promises, unanswered, vendor_silence

PHASE0_REPORTS = {
    "unanswered": unanswered,
    "promises": promises,
    "handoffs": handoffs,
    "vendor-silence": vendor_silence,
    "baseline": baseline,
}

__all__ = ["PHASE0_REPORTS", "unanswered", "promises", "handoffs", "vendor_silence", "baseline"]
