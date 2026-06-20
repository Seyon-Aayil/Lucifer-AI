-- 0002: device hardware fingerprint
-- Stores the client-derived hardware fingerprint (TPM / Secure Enclave hash)
-- presented at pairing time. Nullable: devices paired before this migration
-- (or clients that don't send one) have no fingerprint and are not subject
-- to fingerprint checks on token refresh.

ALTER TABLE devices ADD COLUMN IF NOT EXISTS fingerprint TEXT;
