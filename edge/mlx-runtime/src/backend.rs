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

#[cfg(test)]
mod tests {
    use super::*;

    /// Minimal backend that emits a fixed sequence of chunks — enough to
    /// exercise the trait-default `generate()` without any real inference.
    struct FakeBackend {
        chunks: Vec<&'static str>,
    }

    #[async_trait]
    impl Inference for FakeBackend {
        fn name(&self) -> &'static str {
            "fake"
        }

        async fn generate_stream(
            &self,
            model: &str,
            _prompt: &str,
        ) -> Result<std::pin::Pin<Box<dyn Stream<Item = Result<InferenceChunk>> + Send>>> {
            let model = model.to_string();
            let last = self.chunks.len().saturating_sub(1);
            let items: Vec<Result<InferenceChunk>> = self
                .chunks
                .iter()
                .enumerate()
                .map(|(i, text)| {
                    Ok(InferenceChunk {
                        model: model.clone(),
                        text: (*text).to_string(),
                        done: i == last,
                        eval_count: None,
                    })
                })
                .collect();
            Ok(Box::pin(futures::stream::iter(items)))
        }
    }

    #[tokio::test]
    async fn generate_default_drains_stream_and_concatenates() {
        let backend = FakeBackend {
            chunks: vec!["Hel", "lo", "!"],
        };
        // The default generate() must join every chunk's text in order.
        assert_eq!(backend.generate("m", "hi").await.unwrap(), "Hello!");
    }

    #[tokio::test]
    async fn generate_default_handles_empty_stream() {
        let backend = FakeBackend { chunks: vec![] };
        assert_eq!(backend.generate("m", "hi").await.unwrap(), "");
    }

    #[test]
    fn inference_chunk_serde_round_trip() {
        let chunk = InferenceChunk {
            model: "qwen".into(),
            text: "hi".into(),
            done: true,
            eval_count: Some(7),
        };
        let json = serde_json::to_string(&chunk).unwrap();
        let back: InferenceChunk = serde_json::from_str(&json).unwrap();
        assert_eq!(back.model, "qwen");
        assert_eq!(back.text, "hi");
        assert!(back.done);
        assert_eq!(back.eval_count, Some(7));
    }

    #[test]
    fn inference_chunk_eval_count_defaults_when_absent() {
        // #[serde(default)] → a payload without eval_count deserialises to None.
        let back: InferenceChunk =
            serde_json::from_str(r#"{"model":"m","text":"x","done":false}"#).unwrap();
        assert_eq!(back.eval_count, None);
    }

    #[test]
    fn backend_serialises_snake_case_and_round_trips() {
        assert_eq!(serde_json::to_string(&Backend::Llama).unwrap(), "\"llama\"");
        assert_eq!(serde_json::to_string(&Backend::Mlx).unwrap(), "\"mlx\"");
        let back: Backend = serde_json::from_str("\"ollama\"").unwrap();
        assert_eq!(back, Backend::Ollama);
    }

    #[test]
    fn label_matches_serde_tag() {
        for backend in [Backend::Mlx, Backend::Ollama, Backend::Llama] {
            let json = serde_json::to_string(&backend).unwrap();
            assert_eq!(json, format!("\"{}\"", backend.label()));
        }
    }
}
