use std::{path::Path, sync::Once};

use rusqlite::{ffi::sqlite3_auto_extension, params, Connection};

use crate::{
    conversations::{self, Conversation, Message},
    error::{Error, Result},
    manifest::{EdgeDelta, NodeDelta, SubgraphManifest},
    EMBEDDING_DIM,
};

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS nodes (
    id              TEXT PRIMARY KEY,
    node_type       TEXT NOT NULL,
    classification  TEXT NOT NULL DEFAULT 'standard',
    payload         BLOB NOT NULL,
    updated_at      INTEGER NOT NULL,
    decay_score     REAL NOT NULL DEFAULT 1.0,
    source_agent    TEXT NOT NULL DEFAULT '',
    deleted_at      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_nodes_classification ON nodes(classification) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_nodes_updated ON nodes(updated_at) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS edges (
    id            TEXT PRIMARY KEY,
    from_id       TEXT NOT NULL,
    to_id         TEXT NOT NULL,
    relation      TEXT NOT NULL,
    weight        REAL NOT NULL DEFAULT 1.0,
    valid_from    INTEGER NOT NULL DEFAULT 0,
    valid_until   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_id);
CREATE INDEX IF NOT EXISTS idx_edges_relation ON edges(relation);

CREATE TABLE IF NOT EXISTS sync_manifest (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    last_sync_at    INTEGER NOT NULL,
    manifest_hash   TEXT NOT NULL,
    full_sync       INTEGER NOT NULL DEFAULT 0
);
"#;

/// Edge-side knowledge-graph store. Wraps a single SQLite connection with
/// `sqlite-vec` loaded for embedding queries.
pub struct EdgeStore {
    conn: Connection,
}

impl EdgeStore {
    /// Open a store at `path`. Creates the file if missing, runs migrations,
    /// loads the bundled `sqlite-vec` extension, and registers the vec table.
    pub fn open<P: AsRef<Path>>(path: P) -> Result<Self> {
        register_vec_extension();
        let conn = Connection::open(path)?;
        Self::init(conn)
    }

    /// In-memory store, useful for tests and ephemeral session caches.
    pub fn open_in_memory() -> Result<Self> {
        register_vec_extension();
        let conn = Connection::open_in_memory()?;
        Self::init(conn)
    }

    fn init(conn: Connection) -> Result<Self> {
        conn.execute_batch(SCHEMA)?;
        conn.execute_batch(conversations::SCHEMA)?;
        // The vec virtual table sits alongside `nodes` and is keyed by `rowid`.
        let create_vec = format!(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_nodes USING vec0(embedding float[{EMBEDDING_DIM}])"
        );
        conn.execute_batch(&create_vec)?;

        // Pragmas for an embedded write-mostly workload.
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;\
             PRAGMA synchronous=NORMAL;\
             PRAGMA temp_store=MEMORY;\
             PRAGMA foreign_keys=ON;",
        )?;
        Ok(Self { conn })
    }

    // ── Manifest application ─────────────────────────────────────────────────

    /// Apply a hot-subgraph manifest atomically. On full sync the existing
    /// rows are wiped first; on incremental sync deltas are merged.
    pub fn apply_manifest(&mut self, manifest: &SubgraphManifest) -> Result<()> {
        let tx = self.conn.transaction()?;

        if manifest.is_full_sync {
            tx.execute("DELETE FROM nodes", [])?;
            tx.execute("DELETE FROM edges", [])?;
            tx.execute("DELETE FROM vec_nodes", [])?;
        }

        for n in &manifest.nodes {
            apply_node_delta(&tx, n)?;
        }
        for e in &manifest.edges {
            apply_edge_delta(&tx, e)?;
        }

        tx.execute(
            "INSERT INTO sync_manifest (id, last_sync_at, manifest_hash, full_sync)
             VALUES (1, ?1, ?2, ?3)
             ON CONFLICT(id) DO UPDATE SET
                last_sync_at = excluded.last_sync_at,
                manifest_hash = excluded.manifest_hash,
                full_sync = excluded.full_sync",
            params![
                manifest.generated_at,
                manifest.manifest_hash,
                manifest.is_full_sync as i32,
            ],
        )?;

        tx.commit()?;
        Ok(())
    }

    // ── Direct mutators ──────────────────────────────────────────────────────

    pub fn upsert_node(&mut self, delta: &NodeDelta) -> Result<()> {
        apply_node_delta(&self.conn, delta)
    }

    pub fn upsert_edge(&mut self, delta: &EdgeDelta) -> Result<()> {
        apply_edge_delta(&self.conn, delta)
    }

    pub fn soft_delete_node(&mut self, node_id: &str, deleted_at: i64) -> Result<()> {
        let n = self.conn.execute(
            "UPDATE nodes SET deleted_at = ?2 WHERE id = ?1",
            params![node_id, deleted_at],
        )?;
        if n == 0 {
            return Err(Error::Invalid(format!("unknown node_id {node_id}")));
        }
        Ok(())
    }

    /// Attach an embedding to a node. Length must equal [`EMBEDDING_DIM`].
    pub fn upsert_embedding(&mut self, node_id: &str, embedding: &[f32]) -> Result<()> {
        if embedding.len() != EMBEDDING_DIM {
            return Err(Error::InvalidEmbedding {
                expected: EMBEDDING_DIM,
                got: embedding.len(),
            });
        }
        let rowid: i64 = self
            .conn
            .query_row("SELECT rowid FROM nodes WHERE id = ?1", [node_id], |r| {
                r.get(0)
            })
            .map_err(|_| Error::Invalid(format!("unknown node_id {node_id}")))?;

        // sqlite-vec accepts BLOBs of contiguous f32 little-endian bytes.
        // vec0 virtual tables do not support UPSERT, so delete-then-insert.
        let bytes: &[u8] = bytemuck_cast(embedding);
        let tx = self.conn.unchecked_transaction()?;
        tx.execute("DELETE FROM vec_nodes WHERE rowid = ?1", params![rowid])?;
        tx.execute(
            "INSERT INTO vec_nodes (rowid, embedding) VALUES (?1, ?2)",
            params![rowid, bytes],
        )?;
        tx.commit()?;
        Ok(())
    }

    // ── Queries ──────────────────────────────────────────────────────────────

    pub fn count_nodes(&self) -> Result<usize> {
        let n: i64 = self.conn.query_row(
            "SELECT COUNT(*) FROM nodes WHERE deleted_at IS NULL",
            [],
            |r| r.get(0),
        )?;
        Ok(n as usize)
    }

    pub fn count_edges(&self) -> Result<usize> {
        let n: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM edges", [], |r| r.get(0))?;
        Ok(n as usize)
    }

    pub fn list_by_type(&self, node_type: &str, limit: usize) -> Result<Vec<NodeDelta>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, node_type, classification, payload, updated_at, source_agent
             FROM nodes
             WHERE node_type = ?1 AND deleted_at IS NULL
             ORDER BY updated_at DESC
             LIMIT ?2",
        )?;
        let rows = stmt.query_map(params![node_type, limit as i64], |r| {
            let payload_blob: Vec<u8> = r.get(3)?;
            let payload: serde_json::Value =
                serde_json::from_slice(&payload_blob).unwrap_or(serde_json::Value::Null);
            Ok(NodeDelta {
                node_id: r.get(0)?,
                operation: "upsert".into(),
                node_type: r.get(1)?,
                classification: r.get(2)?,
                payload,
                updated_at: r.get(4)?,
                source_agent: r.get(5)?,
            })
        })?;
        rows.collect::<rusqlite::Result<Vec<_>>>()
            .map_err(Into::into)
    }

    /// k-NN over node embeddings. Returns `(node_id, distance)` pairs ascending.
    pub fn vector_search(&self, query: &[f32], k: usize) -> Result<Vec<(String, f64)>> {
        if query.len() != EMBEDDING_DIM {
            return Err(Error::InvalidEmbedding {
                expected: EMBEDDING_DIM,
                got: query.len(),
            });
        }
        // vec0 requires `k = ?` (or a literal LIMIT) to be present *on the
        // virtual table itself* — we can't rely on it leaking through a JOIN.
        // Resolve nearest rowids first, then look up the node ids.
        let bytes: &[u8] = bytemuck_cast(query);
        let mut stmt = self.conn.prepare(
            "SELECT rowid, distance
             FROM vec_nodes
             WHERE embedding MATCH ?1 AND k = ?2
             ORDER BY distance",
        )?;
        let rowid_rows = stmt
            .query_map(params![bytes, k as i64], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, f64>(1)?))
            })?
            .collect::<rusqlite::Result<Vec<(i64, f64)>>>()?;

        let mut out = Vec::with_capacity(rowid_rows.len());
        for (rowid, distance) in rowid_rows {
            let id: Option<String> = self
                .conn
                .query_row(
                    "SELECT id FROM nodes WHERE rowid = ?1 AND deleted_at IS NULL",
                    [rowid],
                    |r| r.get(0),
                )
                .ok();
            if let Some(id) = id {
                out.push((id, distance));
            }
        }
        Ok(out)
    }

    // ── Conversations ───────────────────────────────────────────────────────

    /// Create a new conversation. Returns the row's id.
    pub fn create_conversation(&self, id: &str, title: &str, now_ms: i64) -> Result<()> {
        if id.is_empty() {
            return Err(Error::Invalid("conversation id is empty".into()));
        }
        self.conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at)
             VALUES (?1, ?2, ?3, ?3)",
            params![id, title, now_ms],
        )?;
        Ok(())
    }

    pub fn rename_conversation(&self, id: &str, title: &str, now_ms: i64) -> Result<()> {
        let n = self.conn.execute(
            "UPDATE conversations SET title = ?2, updated_at = ?3 WHERE id = ?1",
            params![id, title, now_ms],
        )?;
        if n == 0 {
            return Err(Error::Invalid(format!("unknown conversation_id {id}")));
        }
        Ok(())
    }

    pub fn delete_conversation(&self, id: &str) -> Result<()> {
        let n = self
            .conn
            .execute("DELETE FROM conversations WHERE id = ?1", params![id])?;
        if n == 0 {
            return Err(Error::Invalid(format!("unknown conversation_id {id}")));
        }
        Ok(())
    }

    pub fn list_conversations(&self, limit: usize) -> Result<Vec<Conversation>> {
        let mut stmt = self.conn.prepare(
            "SELECT c.id, c.title, c.created_at, c.updated_at,
                    (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS n
             FROM conversations c
             ORDER BY c.updated_at DESC
             LIMIT ?1",
        )?;
        let rows = stmt.query_map(params![limit as i64], |r| {
            Ok(Conversation {
                id: r.get(0)?,
                title: r.get(1)?,
                created_at: r.get(2)?,
                updated_at: r.get(3)?,
                message_count: r.get::<_, i64>(4)? as usize,
            })
        })?;
        rows.collect::<rusqlite::Result<Vec<_>>>()
            .map_err(Into::into)
    }

    /// Append a message to an existing conversation. Bumps the parent
    /// conversation's `updated_at` so list ordering follows recency.
    pub fn append_message(&self, msg: &Message) -> Result<()> {
        if msg.conversation_id.is_empty() || msg.id.is_empty() {
            return Err(Error::Invalid(
                "message id / conversation_id required".into(),
            ));
        }
        let tx = self.conn.unchecked_transaction()?;
        let exists: i64 = tx.query_row(
            "SELECT COUNT(*) FROM conversations WHERE id = ?1",
            [&msg.conversation_id],
            |r| r.get(0),
        )?;
        if exists == 0 {
            return Err(Error::Invalid(format!(
                "unknown conversation_id {}",
                msg.conversation_id
            )));
        }
        tx.execute(
            "INSERT INTO messages (id, conversation_id, role, content, agent_id, model,
                                    tokens, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![
                msg.id,
                msg.conversation_id,
                msg.role,
                msg.content,
                msg.agent_id,
                msg.model,
                msg.tokens.map(i64::from),
                msg.created_at,
            ],
        )?;
        tx.execute(
            "UPDATE conversations SET updated_at = ?2 WHERE id = ?1",
            params![msg.conversation_id, msg.created_at],
        )?;
        tx.commit()?;
        Ok(())
    }

    pub fn list_messages(&self, conversation_id: &str, limit: usize) -> Result<Vec<Message>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, conversation_id, role, content, agent_id, model, tokens, created_at
             FROM messages
             WHERE conversation_id = ?1
             ORDER BY created_at ASC
             LIMIT ?2",
        )?;
        let rows = stmt.query_map(params![conversation_id, limit as i64], |r| {
            Ok(Message {
                id: r.get(0)?,
                conversation_id: r.get(1)?,
                role: r.get(2)?,
                content: r.get(3)?,
                agent_id: r.get(4)?,
                model: r.get(5)?,
                tokens: r.get::<_, Option<i64>>(6)?.map(|v| v as u32),
                created_at: r.get(7)?,
            })
        })?;
        rows.collect::<rusqlite::Result<Vec<_>>>()
            .map_err(Into::into)
    }

    pub fn count_conversations(&self) -> Result<usize> {
        let n: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM conversations", [], |r| r.get(0))?;
        Ok(n as usize)
    }

    pub fn last_sync_at(&self) -> Result<Option<i64>> {
        let r = self
            .conn
            .query_row(
                "SELECT last_sync_at FROM sync_manifest WHERE id = 1",
                [],
                |r| r.get::<_, i64>(0),
            )
            .ok();
        Ok(r)
    }
}

