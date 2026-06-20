//! Lucifer sync client.
//!
//! gRPC + mTLS client for the edge ↔ master sync protocol defined in
//! `infra/proto/lucifer_sync.proto`. Wraps `tonic` with:
//! - mTLS channel construction from PEM file paths
//! - JWT bearer token injection per RPC
//! - `SyncStream`, `GetHotSubgraph`, `PushTelemetry` helpers

pub mod auth;
pub mod config;
pub mod error;
pub mod sign;
pub mod transport;

pub mod proto {
    //! Generated protobuf types. The full path matches the proto package
    //! `lucifer.sync.v1` declared in `infra/proto/lucifer_sync.proto`.
    tonic::include_proto!("lucifer.sync.v1");
}

pub use auth::AuthInterceptor;
pub use config::ClientConfig;
pub use error::{Error, Result};
pub use sign::sign_message;
pub use transport::SyncClient;
