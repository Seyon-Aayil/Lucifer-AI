//! MLX backend stub.
//!
//! Compile-gated behind the `mlx` feature so non-Apple-Silicon builds don't
//! pay the C++ link cost. Until the upstream `mlx-rs` integration lands, this
//! file ships a placeholder that wraps the Ollama backend so callers can keep
//! the same dispatch surface — they will start getting genuinely-MLX paths the
//! day this file's body changes.

#![cfg(feature = "mlx")]

use async_trait::async_trait;
use futures::Stream;

use crate::{
    backend::{Inference, InferenceChunk},
    error::Result,
    ollama::OllamaInference,
};

/// Apple-Silicon MLX runtime. Currently delegates to Ollama until the real
/// `mlx-rs` model loader is wired up. The compile-time `mlx` feature exists
/// so callers can opt in once the implementation is genuine.
pub struct MlxInference {
    delegate: OllamaInference,
}

impl MlxInference {
    pub fn new(ollama_endpoint: impl Into<String>) -> Self {
        Self {
            delegate: OllamaInference::from_endpoint(ollama_endpoint),
        }
    }
}

#[async_trait]
impl Inference for MlxInference {
    fn name(&self) -> &'static str {
        "mlx"
    }

    async fn generate_stream(
        &self,
        model: &str,
        prompt: &str,
    ) -> Result<std::pin::Pin<Box<dyn Stream<Item = Result<InferenceChunk>> + Send>>> {
        // TODO: load model via mlx-rs and stream tokens directly. For now we
        // still call Ollama so the integration path keeps producing output.
        self.delegate.generate_stream(model, prompt).await
    }
}
