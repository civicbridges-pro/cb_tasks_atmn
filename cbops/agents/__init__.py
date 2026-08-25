"""Phase 1 agents: triage, router, chaser, auditor.

None of these send anything. Phase 1 turns the ledger on and keeps outbound off, which is
what makes it safe to run for four weeks while the team learns to trust it.

Every agent honors `preview` mode. In preview an agent computes exactly what it would do
and persists nothing, so the whole Phase 1 ledger can be inspected against real mail while
the deployment is still the Phase 0 observatory. That is the point: Phase 1 should not go
live on faith, and a preview run is the evidence that it works.
"""

from . import auditor, chaser, router, triage

__all__ = ["triage", "router", "chaser", "auditor"]
