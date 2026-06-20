//! Payload-HMAC signing for outbound `SyncMessage`s.
//!
//! The master verifies a keyed HMAC over every `SyncMessage` (see
//! `master/sync/server.py::_verify_hmac`). The contract is:
//!
//! ```text
//! payload_hmac = HMAC-SHA256(key, encode(message with payload_hmac cleared))
//! ```
//!
//! full 32 bytes. `key` is **per device**: the master derives it as
//! `HKDF-SHA256(app_secret_key, info = "lucifer-sync-payload-hmac:" + device_id)`
//! and hands the edge its own key in the pairing bundle
//! (`PairResponse.payload_hmac_key_b64`). Clearing the field before encoding is
//! what lets the MAC avoid covering itself — prost omits an empty `bytes` field
//! (default value), exactly as Python's `ClearField` drops it from
//! `SerializeToString`, so both sides hash identical wire bytes.

use hmac::{Hmac, Mac};
use prost::Message;
use sha2::Sha256;

use crate::proto::SyncMessage;

type HmacSha256 = Hmac<Sha256>;

/// Sign `msg` in place: compute the payload HMAC under `key` and store it in
/// `msg.payload_hmac`. Any pre-existing MAC is overwritten (the field is
/// cleared before hashing), so re-signing is idempotent for a fixed key.
pub fn sign_message(key: &[u8], msg: &mut SyncMessage) {
    msg.payload_hmac.clear();
    let wire = msg.encode_to_vec();
    let mut mac = HmacSha256::new_from_slice(key).expect("HMAC-SHA256 accepts any key length");
    mac.update(&wire);
    msg.payload_hmac = mac.finalize().into_bytes().to_vec();
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> SyncMessage {
        SyncMessage {
            device_id: "device-mac-01".to_string(),
            sync_session_id: "sess-1".to_string(),
            vector_clock: 42,
            ..Default::default()
        }
    }

    /// The produced MAC matches an independent computation that mirrors the
    /// master's contract: clear field 7, encode, HMAC-SHA256.
    #[test]
    fn signature_matches_contract() {
        let key = (0u8..32).collect::<Vec<u8>>();
        let mut msg = sample();
        sign_message(&key, &mut msg);

        let mut bare = msg.clone();
        bare.payload_hmac.clear();
        let mut mac = HmacSha256::new_from_slice(&key).unwrap();
        mac.update(&bare.encode_to_vec());
        let expected = mac.finalize().into_bytes().to_vec();

        assert_eq!(msg.payload_hmac, expected);
        assert_eq!(msg.payload_hmac.len(), 32);
    }

    #[test]
    fn signing_is_idempotent() {
        let key = b"per-device-key-bytes";
        let mut a = sample();
        let mut b = sample();
        sign_message(key, &mut a);
        sign_message(key, &mut b);
        assert_eq!(a.payload_hmac, b.payload_hmac);

        // Re-signing the already-signed message yields the same MAC.
        let once = a.payload_hmac.clone();
        sign_message(key, &mut a);
        assert_eq!(a.payload_hmac, once);
    }

    #[test]
    fn different_keys_differ() {
        let mut a = sample();
        let mut b = sample();
        sign_message(b"key-a", &mut a);
        sign_message(b"key-b", &mut b);
        assert_ne!(a.payload_hmac, b.payload_hmac);
    }

    #[test]
    fn tamper_changes_mac() {
        let key = b"k";
        let mut a = sample();
        sign_message(key, &mut a);
        let before = a.payload_hmac.clone();

        let mut b = sample();
        b.vector_clock = 43; // mutate a covered field
        sign_message(key, &mut b);
        assert_ne!(before, b.payload_hmac);
    }
}
