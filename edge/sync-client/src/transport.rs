use std::time::Duration;

use tokio::fs;
use tonic::{
    codegen::InterceptedService,
    transport::{Certificate, Channel, ClientTlsConfig, Endpoint, Identity},
};

use crate::{
    auth::AuthInterceptor,
    config::ClientConfig,
    error::{Error, Result},
    proto::{
        lucifer_sync_client::LuciferSyncClient, PushAck, SubgraphRequest, SubgraphResponse,
        SyncMessage, TelemetryBatch,
    },
};

/// High-level wrapper around the generated `LuciferSyncClient`.
///
/// Hides mTLS setup, JWT auth, and exposes typed helpers for the three RPCs
/// declared in `lucifer_sync.proto`. Constructed via [`SyncClient::connect`].
pub struct SyncClient {
    inner: LuciferSyncClient<InterceptedService<Channel, AuthInterceptor>>,
    interceptor: AuthInterceptor,
}

impl SyncClient {
    /// Open a TLS-protected channel to the master endpoint and wrap it with
    /// the JWT auth interceptor.
    pub async fn connect(cfg: &ClientConfig) -> Result<Self> {
        cfg.validate()?;

        let cert = fs::read(&cfg.client_cert)
            .await
            .map_err(|e| Error::Tls(format!("read client_cert: {e}")))?;
        let key = fs::read(&cfg.client_key)
            .await
            .map_err(|e| Error::Tls(format!("read client_key: {e}")))?;
        let ca = fs::read(&cfg.ca_cert)
            .await
            .map_err(|e| Error::Tls(format!("read ca_cert: {e}")))?;

        let tls = ClientTlsConfig::new()
            .ca_certificate(Certificate::from_pem(&ca))
            .identity(Identity::from_pem(&cert, &key))
            .domain_name(cfg.sni_hostname()?);

        let endpoint = Endpoint::from_shared(cfg.master_endpoint.clone())
            .map_err(|e| Error::Config(format!("invalid endpoint: {e}")))?
            .tls_config(tls)?
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(30))
            .keep_alive_while_idle(true);

        let channel = endpoint.connect().await?;
        let interceptor = AuthInterceptor::new(&cfg.jwt)?;
        let inner = LuciferSyncClient::with_interceptor(channel, interceptor.clone());

        Ok(Self { inner, interceptor })
    }

    /// Cloneable handle to the channel-level auth interceptor. Used by the
    /// JWT auto-refresh worker to swap a freshly-issued bearer token without
    /// recreating the gRPC channel.
    pub fn auth_interceptor(&self) -> AuthInterceptor {
        self.interceptor.clone()
    }

    /// Bidirectional streaming sync. The caller drives the outbound stream;
    /// the returned stream yields server responses as they arrive.
    pub async fn sync_stream<S>(
        &mut self,
        outbound: S,
    ) -> Result<tonic::codec::Streaming<SyncMessage>>
    where
        S: futures::Stream<Item = SyncMessage> + Send + 'static,
    {
        let response = self.inner.sync_stream(outbound).await?;
        Ok(response.into_inner())
    }

    /// Pull the device's hot-subgraph manifest. Used on cold-start sync or
    /// after long offline periods.
    pub async fn get_hot_subgraph(&mut self, request: SubgraphRequest) -> Result<SubgraphResponse> {
        let response = self.inner.get_hot_subgraph(request).await?;
        Ok(response.into_inner())
    }

    /// Push a telemetry batch (fire-and-forget on the master side; we still
    /// surface the ack for backpressure-aware retries).
    pub async fn push_telemetry(&mut self, batch: TelemetryBatch) -> Result<PushAck> {
        let response = self.inner.push_telemetry(batch).await?;
        Ok(response.into_inner())
    }
}
