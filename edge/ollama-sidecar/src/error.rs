use thiserror::Error;

#[derive(Debug, Error)]
pub enum Error {
    #[error("ollama binary not found at {0}")]
    BinaryNotFound(String),

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("http error: {0}")]
    Http(#[from] reqwest::Error),

    #[error("ollama not ready after {0:?}")]
    NotReady(std::time::Duration),

    #[error("invalid response: {0}")]
    Invalid(String),

    #[error("subprocess exited with code {0:?}")]
    Exited(Option<i32>),

    #[error("serde error: {0}")]
    Serde(#[from] serde_json::Error),
}

pub type Result<T> = std::result::Result<T, Error>;
