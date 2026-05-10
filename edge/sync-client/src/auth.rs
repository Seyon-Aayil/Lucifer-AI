use std::sync::{Arc, RwLock};

use tonic::{
    metadata::{Ascii, MetadataValue},
    service::Interceptor,
    Request, Status,
};

use crate::error::{Error, Result};

/// Client-side gRPC interceptor that injects the bearer JWT into every RPC.
///
/// Mirrors the master's `master.sync.auth.DeviceAuthInterceptor`: each call
/// must carry `authorization: Bearer <jwt>` metadata. The token is stored
/// behind an `RwLock` so a background refresh task can swap it without
/// recreating the channel.
#[derive(Clone, Debug)]
pub struct AuthInterceptor {
    token: Arc<RwLock<MetadataValue<Ascii>>>,
}

impl AuthInterceptor {
    /// Build an interceptor from an initial JWT.
    pub fn new(jwt: &str) -> Result<Self> {
        let value = encode_bearer(jwt)?;
        Ok(Self {
            token: Arc::new(RwLock::new(value)),
        })
    }

    /// Atomically replace the stored token. Subsequent RPCs use the new value.
    pub fn update_token(&self, jwt: &str) -> Result<()> {
        let value = encode_bearer(jwt)?;
        let mut guard = self
            .token
            .write()
            .map_err(|e| Error::Auth(format!("token lock poisoned: {e}")))?;
        *guard = value;
        Ok(())
    }
}

impl Interceptor for AuthInterceptor {
    fn call(&mut self, mut req: Request<()>) -> std::result::Result<Request<()>, Status> {
        let token = self
            .token
            .read()
            .map_err(|e| Status::internal(format!("auth lock poisoned: {e}")))?
            .clone();
        req.metadata_mut().insert("authorization", token);
        Ok(req)
    }
}

fn encode_bearer(jwt: &str) -> Result<MetadataValue<Ascii>> {
    if jwt.is_empty() {
        return Err(Error::Auth("jwt is empty".into()));
    }
    let header = format!("Bearer {jwt}");
    header
        .parse::<MetadataValue<Ascii>>()
        .map_err(|e| Error::Auth(format!("token contains non-ASCII bytes: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_empty_jwt() {
        assert!(AuthInterceptor::new("").is_err());
    }

    #[test]
    fn injects_bearer_metadata() {
        let mut interceptor = AuthInterceptor::new("test-jwt").unwrap();
        let req = Request::new(());
        let out = interceptor.call(req).unwrap();
        let header = out.metadata().get("authorization").unwrap();
        assert_eq!(header.to_str().unwrap(), "Bearer test-jwt");
    }

    #[test]
    fn update_token_swaps_value() {
        let interceptor = AuthInterceptor::new("first").unwrap();
        interceptor.update_token("second").unwrap();
        let mut clone = interceptor.clone();
        let out = clone.call(Request::new(())).unwrap();
        assert_eq!(
            out.metadata()
                .get("authorization")
                .unwrap()
                .to_str()
                .unwrap(),
            "Bearer second"
        );
    }

    #[test]
    fn update_rejects_empty() {
        let interceptor = AuthInterceptor::new("first").unwrap();
        assert!(interceptor.update_token("").is_err());
    }
}
