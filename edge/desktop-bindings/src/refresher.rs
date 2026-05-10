//! JWT auto-refresh worker.
//!
//! Spawned at `connect_master` time. Parses the current JWT's `exp` claim,
//! sleeps until 5 minutes before expiry, hits the master's
//! `POST /auth/token/refresh` endpoint, and atomically swaps the new token
//! into the gRPC `AuthInterceptor` so existing channels keep working.
//!
//! Failures are logged and retried after a backoff. On terminal failure
//! (refresh token replay, device revoked) the worker exits — caller is
//! responsible for prompting the user to re-pair.

use std::sync::Arc;
use std::time::Duration;

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use tokio::sync::Notify;
use tokio::time::sleep;

use crate::keychain;

const REFRESH_LEAD_SECONDS: i64 = 300;
const RETRY_BACKOFF_SECONDS: u64 = 30;

#[derive(Clone)]
pub struct RefresherSpec {
    pub master_endpoint: String,
    pub device_id: String,
    pub initial_jwt: String,
    pub refresh_token: String,
    /// Callback invoked with the new JWT each time a refresh succeeds. The
    /// concrete implementation in `desktop-bindings` plumbs this into the
    /// gRPC `AuthInterceptor::update_token`.
    pub on_jwt_updated: Arc<dyn Fn(String) + Send + Sync>,
}

impl std::fmt::Debug for RefresherSpec {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RefresherSpec")
            .field("master_endpoint", &self.master_endpoint)
            .field("device_id", &self.device_id)
            .field("initial_jwt", &"<redacted>")
            .field("refresh_token", &"<redacted>")
            .finish()
    }
}

#[derive(Debug, Serialize)]
struct RefreshRequest<'a> {
    device_id: &'a str,
    refresh_token: &'a str,
}

#[derive(Debug, Deserialize)]
struct RefreshResponse {
    access_token: String,
    refresh_token: String,
    #[serde(default)]
    #[allow(dead_code)]
    access_expires_in: u64,
    #[serde(default)]
    #[allow(dead_code)]
    refresh_expires_in: u64,
}

/// Single tick: parse the JWT exp, sleep until 5 min before, swap.
/// Returns the new (jwt, refresh_token) on success.
pub async fn refresh_once(spec: &RefresherSpec) -> Result<(String, String), RefreshError> {
    let url = format!(
        "{}/auth/token/refresh",
        spec.master_endpoint.trim_end_matches('/')
    );
    let body = RefreshRequest {
        device_id: &spec.device_id,
        refresh_token: &spec.refresh_token,
    };
    let resp = Client::builder()
        .timeout(Duration::from_secs(15))
        .build()
        .map_err(|e| RefreshError::Http(e.to_string()))?
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| RefreshError::Http(e.to_string()))?;
    if !resp.status().is_success() {
        let status = resp.status();
        let body = resp.text().await.unwrap_or_default();
        return Err(RefreshError::Server(status.as_u16(), body));
    }
    let parsed: RefreshResponse = resp
        .json()
        .await
        .map_err(|e| RefreshError::Decode(e.to_string()))?;
    Ok((parsed.access_token, parsed.refresh_token))
}

