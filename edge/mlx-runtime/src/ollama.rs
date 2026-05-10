use async_trait::async_trait;
use futures::{Stream, StreamExt};
use lucifer_ollama_sidecar::{GenerateChunk, OllamaClient};

use crate::{
    backend::{Inference, InferenceChunk},
    error::{Error, Result},
};

/// `Inference` implementation backed by [`OllamaClient`].
pub struct OllamaInference {
    client: OllamaClient,
}

impl OllamaInference {
    pub fn new(client: OllamaClient) -> Self {
        Self { client }
    }

    pub fn from_endpoint(endpoint: impl Into<String>) -> Self {
        Self::new(OllamaClient::new(endpoint))
    }
}

#[async_trait]
impl Inference for OllamaInference {
    fn name(&self) -> &'static str {
        "ollama"
    }

    async fn generate_stream(
        &self,
        model: &str,
        prompt: &str,
    ) -> Result<std::pin::Pin<Box<dyn Stream<Item = Result<InferenceChunk>> + Send>>> {
        let stream = self
            .client
            .generate_stream(model, prompt)
            .await
            .map_err(Error::from)?;
        let mapped = stream.map(|res| res.map(map_chunk).map_err(Error::from));
        Ok(Box::pin(mapped))
    }
}

fn map_chunk(c: GenerateChunk) -> InferenceChunk {
    InferenceChunk {
        model: c.model,
        text: c.response,
        done: c.done,
        eval_count: c.eval_count,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn name_is_ollama() {
        let i = OllamaInference::from_endpoint("http://127.0.0.1:11434");
        assert_eq!(i.name(), "ollama");
    }

    #[tokio::test]
    async fn dead_endpoint_surfaces_error() {
        let i = OllamaInference::from_endpoint("http://127.0.0.1:1");
        let res = i.generate_stream("nope", "hello").await;
        assert!(res.is_err());
    }
}
