use std::{path::PathBuf, sync::Arc};

use lucifer_edge_store::EdgeStore;
use lucifer_mlx_runtime::Inference;
use lucifer_offline_queue::OfflineQueue;
use lucifer_sync_client::SyncClient;
use tokio::sync::Mutex;

use crate::types::ConnectError;

/// Shared, mutable handle to the (optional) connected `SyncClient`.
/// Registered on the Tauri `App` via `.manage()`.
#[derive(Default, Clone)]
pub struct ClientHandle {
    inner: Arc<Mutex<Option<SyncClient>>>,
    refresher_cancel: Arc<Mutex<Option<Arc<tokio::sync::Notify>>>>,
}

impl ClientHandle {
    pub async fn set(&self, client: SyncClient) -> Result<(), ConnectError> {
        let mut guard = self.inner.lock().await;
        if guard.is_some() {
            return Err(ConnectError::AlreadyConnected);
        }
        *guard = Some(client);
        Ok(())
    }

    pub async fn take(&self) -> Option<SyncClient> {
        // Cancel any in-flight refresher first.
        if let Some(notify) = self.refresher_cancel.lock().await.take() {
            notify.notify_one();
        }
        self.inner.lock().await.take()
    }

    /// Register a notify handle the next disconnect can use to stop the
    /// background JWT refresher.
    pub async fn set_refresher_cancel(&self, notify: Arc<tokio::sync::Notify>) {
        *self.refresher_cancel.lock().await = Some(notify);
    }

    pub async fn is_connected(&self) -> bool {
        self.inner.lock().await.is_some()
    }

    /// Run `f` against the live client. Returns `NotConnected` if the channel
    /// hasn't been opened yet.
    pub async fn with_mut<F, R>(&self, f: F) -> Result<R, ConnectError>
    where
        F: for<'a> FnOnce(
            &'a mut SyncClient,
        ) -> std::pin::Pin<
            Box<dyn std::future::Future<Output = Result<R, ConnectError>> + Send + 'a>,
        >,
    {
        let mut guard = self.inner.lock().await;
        let client = guard.as_mut().ok_or(ConnectError::NotConnected)?;
        f(client).await
    }
}

/// Per-process EdgeStore handle. Path is captured so blocking ops can re-open
/// or seed it as needed; the open Connection lives behind a tokio Mutex.
#[derive(Default, Clone)]
pub struct EdgeStoreHandle {
    inner: Arc<Mutex<Option<EdgeStore>>>,
    path: Arc<Mutex<Option<PathBuf>>>,
}

impl EdgeStoreHandle {
    pub async fn open(&self, path: PathBuf) -> Result<(), ConnectError> {
        let store_path = path.clone();
        let store = tokio::task::spawn_blocking(move || EdgeStore::open(&store_path))
            .await
            .map_err(|e| ConnectError::Internal(format!("join error: {e}")))?
            .map_err(|e| ConnectError::Store(format!("open: {e}")))?;
        *self.inner.lock().await = Some(store);
        *self.path.lock().await = Some(path);
        Ok(())
    }

    pub async fn is_open(&self) -> bool {
        self.inner.lock().await.is_some()
    }

    pub async fn path(&self) -> Option<PathBuf> {
        self.path.lock().await.clone()
    }

    /// Run a synchronous closure against the underlying store. Wraps the call
    /// in `spawn_blocking` so we don't stall the async runtime on SQLite.
    pub async fn with_blocking<F, R>(&self, f: F) -> Result<R, ConnectError>
    where
        F: FnOnce(&EdgeStore) -> Result<R, ConnectError> + Send + 'static,
        R: Send + 'static,
    {
        let inner = self.inner.clone();
        tokio::task::spawn_blocking(move || {
            let guard = inner.blocking_lock();
            let store = guard.as_ref().ok_or(ConnectError::NotConnected)?;
            f(store)
        })
        .await
        .map_err(|e| ConnectError::Internal(format!("join error: {e}")))?
    }
}

/// Per-process OfflineQueue handle, modelled identically to EdgeStoreHandle.
#[derive(Default, Clone)]
pub struct OfflineQueueHandle {
    inner: Arc<Mutex<Option<OfflineQueue>>>,
    path: Arc<Mutex<Option<PathBuf>>>,
}

impl OfflineQueueHandle {
    pub async fn open(&self, path: PathBuf) -> Result<(), ConnectError> {
        let queue_path = path.clone();
        let queue = tokio::task::spawn_blocking(move || OfflineQueue::open(&queue_path))
            .await
            .map_err(|e| ConnectError::Internal(format!("join error: {e}")))?
            .map_err(|e| ConnectError::Queue(format!("open: {e}")))?;
        *self.inner.lock().await = Some(queue);
        *self.path.lock().await = Some(path);
        Ok(())
    }

    pub async fn is_open(&self) -> bool {
        self.inner.lock().await.is_some()
    }

    pub async fn path(&self) -> Option<PathBuf> {
        self.path.lock().await.clone()
    }

    pub async fn with_blocking<F, R>(&self, f: F) -> Result<R, ConnectError>
    where
        F: FnOnce(&mut OfflineQueue) -> Result<R, ConnectError> + Send + 'static,
        R: Send + 'static,
    {
        let inner = self.inner.clone();
        tokio::task::spawn_blocking(move || {
            let mut guard = inner.blocking_lock();
            let queue = guard.as_mut().ok_or(ConnectError::NotConnected)?;
            f(queue)
        })
        .await
        .map_err(|e| ConnectError::Internal(format!("join error: {e}")))?
    }
}

/// Handle to the chosen local-inference backend (MLX or Ollama). Wraps an
/// `Arc<dyn Inference>` selected at startup via
/// `lucifer_mlx_runtime::select_backend`.
#[derive(Clone)]
pub struct InferenceHandle {
    inner: Arc<dyn Inference>,
    backend_label: &'static str,
}

impl InferenceHandle {
    pub fn new(inner: Arc<dyn Inference>) -> Self {
        let backend_label = inner.name();
        Self {
            inner,
            backend_label,
        }
    }

    pub fn backend(&self) -> &'static str {
        self.backend_label
    }

    pub fn inference(&self) -> Arc<dyn Inference> {
        self.inner.clone()
    }
}
