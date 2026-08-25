"""Validation: how you find out whether Phase 0 is telling the truth.

Two different questions, two different tools, because they fail in different ways.

*   `selfcheck` answers "did the pipeline handle this mail correctly?" It needs no human
    labels. It looks for the pathologies that make a report structurally wrong rather than
    merely inaccurate: a missing Sent folder, broken threading, unparsed dates. A report
    built on any of those is confidently empty, which is the most dangerous output this
    system can produce.

*   `sample` answers "are the findings true?" That cannot be answered without a human
    verdict, so it draws a stratified sample, writes a worksheet, and scores your labels
    against the system's. Precision and recall, per report, on your own mail.

Run them in that order. There is no point measuring the accuracy of a report whose inputs
are half missing.
"""

from . import sample, selfcheck

__all__ = ["selfcheck", "sample"]
