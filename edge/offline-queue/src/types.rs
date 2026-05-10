use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActionStatus {
    Pending,
    InFlight,
    Completed,
    Failed,
}

impl ActionStatus {
    pub(crate) fn as_str(&self) -> &'static str {
        match self {
            ActionStatus::Pending => "pending",
            ActionStatus::InFlight => "in_flight",
            ActionStatus::Completed => "completed",
            ActionStatus::Failed => "failed",
        }
    }

    pub(crate) fn parse(s: &str) -> Option<Self> {
        Some(match s {
            "pending" => ActionStatus::Pending,
            "in_flight" => ActionStatus::InFlight,
            "completed" => ActionStatus::Completed,
            "failed" => ActionStatus::Failed,
            _ => return None,
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueuedAction {
    pub id: String,
    pub action_type: String,
    /// Opaque payload — caller decides whether to encrypt it before enqueue.
    pub payload: Vec<u8>,
    pub queued_at: i64,
    pub status: ActionStatus,
    pub attempt_count: u32,
    pub last_attempted_at: Option<i64>,
    pub last_error: Option<String>,
}
