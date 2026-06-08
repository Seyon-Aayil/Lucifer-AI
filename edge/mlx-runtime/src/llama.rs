//! llama.cpp inference backend (cross-platform GGUF).
//!
//! This is the chosen **mobile** inference engine for Phase 4c — it runs on both
//! iOS (Metal) and Android (Vulkan/NNAPI), unlike Ollama (no mobile build) or MLX
//! (Apple-only). See `docs/phase-4c-mobile-go-no-go.md`.
//!
//! ## Status
//!
//! This module scaffolds the integration **seam**: the [`LlamaCppInference`] type,
//! its [`Inference`] impl, and construction from a GGUF model path. The actual
//! token loop is wired to the `llama-cpp-2` crate during the Phase 4c device spike
//! (Gate 3), where it can be exercised against a real model on a real device —
//! the same way [`crate::mlx`] began as a structurally-complete stub.
//!
//! Until then, `generate_stream` returns [`Error::LlamaUnavailable`]; the backend
//! is never selected by default (`select_backend` only picks it when the spike
//! explicitly opts in via `LUCIFER_LLAMA_MODEL`).
//!
//! Gated on the `llama` feature by `lib.rs` (`#[cfg(feature = "llama")] pub mod llama`).

use std::path::{Path, PathBuf};
use std::pin::Pin;

use async_trait::async_trait;
use futures::Stream;

use crate::backend::{Inference, InferenceChunk};
use crate::error::{Error, Result};

/// llama.cpp-backed [`Inference`]. Constructed from a path to a GGUF model file.
pub struct LlamaCppInference {
    model_path: PathBuf,
}

impl LlamaCppInference {
    /// Point the backend at a GGUF model file. The model is loaded lazily on the
    /// first request (once the `llama-cpp-2` wiring lands).
    pub fn new(model_path: impl AsRef<Path>) -> Self {
        Self {
            model_path: model_path.as_ref().to_path_buf(),
        }
    }

    /// The configured model file path.
    pub fn model_path(&self) -> &Path {
        &self.model_path
    }
}

#[async_trait]
impl Inference for LlamaCppInference {
    fn name(&self) -> &'static str {
        "llama"
    }

    async fn generate_stream(
        &self,
        _model: &str,
        _prompt: &str,
    ) -> Result<Pin<Box<dyn Stream<Item = Result<InferenceChunk>> + Send>>> {
        // Phase 4c spike, Gate 3: load `self.model_path` via llama-cpp-2, build a
        // context, tokenize the prompt, and stream sampled tokens as
        // InferenceChunk { done: false } ending with a final { done: true }.
        Err(Error::LlamaUnavailable(format!(
            "llama.cpp token loop not yet wired (Phase 4c spike); model={}",
            self.model_path.display()
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exposes_model_path_and_name() {
        let inf = LlamaCppInference::new("/models/qwen2.5-0.5b.gguf");
        assert_eq!(inf.name(), "llama");
        assert_eq!(
            inf.model_path(),
            std::path::Path::new("/models/qwen2.5-0.5b.gguf")
        );
    }

    #[tokio::test]
    async fn generate_is_not_yet_wired() {
        let inf = LlamaCppInference::new("/models/x.gguf");
        // The Ok variant (a boxed Stream) isn't Debug, so match rather than unwrap.
        let res = inf.generate_stream("x", "hi").await;
        assert!(matches!(res, Err(Error::LlamaUnavailable(_))));
    }
}
