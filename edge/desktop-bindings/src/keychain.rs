//! macOS Keychain (and cross-platform fallbacks) helpers for the Lucifer
//! desktop bindings.
//!
//! Stores the short-lived JWT in the OS keyring under
//! `service = lucifer-desktop`, `user = jwt:<device_id>`.
//! Persists the per-device client cert + key + CA chain to disk in the
//! Tauri app-data directory with `0600` permissions on Unix.

use std::path::Path;

use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};

use crate::types::ConnectError;

const KEYRING_SERVICE: &str = "lucifer-desktop";

/// PEM bundle returned by the master `/devices/pair` endpoint.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PairingBundle {
    pub device_id: String,
    pub jwt: String,
    pub client_cert_pem_b64: String,
    pub client_key_pem_b64: String,
    pub ca_cert_pem_b64: String,
}

/// Materialised paths the WebView passes back into `connect_master`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PersistedCredentials {
    pub device_id: String,
    pub client_cert_path: String,
    pub client_key_path: String,
    pub ca_cert_path: String,
    /// True iff the JWT was persisted into the OS keyring. False indicates a
    /// fallback path (e.g. headless CI where keyring is unavailable) — the
    /// caller must hold the JWT in memory itself.
    pub jwt_in_keychain: bool,
}

/// Write the cert / key / CA PEM bytes into `app_data_dir / device_id /` and
/// store the JWT in the OS keychain under
/// `lucifer-desktop / jwt:<device_id>`.
pub fn persist_pairing_bundle(
    app_data_dir: &Path,
    bundle: &PairingBundle,
) -> Result<PersistedCredentials, ConnectError> {
    if bundle.device_id.is_empty() {
        return Err(ConnectError::Internal("device_id is empty".into()));
    }

    let dir = app_data_dir.join(sanitise(&bundle.device_id));
    std::fs::create_dir_all(&dir)
        .map_err(|e| ConnectError::Internal(format!("create cert dir {}: {e}", dir.display())))?;

    let cert_path = dir.join("client.pem");
    let key_path = dir.join("client.key");
    let ca_path = dir.join("ca.pem");

    write_pem(&cert_path, &bundle.client_cert_pem_b64)?;
    write_pem(&key_path, &bundle.client_key_pem_b64)?;
    write_pem(&ca_path, &bundle.ca_cert_pem_b64)?;

    let jwt_in_keychain = match write_jwt(&bundle.device_id, &bundle.jwt) {
        Ok(()) => true,
        Err(e) => {
            tracing::warn!(error = %e, "keyring unavailable; jwt left in caller's memory");
            false
        }
    };

    Ok(PersistedCredentials {
        device_id: bundle.device_id.clone(),
        client_cert_path: cert_path.to_string_lossy().into_owned(),
        client_key_path: key_path.to_string_lossy().into_owned(),
        ca_cert_path: ca_path.to_string_lossy().into_owned(),
        jwt_in_keychain,
    })
}

/// Retrieve a JWT previously stored via `persist_pairing_bundle`.
pub fn read_stored_jwt(device_id: &str) -> Result<Option<String>, ConnectError> {
    if device_id.is_empty() {
        return Err(ConnectError::Internal("device_id is empty".into()));
    }
    let entry = keyring_entry(device_id)?;
    match entry.get_password() {
        Ok(s) => Ok(Some(s)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(ConnectError::Internal(format!("keyring read: {e}"))),
    }
}

/// Erase JWT + on-disk PEMs for `device_id`. Best-effort: missing entries are
/// not errors.
pub fn forget_device(app_data_dir: &Path, device_id: &str) -> Result<(), ConnectError> {
    if device_id.is_empty() {
        return Err(ConnectError::Internal("device_id is empty".into()));
    }
    if let Ok(entry) = keyring_entry(device_id) {
        let _ = entry.delete_credential();
    }
    let dir = app_data_dir.join(sanitise(device_id));
    if dir.exists() {
        std::fs::remove_dir_all(&dir).map_err(|e| {
            ConnectError::Internal(format!("remove cert dir {}: {e}", dir.display()))
        })?;
    }
    Ok(())
}

// ── Internals ────────────────────────────────────────────────────────────────

fn write_pem(path: &Path, b64: &str) -> Result<(), ConnectError> {
    let bytes = STANDARD
        .decode(b64)
        .map_err(|e| ConnectError::Internal(format!("base64 decode {}: {e}", path.display())))?;
    std::fs::write(path, &bytes)
        .map_err(|e| ConnectError::Internal(format!("write {}: {e}", path.display())))?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let perm = std::fs::Permissions::from_mode(0o600);
        std::fs::set_permissions(path, perm)
            .map_err(|e| ConnectError::Internal(format!("chmod {}: {e}", path.display())))?;
    }
    Ok(())
}

