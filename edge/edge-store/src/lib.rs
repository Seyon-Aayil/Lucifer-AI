//! Lucifer edge knowledge-graph store.
//!
//! SQLite-backed mirror of the master Neo4j hot-subgraph. Uses
//! [`sqlite-vec`](https://github.com/asg017/sqlite-vec) for vector search over
//! 1536-dim embeddings. Designed to be opened from the Tauri WebView side via
//! `lucifer_desktop_bindings` later.
//!
//! Schema (see `migrations.sql` for the canonical definition):
//!
//! ```text
//! nodes(id PRIMARY KEY, node_type, classification, payload BLOB,
//!       updated_at INTEGER, decay_score REAL, source_agent,
//!       deleted_at INTEGER NULL)
//! edges(id PRIMARY KEY, from_id, to_id, relation, weight REAL,
//!       valid_from INTEGER, valid_until INTEGER)
//! vec_nodes USING vec0(embedding float[1536])  -- rowid = nodes.rowid
//! ```
//!
//! Operations live in [`EdgeStore`]: open / apply_manifest / upsert /
//! soft_delete / query_by_type / vector_search.

pub mod conversations;
pub mod error;
pub mod manifest;
pub mod store;

pub use conversations::{Conversation, Message};

pub use error::{Error, Result};
pub use manifest::{EdgeDelta, NodeDelta, SubgraphManifest};
pub use store::EdgeStore;

/// Embedding dimensionality used by the master Librarian Agent.
pub const EMBEDDING_DIM: usize = 1536;
