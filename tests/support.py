"""Shared test helpers.

`NOW` is pinned. Every report takes `now` as a parameter precisely so the suite is
deterministic: a report whose output depends on when CI runs is a report nobody can
regression test, and these reports are the Phase 0 deliverable.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from cbops import config as config_mod
from cbops import pipeline
from cbops.ingest import get_source
from cbops.store import Store

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_MAIL = REPO_ROOT / "tests" / "fixtures" / "mail"

# Tuesday 25 August 2026, 15:00 UTC. Fixtures are dated the week before.
NOW = dt.datetime(2026, 8, 25, 15, 0, tzinfo=dt.timezone.utc)


def load_config():
    return config_mod.load_unvalidated(REPO_ROOT / "config")


def ingested_store(paths=None) -> tuple[Store, object]:
    """An in-memory store with the fixture corpus captured through the real pipeline."""
    cfg = load_config()
    store = Store(":memory:")
    store.migrate()
    source = get_source(
        "mbox", paths=paths or [FIXTURE_MAIL], mailbox_address="quotes@civicbridges.com"
    )
    pipeline.ingest_email(cfg, store, source, None)
    store.reconcile_threads()
    return store, cfg


def rows_by(report, column: str) -> dict:
    return {row[column]: row for row in report.rows}


def phase1_config():
    """The shipped config with phase 1 declared.

    Phase is a decision humans make in config/guardrails.yaml, and the committed value stays
    0 until Phase 0 has run on real mail. The Phase 1 tests need a phase 1 config without
    changing that, so they build one here.
    """
    import copy

    from cbops.config import Config

    cfg = load_config()
    clone = Config(
        people=copy.deepcopy(cfg.people), routing=copy.deepcopy(cfg.routing),
        sla=copy.deepcopy(cfg.sla), guardrails=copy.deepcopy(cfg.guardrails),
        naming=copy.deepcopy(cfg.naming), counterparties=copy.deepcopy(cfg.counterparties),
    )
    clone.guardrails["phase"] = 1
    clone.problems = cfg.problems
    return clone


def ledger_store():
    """The fixture corpus triaged, routed, and committed to an in-memory ledger."""
    from cbops.agents import router, triage

    store, _ = ingested_store()
    cfg = phase1_config()
    candidates = triage.scan(cfg, store, now=NOW)
    routed = router.build(cfg, store, candidates, now=NOW, persist=True)
    return store, cfg, routed
