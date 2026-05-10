use std::path::Path;

use rusqlite::{params, Connection, OptionalExtension};
use uuid::Uuid;

use crate::{
    error::{Error, Result},
    types::{ActionStatus, QueuedAction},
    MAX_ACTIONS, MAX_ATTEMPTS,
};

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS queued_actions (
    id                  TEXT PRIMARY KEY,
    action_type         TEXT NOT NULL,
    payload             BLOB NOT NULL,
    queued_at           INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending',
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    last_attempted_at   INTEGER,
    last_error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_status_queued
    ON queued_actions(status, queued_at);
"#;

/// SQLite-backed FIFO queue. Single-writer model — callers should serialise
/// access through one [`OfflineQueue`] per process. Internally each public
/// method holds the SQLite connection mutex for the duration of its work.
pub struct OfflineQueue {
    conn: Connection,
    cap: usize,
}

impl OfflineQueue {
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self> {
        let conn = Connection::open(path)?;
        Self::init(conn, MAX_ACTIONS)
    }

    pub fn open_in_memory() -> Result<Self> {
        let conn = Connection::open_in_memory()?;
        Self::init(conn, MAX_ACTIONS)
    }

    /// Override the queue cap. Mostly useful for tests.
    pub fn with_cap<P: AsRef<Path>>(path: P, cap: usize) -> Result<Self> {
        let conn = Connection::open(path)?;
        Self::init(conn, cap)
    }

    fn init(conn: Connection, cap: usize) -> Result<Self> {
        conn.execute_batch(SCHEMA)?;
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;\
             PRAGMA synchronous=NORMAL;\
             PRAGMA temp_store=MEMORY;",
        )?;
        Ok(Self { conn, cap })
    }

    // ── Enqueue ───────────────────────────────────────────────────────────────

    /// Enqueue a new action. Returns the generated id. If the queue is at cap
    /// and at least one row is in `completed` or `failed`, those are pruned to
    /// make room. If still full of `pending`/`in_flight`, [`Error::Full`] is
    /// returned (caller must drain or accept loss).
    pub fn enqueue(
        &mut self,
        action_type: &str,
        payload: &[u8],
        queued_at_ms: i64,
    ) -> Result<String> {
        if action_type.is_empty() {
            return Err(Error::Invalid("action_type is empty".into()));
        }

        let tx = self.conn.transaction()?;

        let total: i64 = tx.query_row("SELECT COUNT(*) FROM queued_actions", [], |r| r.get(0))?;
        if total as usize >= self.cap {
            // Drop completed/failed rows first, oldest first.
            let evicted = tx.execute(
                "DELETE FROM queued_actions
                 WHERE id IN (
                    SELECT id FROM queued_actions
                    WHERE status IN ('completed','failed')
                    ORDER BY queued_at ASC
                    LIMIT ?1
                 )",
                params![(total as usize - self.cap + 1) as i64],
            )?;
            if evicted == 0 {
                return Err(Error::Full { cap: self.cap });
            }
        }

        let id = Uuid::new_v4().to_string();
        tx.execute(
            "INSERT INTO queued_actions (id, action_type, payload, queued_at, status,
                                          attempt_count)
             VALUES (?1, ?2, ?3, ?4, 'pending', 0)",
            params![id, action_type, payload, queued_at_ms],
        )?;
        tx.commit()?;
        Ok(id)
    }

    // ── Claim / replay ────────────────────────────────────────────────────────

    /// Atomically claim up to `limit` pending actions. Selected rows transition
    /// to `in_flight` so concurrent claims don't double-issue them.
    pub fn claim_batch(&mut self, limit: usize, now_ms: i64) -> Result<Vec<QueuedAction>> {
        if limit == 0 {
            return Ok(vec![]);
        }
        let tx = self.conn.transaction()?;

        let ids: Vec<String> = {
            let mut stmt = tx.prepare(
                "SELECT id FROM queued_actions
                 WHERE status = 'pending'
                 ORDER BY queued_at ASC
                 LIMIT ?1",
            )?;
            let rows = stmt.query_map(params![limit as i64], |r| r.get::<_, String>(0))?;
            rows.collect::<rusqlite::Result<Vec<_>>>()?
        };

        if ids.is_empty() {
            return Ok(vec![]);
        }

        // Mark in_flight and bump attempt_count + last_attempted_at.
        for id in &ids {
            tx.execute(
                "UPDATE queued_actions
                 SET status = 'in_flight',
                     attempt_count = attempt_count + 1,
                     last_attempted_at = ?2
                 WHERE id = ?1 AND status = 'pending'",
                params![id, now_ms],
            )?;
        }

        let mut out = Vec::with_capacity(ids.len());
        {
            let mut stmt = tx.prepare(
                "SELECT id, action_type, payload, queued_at, status, attempt_count,
                        last_attempted_at, last_error
                 FROM queued_actions WHERE id = ?1",
            )?;
            for id in &ids {
                let action = stmt.query_row(params![id], row_to_action)?;
                out.push(action);
            }
        }

        tx.commit()?;
        Ok(out)
    }

    /// Mark a previously-claimed action as completed. Idempotent.
    pub fn mark_completed(&mut self, id: &str) -> Result<()> {
        let n = self.conn.execute(
            "UPDATE queued_actions SET status = 'completed', last_error = NULL WHERE id = ?1",
            params![id],
        )?;
        if n == 0 {
            return Err(Error::UnknownAction(id.into()));
        }
        Ok(())
    }

    /// Record a failure for a claimed action. Returns the resulting status:
    /// pending (will be retried) once attempt_count < MAX_ATTEMPTS, otherwise
    /// failed.
    pub fn mark_failed(&mut self, id: &str, error: &str) -> Result<ActionStatus> {
        let attempts: Option<u32> = self
            .conn
            .query_row(
                "SELECT attempt_count FROM queued_actions WHERE id = ?1",
                params![id],
                |r| r.get(0),
            )
            .optional()?;
        let attempts = attempts.ok_or_else(|| Error::UnknownAction(id.into()))?;

        let next_status = if attempts >= MAX_ATTEMPTS {
            ActionStatus::Failed
        } else {
            ActionStatus::Pending
        };

        self.conn.execute(
            "UPDATE queued_actions
             SET status = ?2, last_error = ?3
             WHERE id = ?1",
            params![id, next_status.as_str(), error],
        )?;
        Ok(next_status)
    }

    // ── Maintenance ───────────────────────────────────────────────────────────

    pub fn pending_count(&self) -> Result<usize> {
        let n: i64 = self.conn.query_row(
            "SELECT COUNT(*) FROM queued_actions WHERE status = 'pending'",
            [],
            |r| r.get(0),
        )?;
        Ok(n as usize)
    }

    pub fn count_by_status(&self, status: ActionStatus) -> Result<usize> {
        let n: i64 = self.conn.query_row(
            "SELECT COUNT(*) FROM queued_actions WHERE status = ?1",
            params![status.as_str()],
            |r| r.get(0),
        )?;
        Ok(n as usize)
    }

    /// Remove all completed rows. Run periodically to keep the table small.
    /// Returns the number of rows pruned.
    pub fn purge_completed(&mut self) -> Result<usize> {
        let n = self
            .conn
            .execute("DELETE FROM queued_actions WHERE status = 'completed'", [])?;
        Ok(n)
    }

    /// Reset abandoned `in_flight` rows back to `pending`. Call on startup so
    /// actions claimed during a process that crashed mid-replay are retried.
    pub fn reset_in_flight(&mut self) -> Result<usize> {
        let n = self.conn.execute(
            "UPDATE queued_actions SET status = 'pending'
             WHERE status = 'in_flight'",
            [],
        )?;
        Ok(n)
    }

    pub fn get(&self, id: &str) -> Result<Option<QueuedAction>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, action_type, payload, queued_at, status, attempt_count,
                    last_attempted_at, last_error
             FROM queued_actions WHERE id = ?1",
        )?;
        stmt.query_row(params![id], row_to_action)
            .optional()
            .map_err(Into::into)
    }
}

