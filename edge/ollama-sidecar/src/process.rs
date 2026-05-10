use std::{path::PathBuf, time::Duration};

use tokio::{
    process::{Child, Command},
    time::sleep,
};

use crate::error::{Error, Result};

/// Spawn-time configuration. Defaults match a vanilla `brew install ollama`
/// install on macOS — listening on 127.0.0.1:11434 with the standard model
/// directory.
#[derive(Debug, Clone)]
pub struct OllamaConfig {
    /// Path to the `ollama` binary. If `None`, looks up `ollama` on `$PATH`.
    pub binary: Option<PathBuf>,
    /// Listen host (default: `127.0.0.1`).
    pub host: String,
    /// Listen port (default: `11434`).
    pub port: u16,
    /// Optional override for the model directory (`OLLAMA_MODELS`).
    pub models_dir: Option<PathBuf>,
}

impl Default for OllamaConfig {
    fn default() -> Self {
        Self {
            binary: None,
            host: "127.0.0.1".into(),
            port: 11434,
            models_dir: None,
        }
    }
}

impl OllamaConfig {
    pub fn endpoint(&self) -> String {
        format!("http://{}:{}", self.host, self.port)
    }

    fn binary_or_default(&self) -> PathBuf {
        self.binary
            .clone()
            .unwrap_or_else(|| PathBuf::from("ollama"))
    }
}

/// Owns a running `ollama serve` subprocess. Drop kills the child.
#[derive(Debug)]
pub struct OllamaProcess {
    child: Option<Child>,
    cfg: OllamaConfig,
}

impl OllamaProcess {
    /// Spawn `ollama serve` with the supplied config. Does **not** wait for the
    /// HTTP listener — call [`wait_ready`] before issuing requests.
    pub async fn spawn(cfg: OllamaConfig) -> Result<Self> {
        let bin = cfg.binary_or_default();
        let mut cmd = Command::new(&bin);
        cmd.arg("serve")
            .env("OLLAMA_HOST", format!("{}:{}", cfg.host, cfg.port))
            .kill_on_drop(true);
        if let Some(dir) = &cfg.models_dir {
            cmd.env("OLLAMA_MODELS", dir);
        }

        let child = cmd.spawn().map_err(|e| match e.kind() {
            std::io::ErrorKind::NotFound => {
                Error::BinaryNotFound(bin.to_string_lossy().into_owned())
            }
            _ => Error::Io(e),
        })?;

        Ok(Self {
            child: Some(child),
            cfg,
        })
    }

    /// Block until `/api/tags` returns 200 OK or `timeout` elapses.
    pub async fn wait_ready(&self, timeout: Duration) -> Result<()> {
        let url = format!("{}/api/tags", self.cfg.endpoint());
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(2))
            .build()?;
        let deadline = std::time::Instant::now() + timeout;
        loop {
            if let Ok(resp) = client.get(&url).send().await {
                if resp.status().is_success() {
                    return Ok(());
                }
            }
            if std::time::Instant::now() >= deadline {
                return Err(Error::NotReady(timeout));
            }
            sleep(Duration::from_millis(250)).await;
        }
    }

    pub fn endpoint(&self) -> String {
        self.cfg.endpoint()
    }

    /// Reap the child and surface its exit status.
    pub async fn shutdown(mut self) -> Result<Option<i32>> {
        if let Some(mut child) = self.child.take() {
            // Best-effort SIGKILL via tokio's `kill`; on Unix Ollama also
            // listens for SIGTERM, but we don't have a portable handle here.
            let _ = child.kill().await;
            let status = child.wait().await?;
            return Ok(status.code());
        }
        Ok(None)
    }
}

impl Drop for OllamaProcess {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            // Best-effort: don't block the drop, just send the kill signal.
            let _ = child.start_kill();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_endpoint_format() {
        let cfg = OllamaConfig::default();
        assert_eq!(cfg.endpoint(), "http://127.0.0.1:11434");
    }

    #[test]
    fn custom_host_port() {
        let cfg = OllamaConfig {
            host: "0.0.0.0".into(),
            port: 8080,
            ..OllamaConfig::default()
        };
        assert_eq!(cfg.endpoint(), "http://0.0.0.0:8080");
    }

    #[tokio::test]
    async fn missing_binary_returns_typed_error() {
        let cfg = OllamaConfig {
            binary: Some(PathBuf::from("/definitely/not/here/ollama")),
            ..OllamaConfig::default()
        };
        let err = OllamaProcess::spawn(cfg).await.unwrap_err();
        assert!(matches!(err, Error::BinaryNotFound(_)));
    }
}
