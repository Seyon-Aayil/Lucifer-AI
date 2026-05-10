use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use crate::error::{Error, Result};

/// Connection settings for an edge → master sync session.
///
/// Built from environment variables, a config file, or programmatically.
/// Validated via [`ClientConfig::validate`] before opening a transport.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClientConfig {
    /// Master gRPC endpoint, e.g. `https://lucifer.local:50051`.
    pub master_endpoint: String,

    /// Stable device identifier; included as the JWT subject and used
    /// to scope the hot-subgraph manifest.
    pub device_id: String,

    /// Path to the device's mTLS client certificate (PEM).
    pub client_cert: PathBuf,

    /// Path to the device's mTLS private key (PEM).
    pub client_key: PathBuf,

    /// Path to the master CA certificate the client uses to authenticate
    /// the server (PEM).
    pub ca_cert: PathBuf,

    /// Bearer token submitted as `authorization: Bearer <jwt>` metadata.
    /// Refreshed by the auth manager; this struct just carries the current
    /// value at connect time.
    pub jwt: String,

    /// Optional override for the SNI/`Host` name used during TLS verification.
    /// Defaults to the host portion of `master_endpoint`.
    pub sni_override: Option<String>,
}

impl ClientConfig {
    /// Reject configurations that cannot produce a working transport.
    pub fn validate(&self) -> Result<()> {
        if self.master_endpoint.is_empty() {
            return Err(Error::Config("master_endpoint is empty".into()));
        }
        if !self.master_endpoint.starts_with("https://")
            && !self.master_endpoint.starts_with("http://")
        {
            return Err(Error::Config(
                "master_endpoint must include scheme (https:// or http://)".into(),
            ));
        }
        if self.device_id.is_empty() {
            return Err(Error::Config("device_id is empty".into()));
        }
        if self.jwt.is_empty() {
            return Err(Error::Config("jwt is empty".into()));
        }
        for (label, path) in [
            ("client_cert", &self.client_cert),
            ("client_key", &self.client_key),
            ("ca_cert", &self.ca_cert),
        ] {
            if path.as_os_str().is_empty() {
                return Err(Error::Config(format!("{label} path is empty")));
            }
        }
        Ok(())
    }

    /// SNI hostname to present to the server during TLS handshake.
    /// Used by [`transport`](crate::transport) when building the channel.
    pub fn sni_hostname(&self) -> Result<String> {
        if let Some(name) = &self.sni_override {
            return Ok(name.clone());
        }
        let url = self
            .master_endpoint
            .split("://")
            .nth(1)
            .ok_or_else(|| Error::Config("master_endpoint missing host".into()))?;
        let host = url.split(':').next().unwrap_or(url);
        if host.is_empty() {
            return Err(Error::Config(
                "master_endpoint host segment is empty".into(),
            ));
        }
        Ok(host.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> ClientConfig {
        ClientConfig {
            master_endpoint: "https://lucifer.local:50051".into(),
            device_id: "device-mac".into(),
            client_cert: PathBuf::from("/tmp/c.pem"),
            client_key: PathBuf::from("/tmp/k.pem"),
            ca_cert: PathBuf::from("/tmp/ca.pem"),
            jwt: "eyJhbGc.payload.sig".into(),
            sni_override: None,
        }
    }

    #[test]
    fn valid_config_passes() {
        cfg().validate().unwrap();
    }

    #[test]
    fn empty_endpoint_rejected() {
        let mut c = cfg();
        c.master_endpoint = String::new();
        assert!(c.validate().is_err());
    }

    #[test]
    fn missing_scheme_rejected() {
        let mut c = cfg();
        c.master_endpoint = "lucifer.local:50051".into();
        assert!(c.validate().is_err());
    }

    #[test]
    fn empty_device_id_rejected() {
        let mut c = cfg();
        c.device_id = String::new();
        assert!(c.validate().is_err());
    }

    #[test]
    fn empty_jwt_rejected() {
        let mut c = cfg();
        c.jwt = String::new();
        assert!(c.validate().is_err());
    }

    #[test]
    fn empty_cert_path_rejected() {
        let mut c = cfg();
        c.client_cert = PathBuf::new();
        assert!(c.validate().is_err());
    }

    #[test]
    fn sni_defaults_to_endpoint_host() {
        let c = cfg();
        assert_eq!(c.sni_hostname().unwrap(), "lucifer.local");
    }

    #[test]
    fn sni_override_takes_precedence() {
        let mut c = cfg();
        c.sni_override = Some("override.example".into());
        assert_eq!(c.sni_hostname().unwrap(), "override.example");
    }

    #[test]
    fn sni_strips_port() {
        let mut c = cfg();
        c.master_endpoint = "https://10.0.0.5:50051".into();
        assert_eq!(c.sni_hostname().unwrap(), "10.0.0.5");
    }

    #[test]
    fn sni_handles_no_port() {
        let mut c = cfg();
        c.master_endpoint = "https://lucifer.local".into();
        assert_eq!(c.sni_hostname().unwrap(), "lucifer.local");
    }

    #[test]
    fn sni_missing_host_errors() {
        let mut c = cfg();
        c.master_endpoint = "https://".into();
        assert!(c.sni_hostname().is_err());
    }
}