fn row_to_action(r: &rusqlite::Row<'_>) -> rusqlite::Result<QueuedAction> {
    let status_str: String = r.get(4)?;
    Ok(QueuedAction {
        id: r.get(0)?,
        action_type: r.get(1)?,
        payload: r.get(2)?,
        queued_at: r.get(3)?,
        status: ActionStatus::parse(&status_str).unwrap_or(ActionStatus::Pending),
        attempt_count: r.get::<_, i64>(5)? as u32,
        last_attempted_at: r.get(6)?,
        last_error: r.get(7)?,
    })
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn q() -> OfflineQueue {
        OfflineQueue::open_in_memory().unwrap()
    }

    #[test]
    fn enqueue_then_pending_count() {
        let mut q = q();
        q.enqueue("agent.execute", b"{}", 100).unwrap();
        q.enqueue("tool.call", b"{}", 200).unwrap();
        assert_eq!(q.pending_count().unwrap(), 2);
    }

    #[test]
    fn enqueue_rejects_empty_action_type() {
        let mut q = q();
        let err = q.enqueue("", b"{}", 100).unwrap_err();
        assert!(matches!(err, Error::Invalid(_)));
    }

    #[test]
    fn claim_batch_orders_by_queued_at() {
        let mut q = q();
        let _id_a = q.enqueue("a", b"a", 100).unwrap();
        let _id_b = q.enqueue("b", b"b", 50).unwrap();
        let claimed = q.claim_batch(2, 999).unwrap();
        assert_eq!(claimed[0].action_type, "b");
        assert_eq!(claimed[1].action_type, "a");
        assert_eq!(claimed[0].status, ActionStatus::InFlight);
        assert_eq!(claimed[0].attempt_count, 1);
        assert_eq!(claimed[0].last_attempted_at, Some(999));
    }

    #[test]
    fn claim_does_not_double_issue() {
        let mut q = q();
        q.enqueue("a", b"a", 100).unwrap();
        let first = q.claim_batch(10, 1).unwrap();
        let second = q.claim_batch(10, 2).unwrap();
        assert_eq!(first.len(), 1);
        assert_eq!(second.len(), 0);
    }

    #[test]
    fn mark_completed_drops_from_pending() {
        let mut q = q();
        q.enqueue("a", b"a", 100).unwrap();
        let claimed = q.claim_batch(1, 1).unwrap();
        q.mark_completed(&claimed[0].id).unwrap();
        assert_eq!(q.pending_count().unwrap(), 0);
        assert_eq!(q.count_by_status(ActionStatus::Completed).unwrap(), 1);
    }

    #[test]
    fn mark_completed_unknown_errors() {
        let mut q = q();
        let err = q.mark_completed("nope").unwrap_err();
        assert!(matches!(err, Error::UnknownAction(_)));
    }

    #[test]
    fn mark_failed_retries_until_max() {
        let mut q = q();
        let id = q.enqueue("a", b"a", 100).unwrap();
        for attempt in 1..=MAX_ATTEMPTS {
            q.claim_batch(1, attempt as i64).unwrap();
            let status = q.mark_failed(&id, "boom").unwrap();
            if attempt < MAX_ATTEMPTS {
                assert_eq!(status, ActionStatus::Pending);
            } else {
                assert_eq!(status, ActionStatus::Failed);
            }
        }
        assert_eq!(q.count_by_status(ActionStatus::Failed).unwrap(), 1);
        assert_eq!(q.pending_count().unwrap(), 0);
    }

    #[test]
    fn purge_completed_clears_done_rows() {
        let mut q = q();
        let id = q.enqueue("a", b"a", 100).unwrap();
        q.claim_batch(1, 1).unwrap();
        q.mark_completed(&id).unwrap();
        let pruned = q.purge_completed().unwrap();
        assert_eq!(pruned, 1);
        assert_eq!(q.count_by_status(ActionStatus::Completed).unwrap(), 0);
    }

    #[test]
    fn reset_in_flight_revives_pending() {
        let mut q = q();
        q.enqueue("a", b"a", 100).unwrap();
        q.claim_batch(1, 1).unwrap();
        assert_eq!(q.pending_count().unwrap(), 0);
        let n = q.reset_in_flight().unwrap();
        assert_eq!(n, 1);
        assert_eq!(q.pending_count().unwrap(), 1);
    }

    #[test]
    fn cap_evicts_completed_rows_first() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("q.db");
        let mut q = OfflineQueue::with_cap(&path, 3).unwrap();

        let id_a = q.enqueue("a", b"a", 100).unwrap();
        q.enqueue("b", b"b", 200).unwrap();
        q.enqueue("c", b"c", 300).unwrap();
        // Mark one completed so it is eligible for eviction.
        q.claim_batch(1, 1).unwrap();
        q.mark_completed(&id_a).unwrap();

        // Now full at 3 (1 completed + 2 pending). Adding one more evicts the
        // completed row to make space.
        q.enqueue("d", b"d", 400).unwrap();
        assert!(q.get(&id_a).unwrap().is_none());
    }

    #[test]
    fn cap_returns_full_when_no_evictable_rows() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("q.db");
        let mut q = OfflineQueue::with_cap(&path, 2).unwrap();
        q.enqueue("a", b"a", 100).unwrap();
        q.enqueue("b", b"b", 200).unwrap();
        let err = q.enqueue("c", b"c", 300).unwrap_err();
        assert!(matches!(err, Error::Full { cap: 2 }));
    }

    #[test]
    fn get_round_trips_payload() {
        let mut q = q();
        let id = q.enqueue("a", b"hello", 100).unwrap();
        let action = q.get(&id).unwrap().unwrap();
        assert_eq!(action.payload, b"hello");
        assert_eq!(action.action_type, "a");
        assert_eq!(action.status, ActionStatus::Pending);
        assert_eq!(action.attempt_count, 0);
    }

    #[test]
    fn claim_batch_zero_returns_empty() {
        let mut q = q();
        q.enqueue("a", b"a", 100).unwrap();
        let claimed = q.claim_batch(0, 1).unwrap();
        assert!(claimed.is_empty());
    }
}
