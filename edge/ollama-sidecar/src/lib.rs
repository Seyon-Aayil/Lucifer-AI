//! Lucifer Ollama sidecar.
//!
//! Two pieces:
//! - [`OllamaProcess`] — spawns and supervises a local `ollama serve` subprocess
//!   so the desktop app ships a single binary the user doesn't have to start.
//! - [`OllamaClient`] — thin async HTTP client over the `/api/*` endpoints
//!   (`generate`, `chat`, `tags`, `pull`, `delete`). Streaming responses are
//!   returned as `tokio_stream::Stream<Item = Result<...>>`.
//!
//! ```ignore
//! let mut proc = OllamaProcess::spawn(OllamaConfig::default()).await?;
//! proc.wait_ready(Duration::from_secs(30)).await?;
//! let client = OllamaClient::new(proc.endpoint());
//! let mut stream = client.generate("llama3.1", "hello", true).await?;
//! while let Some(chunk) = stream.next().await { /* ... */ }
//! ```

pub mod client;
pub mod error;
pub mod process;
pub mod types;

pub use client::OllamaClient;
pub use error::{Error, Result};
pub use process::{OllamaConfig, OllamaProcess};
pub use types::{ChatMessage, ChatRequest, GenerateChunk, ModelInfo};
