//! `lucifer-edge` — integration-test CLI for the Lucifer edge crates.
//!
//! Exercises sync-client, edge-store, offline-queue, and ollama-sidecar without
//! requiring a Tauri runtime. Useful for smoke-testing the master server,
//! seeding a local SQLite mirror, or driving the offline replay loop.

use std::{path::PathBuf, time::Duration};

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use futures::StreamExt;
use lucifer_edge_store::EdgeStore;
use lucifer_offline_queue::{ActionStatus, OfflineQueue};
use lucifer_ollama_sidecar::OllamaClient;
use lucifer_sync_client::{ClientConfig, SyncClient};

#[derive(Parser, Debug)]
#[command(name = "lucifer-edge", version, about)]
struct Cli {
    /// Verbose tracing.
    #[arg(short, long, global = true)]
    verbose: bool,

    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand, Debug)]
enum Cmd {
    /// Ping the master gRPC server with a hot-subgraph request (`last_sync_at=0`).
    Ping {
        #[arg(long)]
        endpoint: String,
        #[arg(long)]
        device_id: String,
        #[arg(long)]
        client_cert: PathBuf,
        #[arg(long)]
        client_key: PathBuf,
        #[arg(long)]
        ca_cert: PathBuf,
        #[arg(long)]
        jwt: String,
    },

    /// Print stats from a local edge-store SQLite file.
    StoreStats {
        #[arg(long)]
        db: PathBuf,
    },

    /// Enqueue a synthetic action into the offline queue.
    Enqueue {
        #[arg(long)]
        db: PathBuf,
        #[arg(long, default_value = "agent.execute")]
        action_type: String,
        #[arg(long, default_value = "{}")]
        payload: String,
    },

    /// Claim and print up to N pending actions (does not mark them completed).
    Claim {
        #[arg(long)]
        db: PathBuf,
        #[arg(long, default_value_t = 5)]
        limit: usize,
    },

    /// Stream a one-shot prompt against a local Ollama daemon.
    OllamaGenerate {
        #[arg(long, default_value = "http://127.0.0.1:11434")]
        endpoint: String,
        #[arg(long, default_value = "llama3.2")]
        model: String,
        prompt: String,
    },

    /// List models known to the local Ollama daemon.
    OllamaList {
        #[arg(long, default_value = "http://127.0.0.1:11434")]
        endpoint: String,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    init_tracing(cli.verbose);

    match cli.cmd {
        Cmd::Ping {
            endpoint,
            device_id,
            client_cert,
            client_key,
            ca_cert,
            jwt,
        } => cmd_ping(endpoint, device_id, client_cert, client_key, ca_cert, jwt).await,
        Cmd::StoreStats { db } => cmd_store_stats(db),
        Cmd::Enqueue {
            db,
            action_type,
            payload,
        } => cmd_enqueue(db, action_type, payload),
        Cmd::Claim { db, limit } => cmd_claim(db, limit),
        Cmd::OllamaGenerate {
            endpoint,
            model,
            prompt,
        } => cmd_ollama_generate(endpoint, model, prompt).await,
        Cmd::OllamaList { endpoint } => cmd_ollama_list(endpoint).await,
    }
}

async fn cmd_ping(
    endpoint: String,
    device_id: String,
    client_cert: PathBuf,
    client_key: PathBuf,
    ca_cert: PathBuf,
    jwt: String,
) -> Result<()> {
    let cfg = ClientConfig {
        master_endpoint: endpoint,
        device_id: device_id.clone(),
        client_cert,
        client_key,
        ca_cert,
        jwt,
        sni_override: None,
    };
    let mut client = SyncClient::connect(&cfg)
        .await
        .context("opening gRPC channel")?;

    let req = lucifer_sync_client::proto::SubgraphRequest {
        device_id,
        last_sync_at: 0,
        max_size_bytes: 0,
    };
    let resp = tokio::time::timeout(Duration::from_secs(30), client.get_hot_subgraph(req))
        .await
        .context("master did not respond within 30s")??;

    println!(
        "{}",
        serde_json::json!({
            "node_count": resp.nodes.len(),
            "edge_count": resp.edges.len(),
            "manifest_hash": resp.manifest_hash,
            "is_full_sync": resp.is_full_sync,
        })
    );
    Ok(())
}

fn cmd_store_stats(db: PathBuf) -> Result<()> {
    let store = EdgeStore::open(&db).context("opening edge-store")?;
    println!(
        "{}",
        serde_json::json!({
            "path": db.display().to_string(),
            "node_count": store.count_nodes()?,
            "edge_count": store.count_edges()?,
            "last_sync_at_ms": store.last_sync_at()?,
        })
    );
    Ok(())
}

fn cmd_enqueue(db: PathBuf, action_type: String, payload: String) -> Result<()> {
    let mut queue = OfflineQueue::open(&db).context("opening offline-queue")?;
    let id = queue.enqueue(&action_type, payload.as_bytes(), chrono_now_ms())?;
    println!("{}", serde_json::json!({"id": id, "status": "pending"}));
    Ok(())
}

fn cmd_claim(db: PathBuf, limit: usize) -> Result<()> {
    let mut queue = OfflineQueue::open(&db).context("opening offline-queue")?;
    let claimed = queue.claim_batch(limit, chrono_now_ms())?;
    let summary: Vec<_> = claimed
        .iter()
        .map(|a| {
            serde_json::json!({
                "id": a.id,
                "action_type": a.action_type,
                "queued_at": a.queued_at,
                "attempt_count": a.attempt_count,
                "status": match a.status {
                    ActionStatus::Pending   => "pending",
                    ActionStatus::InFlight  => "in_flight",
                    ActionStatus::Completed => "completed",
                    ActionStatus::Failed    => "failed",
                },
            })
        })
        .collect();
    println!("{}", serde_json::to_string_pretty(&summary)?);
    Ok(())
}

async fn cmd_ollama_generate(endpoint: String, model: String, prompt: String) -> Result<()> {
    let client = OllamaClient::new(endpoint);
    let stream = client.generate_stream(&model, &prompt).await?;
    let mut stream = Box::pin(stream);
    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;
        print!("{}", chunk.response);
        use std::io::Write;
        std::io::stdout().flush().ok();
        if chunk.done {
            println!();
            break;
        }
    }
    Ok(())
}

async fn cmd_ollama_list(endpoint: String) -> Result<()> {
    let client = OllamaClient::new(endpoint);
    let models = client.list_models().await?;
    println!(
        "{}",
        serde_json::to_string_pretty(&serde_json::json!(models
            .iter()
            .map(|m| serde_json::json!({
                "name": m.name,
                "size": m.size,
                "modified_at": m.modified_at,
            }))
            .collect::<Vec<_>>()))?
    );
    Ok(())
}

// ── Helpers ──────────────────────────────────────────────────────────────────

fn init_tracing(verbose: bool) {
    let level = if verbose { "debug" } else { "warn" };
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new(level)),
        )
        .with_target(false)
        .with_writer(std::io::stderr)
        .init();
}

fn chrono_now_ms() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}