/// Long-running task. Loops until cancelled via `cancel.notify_one()` or a
/// terminal server error is returned.
pub async fn run(spec: RefresherSpec, cancel: Arc<Notify>) {
    let mut current_jwt = spec.initial_jwt.clone();
    let mut current_refresh = spec.refresh_token.clone();

    loop {
        let secs_until_refresh = compute_sleep_seconds(&current_jwt);
        tracing::debug!(
            secs = secs_until_refresh,
            device_id = %spec.device_id,
            "jwt-refresher: scheduled next refresh"
        );

        tokio::select! {
            _ = cancel.notified() => {
                tracing::info!(device_id = %spec.device_id, "jwt-refresher: cancelled");
                return;
            }
            _ = sleep(Duration::from_secs(secs_until_refresh)) => {}
        }

        let live_spec = RefresherSpec {
            initial_jwt: current_jwt.clone(),
            refresh_token: current_refresh.clone(),
            ..spec.clone()
        };
        match refresh_once(&live_spec).await {
            Ok((jwt, refresh)) => {
                current_jwt = jwt.clone();
                current_refresh = refresh.clone();
                if let Err(e) = keychain::replace_stored_jwt(&spec.device_id, &jwt) {
                    tracing::warn!(error = %e, "jwt-refresher: keychain jwt write failed");
                }
                if let Err(e) = keychain::replace_stored_refresh_token(&spec.device_id, &refresh) {
                    tracing::warn!(error = %e, "jwt-refresher: keychain refresh write failed");
                }
                (spec.on_jwt_updated)(jwt);
                tracing::info!(device_id = %spec.device_id, "jwt-refresher: rotated");
            }
            Err(RefreshError::Server(status, body)) if (400..500).contains(&status) => {
                tracing::error!(
                    status,
                    %body,
                    device_id = %spec.device_id,
                    "jwt-refresher: terminal server error — exiting"
                );
                return;
            }
            Err(e) => {
                tracing::warn!(error = %e, "jwt-refresher: transient error — backing off");
                sleep(Duration::from_secs(RETRY_BACKOFF_SECONDS)).await;
            }
        }
    }
}

/// Decode the JWT's `exp` claim and compute seconds until the next refresh
/// should fire. `REFRESH_LEAD_SECONDS` before expiry. Falls back to the
/// access-token TTL if the token can't be parsed.
pub fn compute_sleep_seconds(jwt: &str) -> u64 {
    let exp = jwt_exp_seconds(jwt).unwrap_or(0);
    if exp == 0 {
        // Conservative fallback — wake every 15 min.
        return 15 * 60;
    }
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let target = exp - REFRESH_LEAD_SECONDS;
    if target <= now {
        return 1; // already overdue — fire immediately
    }
    (target - now) as u64
}

fn jwt_exp_seconds(jwt: &str) -> Option<i64> {
    let parts: Vec<&str> = jwt.split('.').collect();
    if parts.len() != 3 {
        return None;
    }
    let payload_bytes = URL_SAFE_NO_PAD.decode(parts[1]).ok()?;
    let payload: serde_json::Value = serde_json::from_slice(&payload_bytes).ok()?;
    payload.get("exp").and_then(|v| v.as_i64())
}

#[derive(Debug, thiserror::Error)]
pub enum RefreshError {
    #[error("http error: {0}")]
    Http(String),
    #[error("server returned {0}: {1}")]
    Server(u16, String),
    #[error("decode error: {0}")]
    Decode(String),
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::engine::general_purpose::URL_SAFE_NO_PAD as B64;

    fn make_jwt(exp_seconds_from_now: i64) -> String {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64;
        let header = B64.encode(br#"{"alg":"HS256","typ":"JWT"}"#);
        let payload = B64.encode(
            serde_json::to_vec(&serde_json::json!({
                "sub": "device-x",
                "exp": now + exp_seconds_from_now,
            }))
            .unwrap(),
        );
        let sig = B64.encode(b"sig");
        format!("{header}.{payload}.{sig}")
    }

    #[test]
    fn jwt_exp_parses() {
        let jwt = make_jwt(3600);
        assert!(jwt_exp_seconds(&jwt).is_some());
    }

    #[test]
    fn malformed_jwt_returns_none() {
        assert!(jwt_exp_seconds("not.a.jwt").is_none());
        assert!(jwt_exp_seconds("only.two").is_none());
        assert!(jwt_exp_seconds("").is_none());
    }

    #[test]
    fn sleep_seconds_fires_immediately_when_overdue() {
        let jwt = make_jwt(60); // expires in 60s, lead is 300s → already overdue
        assert_eq!(compute_sleep_seconds(&jwt), 1);
    }

    #[test]
    fn sleep_seconds_respects_lead() {
        let jwt = make_jwt(REFRESH_LEAD_SECONDS + 600); // 10 min before refresh point
        let secs = compute_sleep_seconds(&jwt);
        assert!((595..=605).contains(&(secs as i64)), "got {secs}");
    }

    #[test]
    fn unparseable_jwt_falls_back_to_15min() {
        assert_eq!(compute_sleep_seconds("garbage"), 15 * 60);
    }
}
