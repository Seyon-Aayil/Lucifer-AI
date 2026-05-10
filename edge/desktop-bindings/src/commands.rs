//! Tauri IPC command handlers. Each function is `pub` so it can also be tested
//! directly without a Tauri context. The `#[tauri::command]` glue lives in the
//! `__handlers` submodule and is gated behind the `tauri-cmd` feature.

use std::path::{Path, PathBuf};

use lucifer_offline_queue::ActionStatus;
use lucifer_sync_client::SyncClient;

use crate::{
    keychain::{self, PairingBundle, PersistedCredentials},
    state::{ClientHandle, EdgeStoreHandle, InferenceHandle, OfflineQueueHandle},
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

// ── Edge store ───────────────────────────────────────────────────────────────

pub async fn open_edge_store(handle: &EdgeStoreHandle, path: String) -> Result<(), ConnectError> {
    handle.open(PathBuf::from(path)).await
}

pub async fn edge_store_stats(handle: &EdgeStoreHandle) -> Result<serde_json::Value, ConnectError> {
    handle
        .with_blocking(|store| {
            Ok(serde_json::json!({
                "node_count":      store.count_nodes().unwrap_or(0),
                "edge_count":      store.count_edges().unwrap_or(0),
                "last_sync_at_ms": store.last_sync_at().unwrap_or(None),
            }))
        })
        .await
}

// ── Offline queue ────────────────────────────────────────────────────────────

pub async fn open_offline_queue(
    handle: &OfflineQueueHandle,
    path: String,
) -> Result<(), ConnectError> {
    handle.open(PathBuf::from(path)).await
}

pub async fn offline_queue_stats(
    handle: &OfflineQueueHandle,
) -> Result<serde_json::Value, ConnectError> {
    handle
        .with_blocking(|q| {
            Ok(serde_json::json!({
                "pending":   q.count_by_status(ActionStatus::Pending).unwrap_or(0),
                "in_flight": q.count_by_status(ActionStatus::InFlight).unwrap_or(0),
                "completed": q.count_by_status(ActionStatus::Completed).unwrap_or(0),
                "failed":    q.count_by_status(ActionStatus::Failed).unwrap_or(0),
            }))
        })
        .await
}

pub async fn enqueue_offline_action(
    handle: &OfflineQueueHandle,
    action_type: String,
    payload: String,
) -> Result<String, ConnectError> {
    handle
        .with_blocking(move |q| {
            q.enqueue(&action_type, payload.as_bytes(), now_ms())
                .map_err(|e| ConnectError::Queue(format!("enqueue: {e}")))
        })
        .await
}

pub async fn list_pending_actions(
    handle: &OfflineQueueHandle,
    limit: usize,
) -> Result<serde_json::Value, ConnectError> {
    handle
        .with_blocking(move |q| {
            let actions = q
                .claim_batch(limit, now_ms())
                .map_err(|e| ConnectError::Queue(format!("claim: {e}")))?;
            Ok(serde_json::json!(actions
                .iter()
                .map(|a| serde_json::json!({
                    "id": a.id,
                    "action_type": a.action_type,
                    "queued_at": a.queued_at,
                    "attempt_count": a.attempt_count,
                    "status": match a.status {
                        ActionStatus::Pending   => "pending",
                        ActionStatus::InFlight  => "in_flight",
                        ActionStatus::Completed => "completed",
                        ActionStatus::Failed    => "failed",
                    },
                    "last_error": a.last_error,
                }))
                .collect::<Vec<_>>()))
        })
        .await
}

pub async fn mark_action_completed(
    handle: &OfflineQueueHandle,
    id: String,
) -> Result<(), ConnectError> {
    handle
        .with_blocking(move |q| {
            q.mark_completed(&id)
                .map_err(|e| ConnectError::Queue(format!("mark_completed: {e}")))
        })
        .await
}

pub async fn mark_action_failed(
    handle: &OfflineQueueHandle,
    id: String,
    error: String,
) -> Result<String, ConnectError> {
    handle
        .with_blocking(move |q| {
            let next = q
                .mark_failed(&id, &error)
                .map_err(|e| ConnectError::Queue(format!("mark_failed: {e}")))?;
            Ok(match next {
                ActionStatus::Pending => "pending",
                ActionStatus::InFlight => "in_flight",
                ActionStatus::Completed => "completed",
                ActionStatus::Failed => "failed",
            }
            .to_string())
        })
        .await
}

// ── Edge store queries ───────────────────────────────────────────────────────

pub async fn list_nodes_by_type(
    handle: &EdgeStoreHandle,
    node_type: String,
    limit: usize,
) -> Result<serde_json::Value, ConnectError> {
    handle
        .with_blocking(move |store| {
            let rows = store
                .list_by_type(&node_type, limit)
                .map_err(|e| ConnectError::Store(format!("list_by_type: {e}")))?;
            Ok(serde_json::json!(rows
                .iter()
                .map(|n| serde_json::json!({
                    "node_id": n.node_id,
                    "node_type": n.node_type,
                    "classification": n.classification,
                    "payload": n.payload,
                    "updated_at": n.updated_at,
                    "source_agent": n.source_agent,
                }))
                .collect::<Vec<_>>()))
        })
        .await
}

// ── Local inference (MLX / Ollama) ───────────────────────────────────────────

pub async fn local_backend(handle: &InferenceHandle) -> &'static str {
    handle.backend()
}

