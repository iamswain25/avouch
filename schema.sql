-- Avouch board (D1). Content lives here; bonds live on Algorand.
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
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_hash ON posts(content_hash);
