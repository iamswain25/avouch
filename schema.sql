-- Avouch board (D1, v4 slot auction). Bodies live here; money lives on Algorand.
--
-- The chain is the source of truth for stake, epoch and which hash holds the slot.
-- These tables are a body store plus a slot registry: the API reads every listed slot's
-- box from algod and returns what the chain says. Nothing here can move money.
--
-- posts (v1-v3) is kept only so old rows are not destroyed; nothing reads it.

CREATE TABLE IF NOT EXISTS slots (
  slot_id     TEXT PRIMARY KEY,           -- uint64 as a decimal string
  question    TEXT NOT NULL DEFAULT '',   -- optional human-readable question
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slots_updated ON slots(updated_at DESC);

CREATE TABLE IF NOT EXISTS answers (
  slot_id       TEXT NOT NULL,
  content_hash  TEXT NOT NULL,            -- sha256:<hex>, must hash the body
  title         TEXT NOT NULL DEFAULT '',
  body          TEXT NOT NULL,
  author        TEXT NOT NULL DEFAULT '', -- address that asked for the plan, not proof of stake
  created_at    INTEGER NOT NULL,
  PRIMARY KEY (slot_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_answers_slot ON answers(slot_id);


-- Read revenue: one row per paid read, settled in batches to the answer that served it.
CREATE TABLE IF NOT EXISTS reads (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  slot_id            TEXT NOT NULL,
  content_hash       TEXT NOT NULL,   -- the answer that was served at read time
  payer              TEXT NOT NULL DEFAULT '',
  txid               TEXT NOT NULL,
  price_micro_algo   INTEGER NOT NULL,
  maintainer_micro_algo INTEGER NOT NULL,  -- the share that goes to the answer's backers
  created_at         INTEGER NOT NULL,
  batch_id           TEXT             -- NULL until settled; set to the settlement txid
);
CREATE INDEX IF NOT EXISTS idx_reads_pending ON reads(created_at) WHERE batch_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_reads_slot ON reads(slot_id, content_hash);

CREATE TABLE IF NOT EXISTS settlements (
  batch_id            TEXT PRIMARY KEY,   -- settlement transaction id
  created_at          INTEGER NOT NULL,
  groups              INTEGER NOT NULL,
  amount_micro_algo   INTEGER NOT NULL,
  txids               TEXT NOT NULL
);

-- v1-v3 board rows. Kept for history; no code path reads or writes it.
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY,
  author TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  bond_micro_algo INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'UNBONDED',
  created_at INTEGER NOT NULL,
  window_seconds INTEGER NOT NULL,
  txid TEXT
);

-- Prepaid read credit. One deposit funds many reads; the edge debits it off chain and the
-- maintainer share still settles in batches (see reads). Custodial: the operator holds the
-- deposit in the pay-to account until it is refunded, which is stated in the whitepaper.
CREATE TABLE IF NOT EXISTS credits (
  credit_id           TEXT PRIMARY KEY,
  payer               TEXT NOT NULL DEFAULT '',
  deposited_micro_algo INTEGER NOT NULL,
  balance_micro_algo  INTEGER NOT NULL DEFAULT 0,
  deposit_txid        TEXT UNIQUE,
  reads               INTEGER NOT NULL DEFAULT 0,
  created_at          INTEGER NOT NULL,
  updated_at          INTEGER NOT NULL,
  expires_at          INTEGER NOT NULL,
  refunded_txid       TEXT
);
CREATE INDEX IF NOT EXISTS idx_credits_payer ON credits(payer);
CREATE INDEX IF NOT EXISTS idx_credits_open ON credits(expires_at) WHERE balance_micro_algo > 0;

-- One row per on-chain payment the edge has accepted. A payment id is a bearer token: without
-- this table the same transfer could be replayed for another read, especially now that a
-- re-submitted transaction is recognised instead of failing.
CREATE TABLE IF NOT EXISTS payments (
  txid        TEXT PRIMARY KEY,
  resource    TEXT NOT NULL,
  payer       TEXT,
  amount      INTEGER,
  created_at  INTEGER NOT NULL
);
