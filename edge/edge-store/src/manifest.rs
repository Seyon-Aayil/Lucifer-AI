use serde::{Deserialize, Serialize};

/// Mirror of `master.sync.hot_subgraph` manifest entries. JSON-friendly so the
/// Tauri WebView can ferry one over IPC without re-encoding.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NodeDelta {
    pub node_id: String,
    /// "upsert" | "soft_delete"
    pub operation: String,
    pub node_type: String,
    pub classification: String,
    /// Plain JSON-serialisable attribute bag (master encrypts at the wire,
    /// the IPC layer is expected to decrypt before calling EdgeStore).
    pub payload: serde_json::Value,
    pub updated_at: i64,
    #[serde(default)]
    pub source_agent: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EdgeDelta {
    pub edge_id: String,
    /// "upsert" | "delete"
    pub operation: String,
    pub from_node_id: String,
    pub to_node_id: String,
    pub relation: String,
    pub weight: f64,
    #[serde(default)]
    pub valid_from: i64,
    #[serde(default)]
    pub valid_until: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SubgraphManifest {
    pub nodes: Vec<NodeDelta>,
    pub edges: Vec<EdgeDelta>,
    pub generated_at: i64,
    pub manifest_hash: String,
    pub is_full_sync: bool,
}
