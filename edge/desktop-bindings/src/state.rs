use std::sync::Arc;

use lucifer_sync_client::SyncClient;
use tokio::sync::Mutex;

use crate::types::ConnectError;

/// Shared, mutable handle to the (optional) connected `SyncClient`.
/// Registered on the Tauri `App` via `.manage()`.
#[derive(Default, Clone)]
pub struct ClientHandle {
    inner: Arc<Mutex<Option<SyncClient>>>,
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
        self.inner.lock().await.take()
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
