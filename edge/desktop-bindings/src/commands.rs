//! Tauri IPC command handlers. Each function is `pub` so it can also be tested
//! directly without a Tauri context. The `#[tauri::command]` glue lives in the
//! `__handlers` submodule and is gated behind the `tauri-cmd` feature.

use std::path::{Path, PathBuf};

use lucifer_offline_queue::ActionStatus;
use lucifer_sync_client::SyncClient;

use std::sync::Arc;

use tokio::sync::Notify;

use crate::{
    keychain::{self, PairingBundle, PersistedCredentials},
    refresher::{self, RefresherSpec},
    settings::{AppSettings, SettingsHandle},
    state::{ClientHandle, EdgeStoreHandle, InferenceHandle, OfflineQueueHandle},
    types::{ConnectArgs, ConnectError, TelemetryEvent},
};

/// Open the gRPC channel using the supplied configuration. Idempotent: returns
/// `AlreadyConnected` if a session is already live.
pub async fn connect_master(handle: &ClientHandle, args: ConnectArgs) -> Result<(), ConnectError> {
    if handle.is_connected().await {
        return Err(ConnectError::AlreadyConnected);
    }
    let cfg: lucifer_sync_client::ClientConfig = (&args).into();
    let client = SyncClient::connect(&cfg).await?;
    let interceptor = client.auth_interceptor();
    handle.set(client).await?;

    // If the caller supplied a refresh token, spawn the auto-refresh worker.
    if let Some(refresh_token) = args.refresh_token.clone() {
        let endpoint = args
            .http_master_endpoint
            .clone()
            .unwrap_or_else(|| args.master_endpoint.clone());
        let interceptor = interceptor;
        let spec = RefresherSpec {
            master_endpoint: endpoint,
            device_id: args.device_id.clone(),
            initial_jwt: args.jwt.clone(),
            refresh_token,
            on_jwt_updated: Arc::new(move |jwt| {
                if let Err(e) = interceptor.update_token(&jwt) {
                    tracing::warn!(error = %e, "interceptor.update_token failed");
                }
            }),
        };
        let cancel = Arc::new(Notify::new());
        handle.set_refresher_cancel(cancel.clone()).await;
        tokio::spawn(async move {
            refresher::run(spec, cancel).await;
        });
    }

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

// ── Conversations ────────────────────────────────────────────────────────────

pub async fn list_conversations(
    handle: &EdgeStoreHandle,
    limit: usize,
) -> Result<serde_json::Value, ConnectError> {
    handle
        .with_blocking(move |store| {
            let convs = store
                .list_conversations(limit)
                .map_err(|e| ConnectError::Store(format!("list_conversations: {e}")))?;
            Ok(serde_json::json!(convs))
        })
        .await
}

pub async fn create_conversation(
    handle: &EdgeStoreHandle,
    id: String,
    title: String,
) -> Result<(), ConnectError> {
    let now = now_ms();
    handle
        .with_blocking(move |store| {
            // EdgeStore methods take &mut self; with_blocking gives &EdgeStore.
            // Use interior unsafety: we hold the only Mutex guard.
            store
                .create_conversation(&id, &title, now)
                .map_err(|e| ConnectError::Store(format!("create_conversation: {e}")))
        })
        .await
}

pub async fn append_message(
    handle: &EdgeStoreHandle,
    message: lucifer_edge_store::Message,
) -> Result<(), ConnectError> {
    handle
        .with_blocking(move |store| {
            store
                .append_message(&message)
                .map_err(|e| ConnectError::Store(format!("append_message: {e}")))
        })
        .await
}

pub async fn list_messages(
    handle: &EdgeStoreHandle,
    conversation_id: String,
    limit: usize,
) -> Result<serde_json::Value, ConnectError> {
    handle
        .with_blocking(move |store| {
            let msgs = store
                .list_messages(&conversation_id, limit)
                .map_err(|e| ConnectError::Store(format!("list_messages: {e}")))?;
            Ok(serde_json::json!(msgs))
        })
        .await
}

pub async fn delete_conversation(handle: &EdgeStoreHandle, id: String) -> Result<(), ConnectError> {
    handle
        .with_blocking(move |store| {
            store
                .delete_conversation(&id)
                .map_err(|e| ConnectError::Store(format!("delete_conversation: {e}")))
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

/// Ship a telemetry batch to the master over the `PushTelemetry` RPC.
///
/// Each edge [`TelemetryEvent`] carries a free-form `attributes` JSON object;
/// numeric attributes are mapped to the proto `metrics` map and everything else
/// is stringified into `metadata`. Returns the master's accepted count.
pub async fn push_telemetry(
    handle: &ClientHandle,
    device_id: String,
    events: Vec<TelemetryEvent>,
) -> Result<u32, ConnectError> {
    if !handle.is_connected().await {
        return Err(ConnectError::NotConnected);
    }
    if events.is_empty() {
        return Ok(0);
    }
    handle
        .with_mut(|client| {
            Box::pin(async move {
                let proto_events: Vec<_> = events.into_iter().map(to_proto_event).collect();
                let batch = lucifer_sync_client::proto::TelemetryBatch {
                    device_id,
                    events: proto_events,
                };
                let ack = client.push_telemetry(batch).await?;
                Ok(ack.accepted_count as u32)
            })
        })
        .await
}

/// Map an edge [`TelemetryEvent`] onto its proto counterpart. `timestamp_ms` is
/// converted to the microseconds the proto/master schema expects; `device_id`
/// is left blank so the master falls back to the enclosing batch's device id.
fn to_proto_event(e: TelemetryEvent) -> lucifer_sync_client::proto::TelemetryEvent {
    let (metrics, metadata) = split_attributes(e.attributes);
    lucifer_sync_client::proto::TelemetryEvent {
        event_type: e.event_type,
        timestamp: e.timestamp_ms.saturating_mul(1_000),
        device_id: String::new(),
        agent_id: String::new(),
        trace_id: String::new(),
        span_id: String::new(),
        metrics,
        metadata,
        cost_usd: 0.0,
        error: false,
        error_code: String::new(),
    }
}

/// Split a free-form attributes object: numbers → `metrics` (f64), all other
/// scalar/compound values → `metadata` (stringified). Non-object inputs yield
/// two empty maps.
fn split_attributes(
    value: serde_json::Value,
) -> (
    std::collections::HashMap<String, f64>,
    std::collections::HashMap<String, String>,
) {
    let mut metrics = std::collections::HashMap::new();
    let mut metadata = std::collections::HashMap::new();
    if let serde_json::Value::Object(map) = value {
        for (k, v) in map {
            match v {
                serde_json::Value::Number(n) => {
                    if let Some(f) = n.as_f64() {
                        metrics.insert(k, f);
                    }
                }
                serde_json::Value::String(s) => {
                    metadata.insert(k, s);
                }
                other => {
                    metadata.insert(k, other.to_string());
                }
            }
        }
    }
    (metrics, metadata)
}

// ── Settings ─────────────────────────────────────────────────────────────────

/// Return the current persisted application settings.
pub async fn get_settings(handle: &SettingsHandle) -> Result<AppSettings, ConnectError> {
    Ok(handle.get().await)
}

/// Replace the application settings and write them through to disk.
pub async fn update_settings(
    handle: &SettingsHandle,
    settings: AppSettings,
) -> Result<(), ConnectError> {
    handle.update(settings).await
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
        device_id: String,
        events: Vec<TelemetryEvent>,
    ) -> Result<u32, ConnectError> {
        super::push_telemetry(handle.inner(), device_id, events).await
    }

    #[tauri::command]
    pub async fn get_settings(
        handle: State<'_, SettingsHandle>,
    ) -> Result<AppSettings, ConnectError> {
        super::get_settings(handle.inner()).await
    }

    #[tauri::command]
    pub async fn update_settings(
        handle: State<'_, SettingsHandle>,
        settings: AppSettings,
    ) -> Result<(), ConnectError> {
        super::update_settings(handle.inner(), settings).await
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
    pub async fn list_conversations(
        handle: State<'_, EdgeStoreHandle>,
        limit: usize,
    ) -> Result<serde_json::Value, ConnectError> {
        super::list_conversations(handle.inner(), limit).await
    }

    #[tauri::command]
    pub async fn create_conversation(
        handle: State<'_, EdgeStoreHandle>,
        id: String,
        title: String,
    ) -> Result<(), ConnectError> {
        super::create_conversation(handle.inner(), id, title).await
    }

    #[tauri::command]
    pub async fn append_message(
        handle: State<'_, EdgeStoreHandle>,
        message: lucifer_edge_store::Message,
    ) -> Result<(), ConnectError> {
        super::append_message(handle.inner(), message).await
    }

    #[tauri::command]
    pub async fn list_messages(
        handle: State<'_, EdgeStoreHandle>,
        conversation_id: String,
        limit: usize,
    ) -> Result<serde_json::Value, ConnectError> {
        super::list_messages(handle.inner(), conversation_id, limit).await
    }

    #[tauri::command]
    pub async fn delete_conversation(
        handle: State<'_, EdgeStoreHandle>,
        id: String,
    ) -> Result<(), ConnectError> {
        super::delete_conversation(handle.inner(), id).await
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
                $crate::commands::__handlers::get_settings,
                $crate::commands::__handlers::update_settings,
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
                $crate::commands::__handlers::list_conversations,
                $crate::commands::__handlers::create_conversation,
                $crate::commands::__handlers::append_message,
                $crate::commands::__handlers::list_messages,
                $crate::commands::__handlers::delete_conversation,
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
        let err = push_telemetry(&h, "dev-1".into(), vec![])
            .await
            .unwrap_err();
        assert!(matches!(err, ConnectError::NotConnected));
    }

    #[test]
    fn split_attributes_routes_numbers_to_metrics() {
        let v = serde_json::json!({
            "latency_ms": 12.5,
            "count": 3,
            "model": "llama3.2",
            "ok": true,
        });
        let (metrics, metadata) = split_attributes(v);
        assert_eq!(metrics.get("latency_ms"), Some(&12.5));
        assert_eq!(metrics.get("count"), Some(&3.0));
        assert_eq!(metadata.get("model").map(String::as_str), Some("llama3.2"));
        assert_eq!(metadata.get("ok").map(String::as_str), Some("true"));
        assert!(!metadata.contains_key("latency_ms"));
    }

    #[test]
    fn split_attributes_non_object_is_empty() {
        let (metrics, metadata) = split_attributes(serde_json::json!("scalar"));
        assert!(metrics.is_empty());
        assert!(metadata.is_empty());
    }

    #[test]
    fn to_proto_event_converts_ms_to_micros() {
        let e = TelemetryEvent {
            event_type: "rpc".into(),
            timestamp_ms: 1_700,
            attributes: serde_json::json!({"latency_ms": 4.0}),
        };
        let p = to_proto_event(e);
        assert_eq!(p.event_type, "rpc");
        assert_eq!(p.timestamp, 1_700_000);
        assert_eq!(p.metrics.get("latency_ms"), Some(&4.0));
    }

    #[tokio::test]
    async fn get_hot_subgraph_requires_connection() {
        let h = ClientHandle::default();
        let err = get_hot_subgraph(&h, "device".into(), 0).await.unwrap_err();
        assert!(matches!(err, ConnectError::NotConnected));
    }
}