// ── Pairing & keychain ───────────────────────────────────────────────────────

pub async fn persist_pairing_bundle(
    app_data_dir: PathBuf,
    bundle: PairingBundle,
) -> Result<PersistedCredentials, ConnectError> {
    tokio::task::spawn_blocking(move || keychain::persist_pairing_bundle(&app_data_dir, &bundle))
        .await
        .map_err(|e| ConnectError::Internal(format!("join error: {e}")))?
}

pub async fn read_stored_jwt(device_id: String) -> Result<Option<String>, ConnectError> {
    tokio::task::spawn_blocking(move || keychain::read_stored_jwt(&device_id))
        .await
        .map_err(|e| ConnectError::Internal(format!("join error: {e}")))?
}

pub async fn forget_device(app_data_dir: PathBuf, device_id: String) -> Result<(), ConnectError> {
    tokio::task::spawn_blocking(move || keychain::forget_device(&app_data_dir, &device_id))
        .await
        .map_err(|e| ConnectError::Internal(format!("join error: {e}")))?
}

#[allow(dead_code)]
fn _path_marker(_p: &Path) {}

// ── Local inference (continued) ──────────────────────────────────────────────

pub async fn local_generate(
    handle: &InferenceHandle,
    model: String,
    prompt: String,
) -> Result<String, ConnectError> {
    let inf = handle.inference();
    inf.generate(&model, &prompt)
        .await
        .map_err(|e| ConnectError::Internal(format!("inference: {e}")))
}

/// Streaming variant. Each chunk is shaped as `{"text": String, "done": bool}`.
/// The caller is expected to be a Tauri `Channel<serde_json::Value>` so the
/// WebView gets a hot stream of tokens.
pub async fn local_generate_stream_inner<F>(
    handle: &InferenceHandle,
    model: String,
    prompt: String,
    mut emit: F,
) -> Result<(), ConnectError>
where
    F: FnMut(serde_json::Value) + Send + 'static,
{
    use futures::StreamExt;
    let inf = handle.inference();
    let mut stream = inf
        .generate_stream(&model, &prompt)
        .await
        .map_err(|e| ConnectError::Internal(format!("inference: {e}")))?;
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| ConnectError::Internal(format!("stream: {e}")))?;
        let payload = serde_json::json!({
            "text": chunk.text,
            "done": chunk.done,
            "eval_count": chunk.eval_count,
        });
        emit(payload);
    }
    Ok(())
}

