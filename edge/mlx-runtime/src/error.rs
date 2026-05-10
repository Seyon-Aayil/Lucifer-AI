use thiserror::Error;

#[derive(Debug, Error)]
pub enum Error {
    #[error("ollama backend: {0}")]
    Ollama(#[from] lucifer_ollama_sidecar::Error),

    #[error("mlx backend not available: {0}")]
    MlxUnavailable(String),

    #[error("invalid input: {0}")]
    Invalid(String),
}

pub type Result<T> = std::result::Result<T, Error>;
