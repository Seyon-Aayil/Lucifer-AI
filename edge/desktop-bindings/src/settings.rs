//! Persistent user settings for the desktop app.
//!
//! Stored as a small JSON file in the app data directory (no external plugin —
//! consistent with the file-based credential storage in [`crate::keychain`]).
//! The handle keeps an in-memory copy behind a mutex and writes through to disk
//! on every update.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;

use crate::types::ConnectError;

fn default_overlay_hotkey() -> String {
    "Super+Space".to_string()
}

fn default_inference_backend() -> String {
    "auto".to_string()
}

fn default_model() -> String {
    "llama3.2".to_string()
}

/// User-facing application settings. Every field has a default so an absent or
/// partial settings file still deserialises.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AppSettings {
    /// Global-shortcut accelerator that toggles the overlay, e.g. `Super+Space`.
    #[serde(default = "default_overlay_hotkey")]
    pub overlay_hotkey: String,
    /// Preferred local inference backend: `auto` | `ollama` | `mlx`.
    #[serde(default = "default_inference_backend")]
    pub inference_backend: String,
    /// Default model id used for local generation.
    #[serde(default = "default_model")]
    pub default_model: String,
    /// Device id assigned at pairing. Empty until the device is paired.
    #[serde(default)]
    pub device_id: String,
}

impl Default for AppSettings {
    fn default() -> Self {
        Self {
            overlay_hotkey: default_overlay_hotkey(),
            inference_backend: default_inference_backend(),
            default_model: default_model(),
            device_id: String::new(),
        }
    }
}

impl AppSettings {
    /// Load settings from `path`. A missing file yields defaults; a malformed
    /// file is an error (so we never silently clobber a user's bad edit).
    pub fn load(path: &Path) -> Result<Self, ConnectError> {
        match std::fs::read(path) {
            Ok(bytes) => serde_json::from_slice(&bytes)
                .map_err(|e| ConnectError::Internal(format!("settings parse: {e}"))),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Self::default()),
            Err(e) => Err(ConnectError::Internal(format!("settings read: {e}"))),
        }
    }

    /// Serialise to `path`, creating parent directories as needed.
    pub fn save(&self, path: &Path) -> Result<(), ConnectError> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| ConnectError::Internal(format!("settings mkdir: {e}")))?;
        }
        let bytes = serde_json::to_vec_pretty(self)
            .map_err(|e| ConnectError::Internal(format!("settings encode: {e}")))?;
        std::fs::write(path, bytes)
            .map_err(|e| ConnectError::Internal(format!("settings write: {e}")))
    }
}

/// Shared handle to the live settings + their backing file path.
#[derive(Default, Clone)]
pub struct SettingsHandle {
    inner: Arc<Mutex<AppSettings>>,
    path: Arc<Mutex<Option<PathBuf>>>,
}

impl SettingsHandle {
    /// Point the handle at `path` and load whatever is there (or defaults).
    pub async fn open(&self, path: PathBuf) -> Result<(), ConnectError> {
        let loaded = {
            let p = path.clone();
            tokio::task::spawn_blocking(move || AppSettings::load(&p))
                .await
                .map_err(|e| ConnectError::Internal(format!("join error: {e}")))??
        };
        *self.inner.lock().await = loaded;
        *self.path.lock().await = Some(path);
        Ok(())
    }

    /// Current settings snapshot.
    pub async fn get(&self) -> AppSettings {
        self.inner.lock().await.clone()
    }

    /// Replace settings and write through to disk (if a path is configured).
    pub async fn update(&self, next: AppSettings) -> Result<(), ConnectError> {
        *self.inner.lock().await = next.clone();
        if let Some(path) = self.path.lock().await.clone() {
            tokio::task::spawn_blocking(move || next.save(&path))
                .await
                .map_err(|e| ConnectError::Internal(format!("join error: {e}")))??;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_are_sensible() {
        let s = AppSettings::default();
        assert_eq!(s.overlay_hotkey, "Super+Space");
        assert_eq!(s.inference_backend, "auto");
        assert_eq!(s.default_model, "llama3.2");
    }

    #[test]
    fn missing_file_yields_defaults() {
        let p = std::env::temp_dir().join("lucifer-settings-does-not-exist-xyz.json");
        let _ = std::fs::remove_file(&p);
        assert_eq!(AppSettings::load(&p).unwrap(), AppSettings::default());
    }

    #[test]
    fn save_then_load_roundtrips() {
        let p = std::env::temp_dir().join("lucifer-settings-roundtrip.json");
        let s = AppSettings {
            overlay_hotkey: "Control+Shift+Space".into(),
            inference_backend: "mlx".into(),
            default_model: "qwen2.5".into(),
            device_id: "device-xyz".into(),
        };
        s.save(&p).unwrap();
        assert_eq!(AppSettings::load(&p).unwrap(), s);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn partial_json_fills_defaults() {
        let p = std::env::temp_dir().join("lucifer-settings-partial.json");
        std::fs::write(&p, br#"{"default_model":"phi3"}"#).unwrap();
        let s = AppSettings::load(&p).unwrap();
        assert_eq!(s.default_model, "phi3");
        assert_eq!(s.overlay_hotkey, "Super+Space"); // default filled
        let _ = std::fs::remove_file(&p);
    }
}