fn now_ms() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
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

    #[tauri::command]
    pub async fn open_edge_store(
        handle: State<'_, EdgeStoreHandle>,
        path: String,
    ) -> Result<(), ConnectError> {
        super::open_edge_store(handle.inner(), path).await
    }

    #[tauri::command]
    pub async fn edge_store_stats(
        handle: State<'_, EdgeStoreHandle>,
    ) -> Result<serde_json::Value, ConnectError> {
        super::edge_store_stats(handle.inner()).await
    }

    #[tauri::command]
    pub async fn open_offline_queue(
        handle: State<'_, OfflineQueueHandle>,
        path: String,
    ) -> Result<(), ConnectError> {
        super::open_offline_queue(handle.inner(), path).await
    }

    #[tauri::command]
    pub async fn offline_queue_stats(
        handle: State<'_, OfflineQueueHandle>,
    ) -> Result<serde_json::Value, ConnectError> {
        super::offline_queue_stats(handle.inner()).await
    }

    #[tauri::command]
    pub async fn enqueue_offline_action(
        handle: State<'_, OfflineQueueHandle>,
        action_type: String,
        payload: String,
    ) -> Result<String, ConnectError> {
        super::enqueue_offline_action(handle.inner(), action_type, payload).await
    }

    #[tauri::command]
    pub async fn list_pending_actions(
        handle: State<'_, OfflineQueueHandle>,
        limit: usize,
    ) -> Result<serde_json::Value, ConnectError> {
        super::list_pending_actions(handle.inner(), limit).await
    }

    #[tauri::command]
    pub async fn mark_action_completed(
        handle: State<'_, OfflineQueueHandle>,
        id: String,
    ) -> Result<(), ConnectError> {
        super::mark_action_completed(handle.inner(), id).await
    }

    #[tauri::command]
    pub async fn mark_action_failed(
        handle: State<'_, OfflineQueueHandle>,
        id: String,
        error: String,
    ) -> Result<String, ConnectError> {
        super::mark_action_failed(handle.inner(), id, error).await
    }

    #[tauri::command]
    pub async fn list_nodes_by_type(
        handle: State<'_, EdgeStoreHandle>,
        node_type: String,
        limit: usize,
    ) -> Result<serde_json::Value, ConnectError> {
        super::list_nodes_by_type(handle.inner(), node_type, limit).await
    }

    #[tauri::command]
    pub async fn local_backend(handle: State<'_, InferenceHandle>) -> Result<String, ConnectError> {
        Ok(super::local_backend(handle.inner()).await.to_string())
    }

    #[tauri::command]
    pub async fn local_generate(
        handle: State<'_, InferenceHandle>,
        model: String,
        prompt: String,
    ) -> Result<String, ConnectError> {
        super::local_generate(handle.inner(), model, prompt).await
    }

    #[tauri::command]
    pub async fn persist_pairing_bundle(
        app: tauri::AppHandle,
        bundle: PairingBundle,
    ) -> Result<PersistedCredentials, ConnectError> {
        use tauri::Manager;
        let dir = app
            .path()
            .app_data_dir()
            .map_err(|e| ConnectError::Internal(format!("app_data_dir: {e}")))?;
        super::persist_pairing_bundle(dir, bundle).await
    }

    #[tauri::command]
    pub async fn read_stored_jwt(device_id: String) -> Result<Option<String>, ConnectError> {
        super::read_stored_jwt(device_id).await
    }

    #[tauri::command]
    pub async fn forget_device(
        app: tauri::AppHandle,
        device_id: String,
    ) -> Result<(), ConnectError> {
        use tauri::Manager;
        let dir = app
            .path()
            .app_data_dir()
            .map_err(|e| ConnectError::Internal(format!("app_data_dir: {e}")))?;
        super::forget_device(dir, device_id).await
    }

    #[tauri::command]
    pub async fn local_generate_stream(
        handle: State<'_, InferenceHandle>,
        model: String,
        prompt: String,
        channel: tauri::ipc::Channel<serde_json::Value>,
    ) -> Result<(), ConnectError> {
        super::local_generate_stream_inner(handle.inner(), model, prompt, move |chunk| {
            // Channel send returns Result; failures mean the WebView dropped
            // the listener — log and stop streaming further chunks (the loop
            // continues but emits will become no-ops).
            if let Err(e) = channel.send(chunk) {
                tracing::warn!(error = %e, "local_generate_stream: channel send failed");
            }
        })
        .await
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
                $crate::commands::__handlers::open_edge_store,
                $crate::commands::__handlers::edge_store_stats,
                $crate::commands::__handlers::open_offline_queue,
                $crate::commands::__handlers::offline_queue_stats,
                $crate::commands::__handlers::enqueue_offline_action,
                $crate::commands::__handlers::list_pending_actions,
                $crate::commands::__handlers::mark_action_completed,
                $crate::commands::__handlers::mark_action_failed,
                $crate::commands::__handlers::list_nodes_by_type,
                $crate::commands::__handlers::local_backend,
                $crate::commands::__handlers::local_generate,
                $crate::commands::__handlers::local_generate_stream,
                $crate::commands::__handlers::persist_pairing_bundle,
                $crate::commands::__handlers::read_stored_jwt,
                $crate::commands::__handlers::forget_device,
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
