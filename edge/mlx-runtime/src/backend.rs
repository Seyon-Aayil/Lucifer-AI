use async_trait::async_trait;
use futures::Stream;
use serde::{Deserialize, Serialize};

use crate::error::Result;

/// One token (or chunk of tokens) produced by a backend.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InferenceChunk {
    pub model: String,
    pub text: String,
    pub done: bool,
    #[serde(default)]
    pub eval_count: Option<u64>,
}

/// Common surface for any local inference backend.
#[async_trait]
pub trait Inference: Send + Sync {
    fn name(&self) -> &'static str;

    /// Streaming generate. The returned stream must yield at least one chunk
    /// with `done = true` before terminating.
    async fn generate_stream(
        &self,
        model: &str,
        prompt: &str,
    ) -> Result<std::pin::Pin<Box<dyn Stream<Item = Result<InferenceChunk>> + Send>>>;

    /// Non-streaming convenience wrapper. Default implementation drains the
    /// stream and concatenates `chunk.text`.
    async fn generate(&self, model: &str, prompt: &str) -> Result<String> {
        use futures::StreamExt;
        let mut stream = self.generate_stream(model, prompt).await?;
        let mut out = String::new();
        while let Some(chunk) = stream.next().await {
            out.push_str(&chunk?.text);
        }
        Ok(out)
    }
}

/// Tag identifying which backend was selected. Useful for telemetry.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Backend {
    Mlx,
    Ollama,
    /// llama.cpp (cross-platform GGUF). The chosen mobile inference engine —
    /// see docs/phase-4c-mobile-go-no-go.md. Wiring to `llama-cpp-2` lands in
    /// the Phase 4c device spike; the seam is scaffolded in `llama.rs`.
    Llama,
}

impl Backend {
    pub fn label(self) -> &'static str {
        match self {
            Backend::Mlx => "mlx",
            Backend::Ollama => "ollama",
            Backend::Llama => "llama",
        }
    }
}
