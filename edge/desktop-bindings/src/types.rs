use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// JS-friendly subset of [`lucifer_sync_client::ClientConfig`]. The frontend
/// passes path strings; this crate maps them onto `PathBuf` before validating.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ConnectArgs {
    pub master_endpoint: String,
    pub device_id: String,
    pub client_cert_path: String,
    pub client_key_path: String,
    pub ca_cert_path: String,
    pub jwt: String,
    #[serde(default)]
    pub sni_override: Option<String>,
}

impl From<ConnectArgs> for lucifer_sync_client::ClientConfig {
    fn from(a: ConnectArgs) -> Self {
        Self {
            master_endpoint: a.master_endpoint,
            device_id: a.device_id,
            client_cert: PathBuf::from(a.client_cert_path),
            client_key: PathBuf::from(a.client_key_path),
            ca_cert: PathBuf::from(a.ca_cert_path),
            jwt: a.jwt,
            sni_override: a.sni_override,
        }
    }
}

/// Single telemetry event the frontend can batch via `push_telemetry`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TelemetryEvent {
    pub event_type: String,
    pub timestamp_ms: i64,
    pub attributes: serde_json::Value,
}

/// Error type surfaced to the WebView. Always serialises as a string so JS
/// callers can show it directly.
#[derive(Debug, Error)]
pub enum ConnectError {
    #[error("not connected")]
    NotConnected,
    #[error("already connected")]
    AlreadyConnected,
    #[error("sync client error: {0}")]
    Sync(#[from] lucifer_sync_client::Error),
}

impl serde::Serialize for ConnectError {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.to_string())
    }
}
