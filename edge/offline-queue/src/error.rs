use thiserror::Error;

#[derive(Debug, Error)]
pub enum Error {
    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),

    #[error("queue full: at cap ({cap})")]
    Full { cap: usize },

    #[error("unknown action_id: {0}")]
    UnknownAction(String),

    #[error("invalid input: {0}")]
    Invalid(String),
}

pub type Result<T> = std::result::Result<T, Error>;
