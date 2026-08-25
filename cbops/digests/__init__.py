"""Daily digests.

Phase 1's whole visible output. The ledger can be perfect and still change nothing if the
obligations in it never reach the person who owes them, so the digest is not a nice-to-have
on top of the ledger, it is the mechanism.

Two audiences, deliberately different:

*   **Personal**, one per owner. Only what that person owes, ordered by what breaks first.
    Short enough to read standing up. Anything they cannot act on is left out.
*   **Exec rollup**, for Doug and Anna. Not a longer personal digest: it answers whether the
    system is working, where it is breaking, and what needs a decision rather than a nudge.

Neither one sends. In Phase 1 they render to markdown and a human circulates them.
"""

from . import exec_rollup, personal

__all__ = ["personal", "exec_rollup"]
