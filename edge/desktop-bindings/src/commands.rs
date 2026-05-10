//! Tauri IPC command handlers. Each function is `pub` so it can also be tested
//! directly without a Tauri context. The `#[tauri::command]` glue lives in the
//! `__handlers` submodule and is gated behind the `tauri-cmd` feature.

use lucifer_sync_client::SyncClient;

use crate::{
    state::ClientHandle,
    types::{ConnectArgs, ConnectError, TelemetryEvent},
};

/// Open the gRPC channel using the supplied configuration. Idempotent: returns
/// `AlreadyConnected` if a session is already live.
pub async fn connect_master(handle: &ClientHandle, args: ConnectArgs) -> Result<(), ConnectError> {
    if handle.is_connected().await {
        return Err(ConnectError::AlreadyConnected);
    }
    let cfg: lucifer_sync_client::ClientConfig = args.into();
    let client = SyncClient::connect(&cfg).await?;
    handle.set(client).await?;
    Ok(())
}

/// Drop the channel. Returns `NotConnected` if there was nothing to drop.
pub async fn disconnect(handle: &ClientHandle) -> Result<(), ConnectError> {
    handle
        .take()
        .await
        .ok_or(ConnectError::NotConnected)
        .map(|_| ())
}

/// Cheap connection-status probe for the frontend.
pub async fn is_connected(handle: &ClientHandle) -> Result<bool, ConnectError> {
    Ok(handle.is_connected().await)
}

/// Pull the device's hot-subgraph manifest and return it as JSON. The proto
/// types aren't `serde::Serialize` so we shape an explicit JSON value.
pub async fn get_hot_subgraph(
    handle: &ClientHandle,
    device_id: String,
    last_sync_at_ms: i64,
) -> Result<serde_json::Value, ConnectError> {
    handle
        .with_mut(|client| {
            Box::pin(async move {
                let req = lucifer_sync_client::proto::SubgraphRequest {
                    device_id,
                    last_sync_at: last_sync_at_ms,
                    max_size_bytes: 0,
                };
                let resp = client.get_hot_subgraph(req).await?;
                Ok(serde_json::json!({
                    "node_count": resp.nodes.len(),
                    "edge_count": resp.edges.len(),
                    "generated_at": resp.generated_at,
                    "manifest_hash": resp.manifest_hash,
                    "is_full_sync": resp.is_full_sync,
                }))
            })
        })
        .await
}

/// Fire a telemetry batch. Currently a stub that converts the JS payload but
/// doesn't yet hit the proto-level `TelemetryBatch` (proto schema for events
/// is still being finalised in `infra/proto/lucifer_sync.proto`).
pub async fn push_telemetry(
    handle: &ClientHandle,
    events: Vec<TelemetryEvent>,
) -> Result<u32, ConnectError> {
    if !handle.is_connected().await {
        return Err(ConnectError::NotConnected);
    }
    // TODO: map TelemetryEvent → proto::TelemetryBatch once the proto event
    // shape is finalised. For now, log and ack.
    tracing::debug!(count = events.len(), "telemetry batch queued");
    Ok(events.len() as u32)
}

#[cfg(feature = "tauri-cmd")]
pub mod __handlers {
    //! Tauri-specific glue. Kept in a submodule so tests can call the bare
    //! async functions in [`super`] without dragging in the Tauri runtime.

    use tauri::State;

    use super::*;

    #[tauri::command]
    pub async fn connect_master(
        handle: State<'_, ClientHandle>,
        args: ConnectArgs,
    ) -> Result<(), ConnectError> {
        super::connect_master(handle.inner(), args).await
    }

    #[tauri::command]
    pub async fn disconnect(handle: State<'_, ClientHandle>) -> Result<(), ConnectError> {
        super::disconnect(handle.inner()).await
    }

    #[tauri::command]
    pub async fn is_connected(handle: State<'_, ClientHandle>) -> Result<bool, ConnectError> {
        super::is_connected(handle.inner()).await
    }

    #[tauri::command]
    pub async fn get_hot_subgraph(
        handle: State<'_, ClientHandle>,
        device_id: String,
        last_sync_at_ms: i64,
    ) -> Result<serde_json::Value, ConnectError> {
        super::get_hot_subgraph(handle.inner(), device_id, last_sync_at_ms).await
    }

    #[tauri::command]
    pub async fn push_telemetry(
        handle: State<'_, ClientHandle>,
        events: Vec<TelemetryEvent>,
    ) -> Result<u32, ConnectError> {
        super::push_telemetry(handle.inner(), events).await
    }

    // Tauri's `generate_handler!` cannot be wrapped in a generic-returning fn
    // because the resulting type closes over `Builder`'s runtime parameter. The
    // recommended pattern is to call the macro directly at the call-site, e.g.
    // `.invoke_handler(lucifer_desktop_bindings::handlers!())` via the helper
    // macro below.

    /// Convenience macro re-exported as `lucifer_desktop_bindings::handlers!()`
    /// — drop-in replacement for `tauri::generate_handler![...]`.
    #[macro_export]
    macro_rules! handlers {
        () => {
            ::tauri::generate_handler![
                $crate::commands::__handlers::connect_master,
                $crate::commands::__handlers::disconnect,
                $crate::commands::__handlers::is_connected,
                $crate::commands::__handlers::get_hot_subgraph,
                $crate::commands::__handlers::push_telemetry,
            ]
        };
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn is_connected_false_initially() {
        let h = ClientHandle::default();
        assert!(!is_connected(&h).await.unwrap());
    }

    #[tokio::test]
    async fn disconnect_when_not_connected_errors() {
        let h = ClientHandle::default();
        let err = disconnect(&h).await.unwrap_err();
        assert!(matches!(err, ConnectError::NotConnected));
    }

    #[tokio::test]
    async fn push_telemetry_requires_connection() {
        let h = ClientHandle::default();
        let err = push_telemetry(&h, vec![]).await.unwrap_err();
        assert!(matches!(err, ConnectError::NotConnected));
    }

    #[tokio::test]
    async fn get_hot_subgraph_requires_connection() {
        let h = ClientHandle::default();
        let err = get_hot_subgraph(&h, "device".into(), 0).await.unwrap_err();
        assert!(matches!(err, ConnectError::NotConnected));
    }
}
