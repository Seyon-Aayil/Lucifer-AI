// ─────────────────────────────────────────────────────────────────────────────
// 0002_last_accessed_at.cypher
//
// ADR-011 — memory decay fix.
//
// The previous decay pass multiplied the already-decayed score by
// exp(-rate * days_since_write), compounding nightly to exp(-rate * N(N+1)/2).
// Effective horizon was ~8 days against a documented ~30, and `lastAccessedAt`
// never existed, so reads did not keep a node alive.
//
// This migration:
//   1. Indexes lastAccessedAt (the decay pass and touch_nodes both filter on it)
//   2. Backfills lastAccessedAt = updatedAt for existing nodes
//   3. AUDITS soft-deleted nodes that the bug killed early
//   4. Resurrects them — GATED, commented out by default. Read §3 first.
//
// Run:  ./infra/neo4j/migrate.sh
// ─────────────────────────────────────────────────────────────────────────────


// ── 1. Index ─────────────────────────────────────────────────────────────────
// touch_nodes() filters on lastAccessedAt on every context build; the nightly
// decay pass scans it. Without this index both degrade to full scans.

CREATE INDEX node_last_accessed_at IF NOT EXISTS
FOR (n:Person) ON (n.lastAccessedAt);

CREATE INDEX node_deleted_at IF NOT EXISTS
FOR (n:Person) ON (n.deletedAt);


// ── 2. Backfill ──────────────────────────────────────────────────────────────
// Seed lastAccessedAt from updatedAt so no live node is scored against a NULL
// reference on the first post-deploy run. Without this, every node's reference
// timestamp is its write time — correct, but it discards nothing and costs one
// pass, so do it explicitly rather than relying on the _latest() NULL fallback.

MATCH (n)
WHERE n.deletedAt IS NULL
  AND n.lastAccessedAt IS NULL
  AND n.updatedAt IS NOT NULL
SET n.lastAccessedAt = n.updatedAt;


// ── 3. AUDIT — run this BEFORE the resurrection below ────────────────────────
// Counts nodes soft-deleted while the bug was live whose true horizon had NOT
// expired. These are false deletions: personal data destroyed by arithmetic.
//
//   Correct horizon: exp(-ln(2)/7 * d) < 0.05  ⟺  d > ~30.3 days
//   Buggy horizon:   ~8 days
//
// A node is a false-deletion candidate if it was soft-deleted while its
// updatedAt was less than 30.3 days before its deletedAt.
//
// REPORT THIS COUNT IN THE PR DESCRIPTION (W7-4 DoD). Do not skip to §4.

MATCH (n)
WHERE n.deletedAt IS NOT NULL
  AND n.updatedAt IS NOT NULL
  AND duration.inSeconds(n.updatedAt, n.deletedAt).days < 31
RETURN
  labels(n)[0]                                        AS node_type,
  count(n)                                            AS false_deletions,
  min(duration.inSeconds(n.updatedAt, n.deletedAt).days) AS min_age_days,
  max(duration.inSeconds(n.updatedAt, n.deletedAt).days) AS max_age_days
ORDER BY false_deletions DESC;


// ── 4. RESURRECTION — GATED. Uncomment only after reviewing §3 output ────────
//
// Restores nodes soft-deleted before their true horizon. Safe because the
// implementation only ever soft-deleted (deletedAt set, no DETACH DELETE) —
// the nodes and all their edges are intact. This is the entire reason ADR-011
// is recoverable rather than a post-mortem.
//
// Scoped to the last 90 days to avoid resurrecting nodes deleted for legitimate
// reasons long ago. Adjust the window against the §3 audit if needed.
//
// Resurrected nodes get lastAccessedAt = deletedAt, so they re-enter with a
// fresh ~30-day horizon rather than being killed again on the next nightly run.
//
// ⚠️  REVIEW THE §3 COUNT FIRST. If it is implausibly large, stop and
//     investigate before writing — a bad resurrection is harder to undo than a
//     bad deletion, because it revives nodes the user may have deleted on purpose.
//
// MATCH (n)
// WHERE n.deletedAt IS NOT NULL
//   AND n.updatedAt IS NOT NULL
//   AND duration.inSeconds(n.updatedAt, n.deletedAt).days < 31
//   AND duration.inSeconds(n.deletedAt, datetime()).days <= 90
// SET n.lastAccessedAt = n.deletedAt,
//     n.decayScore     = 1.0,
//     n.resurrectedAt  = datetime(),
//     n.resurrectedBy  = 'ADR-011'
// REMOVE n.deletedAt
// RETURN count(n) AS resurrected;


// ── 5. Post-resurrection verification ────────────────────────────────────────
// Expect: 0 rows. Any row means a resurrected node is still inside the buggy
// horizon and would be re-killed on the next nightly pass.
//
// MATCH (n)
// WHERE n.resurrectedBy = 'ADR-011'
//   AND n.lastAccessedAt < datetime() - duration({days: 31})
// RETURN count(n) AS would_be_rekilled;
