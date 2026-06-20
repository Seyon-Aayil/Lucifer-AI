//! Stable per-install device fingerprint.
//!
//! The master persists a device's fingerprint at pairing and rejects token
//! refreshes whose presented fingerprint doesn't match the stored one
//! (anti-clone, constant-time compare on the master side). We derive it from
//! the OS machine id (`IOPlatformUUID` on macOS, `/etc/machine-id` on Linux,
//! `MachineGuid` on Windows) and **hash it** so the raw hardware id never
//! leaves the device.

use sha2::{Digest, Sha256};

/// Return `sha256(machine_uid)` as lowercase hex, or an empty string if the
/// machine id can't be read (the master treats an absent fingerprint as
/// "legacy device" and skips the check, so this degrades safely).
pub fn device_fingerprint() -> String {
    match machine_uid::get() {
        Ok(uid) => {
            let digest = Sha256::digest(uid.as_bytes());
            hex_lower(&digest)
        }
        Err(e) => {
            tracing::warn!(error = %e, "could not read machine uid for fingerprint");
            String::new()
        }
    }
}

fn hex_lower(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(char::from_digit((b >> 4) as u32, 16).unwrap());
        s.push(char::from_digit((b & 0xf) as u32, 16).unwrap());
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fingerprint_is_stable_and_hex() {
        let a = device_fingerprint();
        let b = device_fingerprint();
        assert_eq!(a, b, "fingerprint must be stable across calls");
        // On any dev/CI host the machine id reads fine → 64-hex sha256.
        if !a.is_empty() {
            assert_eq!(a.len(), 64);
            assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        }
    }

    #[test]
    fn hex_lower_pads_bytes() {
        assert_eq!(hex_lower(&[0x00, 0x0f, 0xa0, 0xff]), "000fa0ff");
    }
}
