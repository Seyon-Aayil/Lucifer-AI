//! Lucifer offline action queue.
//!
//! SQLite WAL-backed FIFO queue. The Tauri WebView enqueues actions while the
//! device is offline; on reconnect, [`OfflineQueue::claim_batch`] pulls a chunk
//! and the caller forwards them as `QueuedAction` protos through `sync-client`.
//!
//! Per ARCHITECTURE.md §8.1: cap is 10 000 actions, oldest pending evicted on
//! overflow, retries bounded by [`MAX_ATTEMPTS`].
//!
//! ```text
//! queued_actions(
//!     id PRIMARY KEY,
//!     action_type, payload BLOB, queued_at,
//!     status TEXT,             -- 'pending' | 'in_flight' | 'completed' | 'failed'
//!     attempt_count INTEGER,
//!     last_attempted_at INTEGER NULL,
//!     last_error TEXT NULL
//! )
//! ```

pub mod error;
pub mod queue;
pub mod types;

pub use error::{Error, Result};
pub use queue::OfflineQueue;
pub use types::{ActionStatus, QueuedAction};

/// Hard cap per ARCHITECTURE.md §8.1.
pub const MAX_ACTIONS: usize = 10_000;

/// Failed actions are retried up to this many times before being parked in
/// `failed` status (operator must inspect / replay manually).
pub const MAX_ATTEMPTS: u32 = 5;
