//! Lucifer desktop bindings.
//!
//! Glue layer between the Tauri WebView frontend (Stitch-generated screens) and
//! the [`lucifer_sync_client`] crate. Exports IPC commands the JS side calls via
//! `invoke()`. Holds a singleton `SyncClient` behind `tokio::sync::Mutex` so the
//! WebView can reuse one mTLS channel for the entire app lifetime.
//!
//! Wire it in your `main.rs` like so:
//!
//! ```ignore
//! fn main() {
//!     tauri::Builder::default()
//!         .manage(lucifer_desktop_bindings::ClientHandle::default())
//!         .invoke_handler(lucifer_desktop_bindings::generate_handler())
//!         .run(tauri::generate_context!())
//!         .expect("failed to launch Lucifer desktop");
//! }
//! ```
//!
//! IPC commands exposed (when the `tauri-cmd` feature is on):
//! - `connect_master(config)` — open the gRPC channel using the supplied config
//! - `disconnect()` — drop the channel
//! - `is_connected()` — bool
//! - `get_hot_subgraph(device_id, last_sync_at_ms)` — pull manifest
//! - `push_telemetry(events)` — fire-and-forget telemetry batch
//! - `update_jwt(jwt)` — rotate the bearer token without reconnecting

pub mod commands;
pub mod keychain;
pub mod refresher;
pub mod state;
pub mod types;

pub use keychain::{
    forget_device, persist_pairing_bundle, read_stored_jwt, PairingBundle, PersistedCredentials,
};
pub use state::{ClientHandle, EdgeStoreHandle, InferenceHandle, OfflineQueueHandle};
pub use types::{ConnectArgs, ConnectError, TelemetryEvent};

// `handlers!()` is the macro-based replacement for `tauri::generate_handler!`.
// It is exported at the crate root automatically thanks to `#[macro_export]`
// in `commands::__handlers`.
