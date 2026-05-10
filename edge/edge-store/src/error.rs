use thiserror::Error;

#[derive(Debug, Error)]
pub enum Error {
    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),

    #[error("sqlite-vec extension load failed: {0}")]
    VecLoad(String),

    #[error("invalid embedding: expected {expected} dims, got {got}")]
    InvalidEmbedding { expected: usize, got: usize },

    #[error("manifest hash mismatch: expected {expected}, got {got}")]
    ManifestHashMismatch { expected: String, got: String },

    #[error("invalid input: {0}")]
    Invalid(String),

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("serde error: {0}")]
    Serde(#[from] serde_json::Error),
}

pub type Result<T> = std::result::Result<T, Error>;
