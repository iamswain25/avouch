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