// ── Internals ────────────────────────────────────────────────────────────────

/// One-shot registration of the sqlite-vec C extension as a SQLite
/// auto-extension. The bundled `sqlite_vec` crate exposes the entry point as a
/// raw C symbol; we transmute it to the function-pointer shape SQLite expects.
fn register_vec_extension() {
    static INIT: Once = Once::new();
    INIT.call_once(|| {
        // SAFETY: `sqlite3_vec_init` is a C function with an empty signature
        // bundled by the `sqlite_vec` crate. We transmute its address to the
        // shape SQLite's `sqlite3_auto_extension` expects (a `Result`-returning
        // C ABI fn). Both pointers are word-sized; the SQLite call only stores
        // it for later invocation. No aliasing or threading concerns at this
        // layer.
        unsafe {
            let entry = sqlite_vec::sqlite3_vec_init as *const ();
            let init: unsafe extern "C" fn(
                *mut rusqlite::ffi::sqlite3,
                *mut *mut std::os::raw::c_char,
                *const rusqlite::ffi::sqlite3_api_routines,
            ) -> std::os::raw::c_int = std::mem::transmute(entry);
            sqlite3_auto_extension(Some(init));
        }
    });
}

fn apply_node_delta(conn: &Connection, n: &NodeDelta) -> Result<()> {
    match n.operation.as_str() {
        "upsert" => {
            let payload_bytes = serde_json::to_vec(&n.payload)?;
            conn.execute(
                "INSERT INTO nodes (id, node_type, classification, payload, updated_at,
                                    source_agent, deleted_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, NULL)
                 ON CONFLICT(id) DO UPDATE SET
                    node_type = excluded.node_type,
                    classification = excluded.classification,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at,
                    source_agent = excluded.source_agent,
                    deleted_at = NULL",
                params![
                    n.node_id,
                    n.node_type,
                    n.classification,
                    payload_bytes,
                    n.updated_at,
                    n.source_agent,
                ],
            )?;
        }
        "soft_delete" => {
            conn.execute(
                "UPDATE nodes SET deleted_at = ?2 WHERE id = ?1",
                params![n.node_id, n.updated_at],
            )?;
        }
        other => return Err(Error::Invalid(format!("unknown node op '{other}'"))),
    }
    Ok(())
}

