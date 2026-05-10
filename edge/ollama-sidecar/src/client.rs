use std::time::Duration;

use futures::{Stream, StreamExt};
use reqwest::Client;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio_util::io::StreamReader;

use crate::{
    error::{Error, Result},
    types::{ChatRequest, GenerateChunk, GenerateRequest, ModelInfo, TagsResponse},
};

/// Async HTTP client for the Ollama daemon. Cloneable — wraps an `Arc` under
/// the hood (via `reqwest::Client`).
#[derive(Clone)]
pub struct OllamaClient {
    endpoint: String,
    http: Client,
}

impl OllamaClient {
    pub fn new(endpoint: impl Into<String>) -> Self {
        let http = Client::builder()
            .timeout(Duration::from_secs(60 * 5))
            .build()
            .expect("reqwest client should build with default config");
        Self {
            endpoint: endpoint.into(),
            http,
        }
    }

    pub fn endpoint(&self) -> &str {
        &self.endpoint
    }

    // ── Model management ─────────────────────────────────────────────────────

    /// `GET /api/tags` — list locally-available models.
    pub async fn list_models(&self) -> Result<Vec<ModelInfo>> {
        let url = format!("{}/api/tags", self.endpoint);
        let resp = self.http.get(&url).send().await?.error_for_status()?;
        let tags: TagsResponse = resp.json().await?;
        Ok(tags.models)
    }

    /// `POST /api/pull` — non-streaming variant that waits until the pull
    /// completes. For UI progress, use [`Self::pull_stream`].
    pub async fn pull(&self, model: &str) -> Result<()> {
        let url = format!("{}/api/pull", self.endpoint);
        self.http
            .post(&url)
            .json(&serde_json::json!({"name": model, "stream": false}))
            .send()
            .await?
            .error_for_status()?;
        Ok(())
    }

    /// `DELETE /api/delete` — remove a downloaded model.
    pub async fn delete(&self, model: &str) -> Result<()> {
        let url = format!("{}/api/delete", self.endpoint);
        self.http
            .delete(&url)
            .json(&serde_json::json!({"name": model}))
            .send()
            .await?
            .error_for_status()?;
        Ok(())
    }

    // ── Inference ────────────────────────────────────────────────────────────

    /// `POST /api/generate` with `stream=true`. Yields one [`GenerateChunk`]
    /// per server-sent JSON line.
    pub async fn generate_stream(
        &self,
        model: &str,
        prompt: &str,
    ) -> Result<impl Stream<Item = Result<GenerateChunk>>> {
        let url = format!("{}/api/generate", self.endpoint);
        let body = GenerateRequest {
            model,
            prompt,
            stream: true,
        };
        let resp = self
            .http
            .post(&url)
            .json(&body)
            .send()
            .await?
            .error_for_status()?;
        Ok(json_lines(resp))
    }

    /// `POST /api/chat` streaming variant.
    pub async fn chat_stream(
        &self,
        request: ChatRequest,
    ) -> Result<impl Stream<Item = Result<GenerateChunk>>> {
        let url = format!("{}/api/chat", self.endpoint);
        let mut body = request;
        body.stream = true;
        let resp = self
            .http
            .post(&url)
            .json(&body)
            .send()
            .await?
            .error_for_status()?;
        Ok(json_lines(resp))
    }

    /// One-shot non-streaming generate. Concatenates all chunks.
    pub async fn generate(&self, model: &str, prompt: &str) -> Result<String> {
        let stream = self.generate_stream(model, prompt).await?;
        let mut stream = Box::pin(stream);
        let mut out = String::new();
        while let Some(chunk) = stream.next().await {
            out.push_str(&chunk?.response);
        }
        Ok(out)
    }
}

/// Convert a `reqwest::Response` body into a stream of newline-delimited JSON
/// values. Ollama always emits one JSON object per line for streaming
/// endpoints.
fn json_lines(resp: reqwest::Response) -> impl Stream<Item = Result<GenerateChunk>> {
    let byte_stream = resp
        .bytes_stream()
        .map(|item| item.map_err(std::io::Error::other));
    let reader = StreamReader::new(byte_stream);
    let lines = BufReader::new(reader).lines();
    let lines_stream = tokio_stream::wrappers::LinesStream::new(lines);
    lines_stream.filter_map(|line_res: std::io::Result<String>| async move {
        match line_res {
            Err(e) => Some(Err(Error::Io(e))),
            Ok(line) if line.trim().is_empty() => None,
            Ok(line) => Some(
                serde_json::from_str::<GenerateChunk>(&line)
                    .map_err(|e| Error::Invalid(format!("malformed JSON line: {e}"))),
            ),
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn endpoint_round_trip() {
        let c = OllamaClient::new("http://localhost:11434");
        assert_eq!(c.endpoint(), "http://localhost:11434");
    }

    #[tokio::test]
    async fn list_models_against_dead_endpoint_errors() {
        let c = OllamaClient::new("http://127.0.0.1:1");
        let err = c.list_models().await.unwrap_err();
        assert!(matches!(err, Error::Http(_)));
    }
}
