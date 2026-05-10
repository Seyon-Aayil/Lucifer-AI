use thiserror::Error;

#[derive(Debug, Error)]
pub enum Error {
    #[error("invalid client config: {0}")]
    Config(String),

    #[error("tls setup failed: {0}")]
    Tls(String),

    #[error("transport error: {0}")]
    Transport(#[from] Box<tonic::transport::Error>),

    #[error("rpc failed: {0}")]
    Rpc(#[from] Box<tonic::Status>),

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("invalid auth token: {0}")]
    Auth(String),
}

impl From<tonic::transport::Error> for Error {
    fn from(e: tonic::transport::Error) -> Self {
        Error::Transport(Box::new(e))
    }
}

impl From<tonic::Status> for Error {
    fn from(e: tonic::Status) -> Self {
        Error::Rpc(Box::new(e))
    }
}

pub type Result<T> = std::result::Result<T, Error>;
