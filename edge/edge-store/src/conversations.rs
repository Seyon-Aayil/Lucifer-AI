//! Conversation history persistence.
//!
//! Mirrors the React `Conversation` screen's shape: a flat list of
//! conversations, each with an ordered list of role-tagged messages.
//! Schema lives in the same SQLite file as the KG mirror so reads can be
//! cross-joined later (e.g. attach a message to a Person node).

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Conversation {
    pub id: String,
    pub title: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub message_count: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    pub id: String,
    pub conversation_id: String,
    /// "user" | "assistant" | "system" | "tool"
    pub role: String,
    pub content: String,
    #[serde(default)]
    pub agent_id: Option<String>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub tokens: Option<u32>,
    pub created_at: i64,
}

pub const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_updated
    ON conversations(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    agent_id        TEXT,
    model           TEXT,
    tokens          INTEGER,
    created_at      INTEGER NOT NULL,
    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_conv_created
    ON messages(conversation_id, created_at);
"#;
