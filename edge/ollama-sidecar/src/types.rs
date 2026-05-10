use serde::{Deserialize, Serialize};

/// A single role-tagged message in a chat conversation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    /// "system" | "user" | "assistant" | "tool"
    pub role: String,
    pub content: String,
}

/// Request shape posted to `/api/chat`.
#[derive(Debug, Clone, Serialize)]
pub struct ChatRequest {
    pub model: String,
    pub messages: Vec<ChatMessage>,
    pub stream: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub options: Option<serde_json::Value>,
}

/// One chunk from the streaming `/api/generate` or `/api/chat` endpoint.
/// We only model the fields the UI needs; Ollama may return more.
#[derive(Debug, Clone, Deserialize)]
pub struct GenerateChunk {
    pub model: String,
    pub created_at: String,
    /// `/api/generate` puts the token here.
    #[serde(default)]
    pub response: String,
    /// `/api/chat` puts the assistant's partial message here.
    #[serde(default)]
    pub message: Option<ChatMessage>,
    pub done: bool,
    #[serde(default)]
    pub total_duration: Option<u64>,
    #[serde(default)]
    pub eval_count: Option<u64>,
}

/// Single model entry returned by `/api/tags`.
#[derive(Debug, Clone, Deserialize)]
pub struct ModelInfo {
    pub name: String,
    #[serde(default)]
    pub size: u64,
    #[serde(default)]
    pub modified_at: String,
    #[serde(default)]
    pub digest: String,
}

#[derive(Debug, Deserialize)]
pub(crate) struct TagsResponse {
    pub models: Vec<ModelInfo>,
}

#[derive(Debug, Serialize)]
pub(crate) struct GenerateRequest<'a> {
    pub model: &'a str,
    pub prompt: &'a str,
    pub stream: bool,
}