fn apply_edge_delta(conn: &Connection, e: &EdgeDelta) -> Result<()> {
    match e.operation.as_str() {
        "upsert" => {
            conn.execute(
                "INSERT INTO edges (id, from_id, to_id, relation, weight, valid_from, valid_until)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
                 ON CONFLICT(id) DO UPDATE SET
                    from_id = excluded.from_id,
                    to_id = excluded.to_id,
                    relation = excluded.relation,
                    weight = excluded.weight,
                    valid_from = excluded.valid_from,
                    valid_until = excluded.valid_until",
                params![
                    e.edge_id,
                    e.from_node_id,
                    e.to_node_id,
                    e.relation,
                    e.weight,
                    e.valid_from,
                    e.valid_until,
                ],
            )?;
        }
        "delete" => {
            conn.execute("DELETE FROM edges WHERE id = ?1", params![e.edge_id])?;
        }
        other => return Err(Error::Invalid(format!("unknown edge op '{other}'"))),
    }
    Ok(())
}

/// Cast `&[f32]` to `&[u8]` with little-endian byte layout. Avoids pulling in a
/// dependency just for one cast — sqlite-vec expects platform-native bytes and
/// our embeddings are produced on the same architecture they're queried on.
fn bytemuck_cast(slice: &[f32]) -> &[u8] {
    // SAFETY: f32 is `Copy` + has well-defined bit pattern; the resulting byte
    // slice has the same lifetime as the source and is read-only for the
    // duration of the SQLite call. sqlite-vec treats it opaquely.
    unsafe { std::slice::from_raw_parts(slice.as_ptr() as *const u8, std::mem::size_of_val(slice)) }
}

// ── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn n(id: &str, ty: &str, op: &str, ts: i64) -> NodeDelta {
        NodeDelta {
            node_id: id.into(),
            operation: op.into(),
            node_type: ty.into(),
            classification: "standard".into(),
            payload: serde_json::json!({"name": id}),
            updated_at: ts,
            source_agent: "personal-agent".into(),
        }
    }

    fn e(id: &str, from: &str, to: &str) -> EdgeDelta {
        EdgeDelta {
            edge_id: id.into(),
            operation: "upsert".into(),
            from_node_id: from.into(),
            to_node_id: to.into(),
            relation: "RELATED_TO".into(),
            weight: 1.0,
            valid_from: 0,
            valid_until: 0,
        }
    }

    #[test]
    fn open_in_memory_initializes_schema() {
        let s = EdgeStore::open_in_memory().unwrap();
        assert_eq!(s.count_nodes().unwrap(), 0);
        assert_eq!(s.count_edges().unwrap(), 0);
    }

    #[test]
    fn upsert_then_query_by_type() {
        let mut s = EdgeStore::open_in_memory().unwrap();
        s.upsert_node(&n("a", "Person", "upsert", 100)).unwrap();
        s.upsert_node(&n("b", "Person", "upsert", 200)).unwrap();
        s.upsert_node(&n("c", "Place", "upsert", 300)).unwrap();
        let people = s.list_by_type("Person", 10).unwrap();
        assert_eq!(people.len(), 2);
        // Ordered by updated_at DESC
        assert_eq!(people[0].node_id, "b");
    }

    #[test]
    fn upsert_is_idempotent() {
        let mut s = EdgeStore::open_in_memory().unwrap();
        s.upsert_node(&n("a", "Person", "upsert", 100)).unwrap();
        s.upsert_node(&n("a", "Person", "upsert", 200)).unwrap();
        assert_eq!(s.count_nodes().unwrap(), 1);
    }

    #[test]
    fn soft_delete_hides_node() {
        let mut s = EdgeStore::open_in_memory().unwrap();
        s.upsert_node(&n("a", "Person", "upsert", 100)).unwrap();
        s.soft_delete_node("a", 200).unwrap();
        assert_eq!(s.count_nodes().unwrap(), 0);
        assert_eq!(s.list_by_type("Person", 10).unwrap().len(), 0);
    }

    #[test]
    fn soft_delete_unknown_errors() {
        let mut s = EdgeStore::open_in_memory().unwrap();
        let err = s.soft_delete_node("missing", 0).unwrap_err();
        assert!(matches!(err, Error::Invalid(_)));
    }

    #[test]
    fn full_sync_wipes_existing() {
        let mut s = EdgeStore::open_in_memory().unwrap();
        s.upsert_node(&n("old", "Person", "upsert", 100)).unwrap();

        let manifest = SubgraphManifest {
            nodes: vec![n("new", "Person", "upsert", 200)],
            edges: vec![],
            generated_at: 200,
            manifest_hash: "h1".into(),
            is_full_sync: true,
        };
        s.apply_manifest(&manifest).unwrap();
        let people = s.list_by_type("Person", 10).unwrap();
        assert_eq!(people.len(), 1);
        assert_eq!(people[0].node_id, "new");
    }

    #[test]
    fn incremental_sync_merges() {
        let mut s = EdgeStore::open_in_memory().unwrap();
        s.upsert_node(&n("keep", "Person", "upsert", 100)).unwrap();

        let manifest = SubgraphManifest {
            nodes: vec![n("added", "Person", "upsert", 200)],
            edges: vec![e("e1", "keep", "added")],
            generated_at: 200,
            manifest_hash: "h2".into(),
            is_full_sync: false,
        };
        s.apply_manifest(&manifest).unwrap();
        assert_eq!(s.count_nodes().unwrap(), 2);
        assert_eq!(s.count_edges().unwrap(), 1);
    }

    #[test]
    fn manifest_records_last_sync_at() {
        let mut s = EdgeStore::open_in_memory().unwrap();
        let manifest = SubgraphManifest {
            nodes: vec![],
            edges: vec![],
            generated_at: 12345,
            manifest_hash: "h".into(),
            is_full_sync: true,
        };
        s.apply_manifest(&manifest).unwrap();
        assert_eq!(s.last_sync_at().unwrap(), Some(12345));
    }

    #[test]
    fn edge_upsert_and_delete() {
        let mut s = EdgeStore::open_in_memory().unwrap();
        s.upsert_edge(&e("e1", "a", "b")).unwrap();
        assert_eq!(s.count_edges().unwrap(), 1);

        let del = EdgeDelta {
            operation: "delete".into(),
            ..e("e1", "a", "b")
        };
        s.upsert_edge(&del).unwrap();
        assert_eq!(s.count_edges().unwrap(), 0);
    }

    #[test]
    fn embedding_dim_is_validated() {
        let mut s = EdgeStore::open_in_memory().unwrap();
        s.upsert_node(&n("a", "Person", "upsert", 100)).unwrap();
        let err = s.upsert_embedding("a", &[0.0_f32; 3]).unwrap_err();
        assert!(matches!(err, Error::InvalidEmbedding { .. }));
    }

    #[test]
    fn vector_search_returns_nearest() {
        let mut s = EdgeStore::open_in_memory().unwrap();
        s.upsert_node(&n("close", "Person", "upsert", 100)).unwrap();
        s.upsert_node(&n("far", "Person", "upsert", 100)).unwrap();

        let mut close = vec![0.0_f32; EMBEDDING_DIM];
        close[0] = 1.0;
        let mut far = vec![0.0_f32; EMBEDDING_DIM];
        far[0] = -1.0;
        s.upsert_embedding("close", &close).unwrap();
        s.upsert_embedding("far", &far).unwrap();

        let results = s.vector_search(&close, 2).unwrap();
        assert_eq!(results[0].0, "close");
        assert!(results[0].1 < results[1].1);
    }

    fn msg(id: &str, conv: &str, role: &str, content: &str, ts: i64) -> Message {
        Message {
            id: id.into(),
            conversation_id: conv.into(),
            role: role.into(),
            content: content.into(),
            agent_id: Some("personal-agent".into()),
            model: Some("llama3.2".into()),
            tokens: Some(42),
            created_at: ts,
        }
    }

    #[test]
    fn create_and_list_conversations() {
        let s = EdgeStore::open_in_memory().unwrap();
        s.create_conversation("c1", "First", 100).unwrap();
        s.create_conversation("c2", "Second", 200).unwrap();
        let convs = s.list_conversations(10).unwrap();
        assert_eq!(convs.len(), 2);
        assert_eq!(convs[0].id, "c2"); // updated_at DESC
        assert_eq!(convs[0].message_count, 0);
    }

    #[test]
    fn create_conversation_rejects_empty_id() {
        let s = EdgeStore::open_in_memory().unwrap();
        assert!(matches!(
            s.create_conversation("", "x", 0).unwrap_err(),
            Error::Invalid(_)
        ));
    }

    #[test]
    fn append_message_bumps_conversation_updated_at() {
        let s = EdgeStore::open_in_memory().unwrap();
        s.create_conversation("c1", "T", 100).unwrap();
        s.append_message(&msg("m1", "c1", "user", "hi", 200))
            .unwrap();
        let convs = s.list_conversations(10).unwrap();
        assert_eq!(convs[0].updated_at, 200);
        assert_eq!(convs[0].message_count, 1);
    }

    #[test]
    fn append_message_unknown_conversation_errors() {
        let s = EdgeStore::open_in_memory().unwrap();
        let err = s
            .append_message(&msg("m1", "ghost", "user", "hi", 0))
            .unwrap_err();
        assert!(matches!(err, Error::Invalid(_)));
    }

    #[test]
    fn list_messages_orders_by_created_at_asc() {
        let s = EdgeStore::open_in_memory().unwrap();
        s.create_conversation("c1", "T", 100).unwrap();
        s.append_message(&msg("m1", "c1", "user", "first", 100))
            .unwrap();
        s.append_message(&msg("m2", "c1", "assistant", "reply", 200))
            .unwrap();
        s.append_message(&msg("m3", "c1", "user", "thanks", 300))
            .unwrap();
        let msgs = s.list_messages("c1", 10).unwrap();
        assert_eq!(
            msgs.iter().map(|m| m.id.as_str()).collect::<Vec<_>>(),
            vec!["m1", "m2", "m3"]
        );
        assert_eq!(msgs[1].role, "assistant");
        assert_eq!(msgs[1].tokens, Some(42));
    }

    #[test]
    fn delete_conversation_cascades_messages() {
        let s = EdgeStore::open_in_memory().unwrap();
        s.create_conversation("c1", "T", 100).unwrap();
        s.append_message(&msg("m1", "c1", "user", "x", 100))
            .unwrap();
        s.delete_conversation("c1").unwrap();
        assert_eq!(s.count_conversations().unwrap(), 0);
        assert_eq!(s.list_messages("c1", 10).unwrap().len(), 0);
    }

    #[test]
    fn rename_conversation() {
        let s = EdgeStore::open_in_memory().unwrap();
        s.create_conversation("c1", "Old", 100).unwrap();
        s.rename_conversation("c1", "New title", 200).unwrap();
        let convs = s.list_conversations(10).unwrap();
        assert_eq!(convs[0].title, "New title");
        assert_eq!(convs[0].updated_at, 200);
    }

    #[test]
    fn unknown_op_errors() {
        let mut s = EdgeStore::open_in_memory().unwrap();
        let bad = NodeDelta {
            operation: "merge".into(),
            ..n("a", "Person", "upsert", 100)
        };
        let err = s.upsert_node(&bad).unwrap_err();
        assert!(matches!(err, Error::Invalid(_)));
    }
}
