-- CivicBridges Obligation Ledger, local store.
--
-- The system of record for obligations is a custom module inside Zoho CRM. This SQLite
-- database is the Phase 0 observatory and the staging layer that feeds Zoho from Phase 1
-- on. It is never the truth once Zoho writeback is live: it is the message index plus a
-- mirror of the ledger, so reports can run without hammering the Zoho API.
--
-- Contract number is the primary key across every system. Where a row can carry one, it
-- does, in contract_ref.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Transport layer. Email and Telegram are transport, never truth.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mailboxes (
    address           TEXT PRIMARY KEY,
    label             TEXT,
    kind              TEXT NOT NULL CHECK (kind IN ('individual','alias','shared')),
    -- On Namecheap Private Email a shared alias has no delegation model, so we cannot
    -- know who acted. readers is the human answer to that gap until the mail path is
    -- decided. See docs/mail-path-decision.md.
    readers           TEXT,
    owner_person      TEXT,
    enabled           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY,
    source            TEXT NOT NULL CHECK (source IN ('email','telegram','portal','phone_log','manual')),
    mailbox           TEXT REFERENCES mailboxes(address),
    message_id        TEXT,
    thread_key        TEXT NOT NULL,
    in_reply_to       TEXT,
    refs              TEXT,
    from_addr         TEXT,
    from_name         TEXT,
    to_addrs          TEXT,
    cc_addrs          TEXT,
    subject           TEXT,
    sent_at           TEXT NOT NULL,
    direction         TEXT NOT NULL CHECK (direction IN ('inbound','outbound','internal')),
    body_text         TEXT,
    snippet           TEXT,
    has_attachments   INTEGER NOT NULL DEFAULT 0,
    attachment_names  TEXT,
    counterparty      TEXT,
    counterparty_class TEXT,
    contract_ref      TEXT,
    quarantined       INTEGER NOT NULL DEFAULT 0,
    quarantine_reason TEXT,
    raw_ref           TEXT,
    ingested_at       TEXT NOT NULL,
    UNIQUE (source, message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_thread   ON messages(thread_key, sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_sent     ON messages(sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_mailbox  ON messages(mailbox, sent_at);
CREATE INDEX IF NOT EXISTS idx_messages_contract ON messages(contract_ref);

CREATE TABLE IF NOT EXISTS threads (
    thread_key        TEXT PRIMARY KEY,
    subject           TEXT,
    first_at          TEXT,
    last_at           TEXT,
    last_direction    TEXT,
    message_count     INTEGER NOT NULL DEFAULT 0,
    counterparty      TEXT,
    counterparty_class TEXT,
    importance        INTEGER NOT NULL DEFAULT 50,
    contract_ref      TEXT,
    solicitation_ref  TEXT,
    solicitation_close_at TEXT,
    owner_person      TEXT,
    is_external       INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_threads_last ON threads(last_at);

-- ---------------------------------------------------------------------------
-- The ledger. One central object; everything else feeds it or renders it.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS obligations (
    id                INTEGER PRIMARY KEY,
    source            TEXT NOT NULL CHECK (source IN ('email','telegram','portal','phone_log','manual')),
    source_ref        TEXT,
    counterparty      TEXT,
    counterparty_class TEXT CHECK (counterparty_class IN
                        ('gov','oem','distributor','customer_sled','customer_commercial',
                         'internal','service','unknown')),
    contract_ref      TEXT,
    -- Types from the brief, plus the routing keys in config/routing.yaml. The two sets
    -- must stay in sync: `./cb doctor` fails if a routed type is not accepted here.
    type              TEXT NOT NULL CHECK (type IN
                        ('quote_request','award','mod','stop_work','rfi',
                         'delivery_confirmation','invoice','payment','cert_renewal',
                         'internal_request','vendor_chase','solicitation','vendor_quote',
                         'purchase_order','delivery','compliance','hr_payroll',
                         'contract_action')),
    what_is_owed      TEXT NOT NULL,
    direction         TEXT NOT NULL CHECK (direction IN ('we_owe_them','they_owe_us')),
    -- Exactly one named human. Never a group. Enforced in code and by this NOT NULL.
    owner             TEXT NOT NULL,
    due_at            TEXT,
    due_basis         TEXT,
    sla_response_at   TEXT,
    status            TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','waiting_external','blocked','done','dropped')),
    -- waiting_external still has a clock. Waiting on a vendor is not done.
    waiting_since     TEXT,
    next_chase_at     TEXT,
    chase_count       INTEGER NOT NULL DEFAULT 0,
    escalation_level  INTEGER NOT NULL DEFAULT 0,
    escalation_path   TEXT,
    confidence        REAL,
    needs_human_review INTEGER NOT NULL DEFAULT 0,
    zoho_record_id    TEXT,
    zoho_task_id      TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    closed_at         TEXT,
    CHECK (status <> 'waiting_external' OR waiting_since IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_obligations_owner  ON obligations(owner, status);
CREATE INDEX IF NOT EXISTS idx_obligations_due    ON obligations(due_at);
CREATE INDEX IF NOT EXISTS idx_obligations_chase  ON obligations(next_chase_at);

-- Nothing closes without evidence. A close with no evidence row is a bug, and
-- auditor.check_closed_without_evidence reports it nightly.
CREATE TABLE IF NOT EXISTS evidence (
    id                INTEGER PRIMARY KEY,
    obligation_id     INTEGER NOT NULL REFERENCES obligations(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL CHECK (kind IN
                        ('email','drive_doc','zoho_record','portal_receipt','attachment','note')),
    ref               TEXT NOT NULL,
    label             TEXT,
    added_by          TEXT NOT NULL,
    added_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_obligation ON evidence(obligation_id);

-- Every state change, with timestamp and actor.
CREATE TABLE IF NOT EXISTS obligation_audit (
    id                INTEGER PRIMARY KEY,
    obligation_id     INTEGER NOT NULL REFERENCES obligations(id) ON DELETE CASCADE,
    ts                TEXT NOT NULL,
    actor             TEXT NOT NULL,
    field             TEXT,
    old_value         TEXT,
    new_value         TEXT,
    note              TEXT
);

-- ---------------------------------------------------------------------------
-- Extraction and Phase 0 observations
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS extractions (
    id                INTEGER PRIMARY KEY,
    message_id        INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    extractor         TEXT NOT NULL,
    kind              TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    confidence        REAL,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_extractions_message ON extractions(message_id, kind);

-- Commitments made in outbound mail. The Broken Promise Report reads this.
CREATE TABLE IF NOT EXISTS promises (
    id                INTEGER PRIMARY KEY,
    message_id        INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    thread_key        TEXT NOT NULL,
    promised_by       TEXT,
    promised_to       TEXT,
    text              TEXT NOT NULL,
    due_text          TEXT,
    due_at            TEXT,
    confidence        REAL NOT NULL DEFAULT 0,
    detector          TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (status IN ('open','kept','broken','unknown')),
    resolved_by_message_id INTEGER REFERENCES messages(id),
    reviewed_by       TEXT,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_promises_thread ON promises(thread_key);
CREATE INDEX IF NOT EXISTS idx_promises_status ON promises(status, due_at);

-- Internal handoffs. Work passed to a named person.
CREATE TABLE IF NOT EXISTS handoffs (
    id                INTEGER PRIMARY KEY,
    message_id        INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    thread_key        TEXT NOT NULL,
    from_person       TEXT,
    to_person         TEXT NOT NULL,
    text              TEXT NOT NULL,
    confidence        REAL NOT NULL DEFAULT 0,
    detector          TEXT NOT NULL,
    acknowledged_at   TEXT,
    acknowledged_by_message_id INTEGER REFERENCES messages(id),
    crosses_offset_boundary INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_handoffs_to ON handoffs(to_person, acknowledged_at);

-- ---------------------------------------------------------------------------
-- Operational health. A mail sync that stalls quietly is failure mode number one.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sync_health (
    id                INTEGER PRIMARY KEY,
    source            TEXT NOT NULL,
    target            TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    messages_seen     INTEGER NOT NULL DEFAULT 0,
    messages_new      INTEGER NOT NULL DEFAULT 0,
    ok                INTEGER NOT NULL DEFAULT 0,
    error             TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_target ON sync_health(target, started_at);

-- Every automated action, with prompt and output. If this gets audited, this is the
-- defense.
CREATE TABLE IF NOT EXISTS action_log (
    id                INTEGER PRIMARY KEY,
    ts                TEXT NOT NULL,
    actor             TEXT NOT NULL,
    action            TEXT NOT NULL,
    entity            TEXT,
    entity_id         TEXT,
    prompt            TEXT,
    output            TEXT,
    detail_json       TEXT
);

CREATE INDEX IF NOT EXISTS idx_action_log_ts ON action_log(ts);

CREATE TABLE IF NOT EXISTS report_runs (
    id                INTEGER PRIMARY KEY,
    report            TEXT NOT NULL,
    generated_at      TEXT NOT NULL,
    row_count         INTEGER NOT NULL DEFAULT 0,
    params_json       TEXT,
    path              TEXT
);

CREATE TABLE IF NOT EXISTS schema_version (
    version           INTEGER PRIMARY KEY,
    applied_at        TEXT NOT NULL
);