fn write_jwt(device_id: &str, jwt: &str) -> Result<(), keyring::Error> {
    let entry = keyring::Entry::new(KEYRING_SERVICE, &keyring_user(device_id))?;
    entry.set_password(jwt)
}

fn keyring_entry(device_id: &str) -> Result<keyring::Entry, ConnectError> {
    keyring::Entry::new(KEYRING_SERVICE, &keyring_user(device_id))
        .map_err(|e| ConnectError::Internal(format!("keyring entry: {e}")))
}

fn keyring_user(device_id: &str) -> String {
    format!("jwt:{device_id}")
}

fn sanitise(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c.is_alphanumeric() || c == '-' || c == '_' || c == '.' {
                c
            } else {
                '_'
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::engine::general_purpose::STANDARD as B64;
    use tempfile::tempdir;

    fn b64(s: &str) -> String {
        B64.encode(s.as_bytes())
    }

    fn bundle(device_id: &str) -> PairingBundle {
        PairingBundle {
            device_id: device_id.into(),
            jwt: "header.payload.sig".into(),
            client_cert_pem_b64: b64(
                "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n",
            ),
            client_key_pem_b64: b64(
                "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
            ),
            ca_cert_pem_b64: b64("-----BEGIN CERTIFICATE-----\nca\n-----END CERTIFICATE-----\n"),
        }
    }

    #[test]
    fn persist_writes_three_pem_files_in_device_dir() {
        let tmp = tempdir().unwrap();
        let creds = persist_pairing_bundle(tmp.path(), &bundle("dev-mac")).unwrap();

        assert!(std::path::Path::new(&creds.client_cert_path).exists());
        assert!(std::path::Path::new(&creds.client_key_path).exists());
        assert!(std::path::Path::new(&creds.ca_cert_path).exists());

        let cert = std::fs::read_to_string(&creds.client_cert_path).unwrap();
        assert!(cert.starts_with("-----BEGIN CERTIFICATE-----"));
    }

    #[test]
    fn persist_rejects_empty_device_id() {
        let tmp = tempdir().unwrap();
        let mut b = bundle("");
        b.device_id = String::new();
        assert!(persist_pairing_bundle(tmp.path(), &b).is_err());
    }

    #[test]
    fn sanitise_strips_separators() {
        assert_eq!(sanitise("dev/mac:01"), "dev_mac_01");
        assert_eq!(sanitise("device-mac-01"), "device-mac-01");
    }

    #[cfg(unix)]
    #[test]
    fn pem_files_are_chmod_600() {
        use std::os::unix::fs::PermissionsExt;
        let tmp = tempdir().unwrap();
        let creds = persist_pairing_bundle(tmp.path(), &bundle("dev-mac")).unwrap();
        for path in [
            &creds.client_cert_path,
            &creds.client_key_path,
            &creds.ca_cert_path,
        ] {
            let perm = std::fs::metadata(path).unwrap().permissions();
            assert_eq!(perm.mode() & 0o777, 0o600, "{path} not 0600");
        }
    }

    #[test]
    fn forget_device_is_idempotent_when_missing() {
        let tmp = tempdir().unwrap();
        // Should not error even though nothing has been persisted.
        forget_device(tmp.path(), "never-paired").unwrap();
    }

    #[test]
    fn read_jwt_for_unknown_device_returns_none() {
        // Use a randomised device id so this test stays hermetic across runs.
        let id = format!("ghost-{}", uuid_like());
        let value = read_stored_jwt(&id).unwrap();
        assert!(value.is_none());
    }

    fn uuid_like() -> String {
        // Cheap unique id without pulling in another dep — use process ns time.
        use std::time::{SystemTime, UNIX_EPOCH};
        format!(
            "{}",
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        )
    }
}
