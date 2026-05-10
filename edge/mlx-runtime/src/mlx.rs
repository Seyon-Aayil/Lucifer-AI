//! Real MLX inference path.
//!
//! Compiled only when the `mlx` feature is enabled. Building this module
//! requires `cmake` on `$PATH` and Apple Silicon hardware (mlx-rs links
//! against the C++ MLX runtime). On non-Apple-Silicon hosts, the
//! [`platform::select_backend`](crate::platform::select_backend) factory
//! falls back to the Ollama backend.
//!
//! Model layout expected on disk (a directory):
//!
//! ```text
//! <model-dir>/
//!   config.json          # Llama-style HuggingFace config
//!   tokenizer.json       # HuggingFace tokenizer
//!   model.safetensors    # weights with the standard Llama key naming
//! ```
//!
//! ## Status
//!
//! - **Weight + tokenizer loading**: implemented (safetensors → mlx Array,
//!   tokenizer.json → `tokenizers::Tokenizer`).
//! - **Generation loop wiring**: implemented — bridges
//!   [`Inference::generate_stream`] to a blocking sampling loop via
//!   `spawn_blocking` and `tokio::sync::mpsc::unbounded_channel`.
//! - **Transformer forward pass**: structurally laid out (per-block
//!   `forward()`) but the matmul/RoPE/SwiGLU body is left as a TODO so
//!   shape and dtype mismatches can be caught by humans against a real
//!   model file. The Ollama fallback covers production users until this
//!   gap closes.

#![cfg(feature = "mlx")]

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use futures::Stream;
use mlx_rs::{Array, Dtype};
use safetensors::SafeTensors;
use serde::Deserialize;
use tokenizers::Tokenizer;
use tokio_stream::wrappers::UnboundedReceiverStream;

use crate::{
    backend::{Inference, InferenceChunk},
    error::{Error, Result},
};

const SAFETENSORS_FILE: &str = "model.safetensors";
const TOKENIZER_FILE: &str = "tokenizer.json";
const CONFIG_FILE: &str = "config.json";
const DEFAULT_MAX_NEW_TOKENS: usize = 256;

// ── Config ────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Deserialize)]
pub struct LlamaConfig {
    pub vocab_size: i32,
    pub hidden_size: i32,
    pub intermediate_size: i32,
    pub num_hidden_layers: i32,
    pub num_attention_heads: i32,
    #[serde(default)]
    pub num_key_value_heads: Option<i32>,
    pub rms_norm_eps: f32,
    #[serde(default = "default_rope_theta")]
    pub rope_theta: f32,
    #[serde(default = "default_max_seq_len")]
    pub max_position_embeddings: i32,
    #[serde(default = "default_eos_token_id")]
    pub eos_token_id: u32,
}

fn default_rope_theta() -> f32 {
    10_000.0
}
fn default_max_seq_len() -> i32 {
    4096
}
fn default_eos_token_id() -> u32 {
    2
}

impl LlamaConfig {
    pub fn n_kv_heads(&self) -> i32 {
        self.num_key_value_heads.unwrap_or(self.num_attention_heads)
    }

    pub fn head_dim(&self) -> i32 {
        self.hidden_size / self.num_attention_heads
    }
}

// ── Weights ──────────────────────────────────────────────────────────────────

/// Read a safetensors file into `(name → Array)`. Supports F32 / F16 / BF16.
fn load_weights(path: &Path) -> Result<HashMap<String, Array>> {
    let bytes = std::fs::read(path)
        .map_err(|e| Error::MlxUnavailable(format!("read weights {}: {e}", path.display())))?;
    let st = SafeTensors::deserialize(&bytes)
        .map_err(|e| Error::MlxUnavailable(format!("safetensors parse: {e}")))?;
    let mut out = HashMap::new();
    for name in st.names() {
        let view = st
            .tensor(name)
            .map_err(|e| Error::MlxUnavailable(format!("safetensors tensor {name}: {e}")))?;
        let shape: Vec<i32> = view.shape().iter().map(|&d| d as i32).collect();
        let dtype = match view.dtype() {
            safetensors::Dtype::F32 => Dtype::Float32,
            safetensors::Dtype::F16 => Dtype::Float16,
            safetensors::Dtype::BF16 => Dtype::Bfloat16,
            other => {
                return Err(Error::MlxUnavailable(format!(
                    "unsupported safetensors dtype {other:?}"
                )))
            }
        };
        let array = Array::from_slice(view.data(), &shape, dtype);
        out.insert(name.to_string(), array);
    }
    Ok(out)
}

// ── Model ─────────────────────────────────────────────────────────────────────

pub struct MlxModel {
    pub config: LlamaConfig,
    pub tokenizer: Tokenizer,
    /// Raw weights keyed by their HuggingFace tensor names (e.g.
    /// `model.layers.0.self_attn.q_proj.weight`). Held as-is so the actual
    /// transformer body can be filled in incrementally without touching
    /// the loader.
    pub weights: HashMap<String, Array>,
}

impl MlxModel {
    pub fn load(model_dir: &Path) -> Result<Self> {
        let config_bytes = std::fs::read(model_dir.join(CONFIG_FILE))
            .map_err(|e| Error::MlxUnavailable(format!("read config.json: {e}")))?;
        let config: LlamaConfig = serde_json::from_slice(&config_bytes)
            .map_err(|e| Error::MlxUnavailable(format!("parse config.json: {e}")))?;

        let tokenizer = Tokenizer::from_file(model_dir.join(TOKENIZER_FILE))
            .map_err(|e| Error::MlxUnavailable(format!("load tokenizer: {e}")))?;

        let weights = load_weights(&model_dir.join(SAFETENSORS_FILE))?;
        require_keys(&weights, &expected_keys(&config))?;

        tracing::info!(
            layers = config.num_hidden_layers,
            hidden = config.hidden_size,
            heads = config.num_attention_heads,
            kv_heads = config.n_kv_heads(),
            "mlx model loaded"
        );

        Ok(Self {
            config,
            tokenizer,
            weights,
        })
    }

    /// Token ids → forward pass → next-token logits. Implementation pending —
    /// see module-level docstring. Once filled in, the rest of
    /// [`MlxModel::generate`] needs no further changes.
    fn forward_step(&self, _input_ids: &[u32], _kv_cache: &mut KvCache) -> Result<Vec<f32>> {
        // TODO: implement Llama-style forward pass using `self.weights`:
        //   1. embed = take(weights["model.embed_tokens.weight"], input_ids)
        //   2. for each layer i:
        //        a. h = rms_norm(embed, weights[f"...layer{i}.input_layernorm.weight"])
        //        b. q = h @ q_proj.T;  k = h @ k_proj.T;  v = h @ v_proj.T
        //        c. q, k = rope(q, k, theta=config.rope_theta)
        //        d. append k, v to kv_cache; expand kv heads if GQA
        //        e. attn = softmax((q @ k.T) / sqrt(head_dim)) @ v   [+ causal mask]
        //        f. attn_out = attn @ o_proj.T
        //        g. x = embed + attn_out
        //        h. h2 = rms_norm(x, post_attn_norm)
        //        i. mlp = (silu(h2 @ gate_proj.T) * (h2 @ up_proj.T)) @ down_proj.T
        //        j. embed = x + mlp
        //   3. logits = rms_norm(embed, model.norm.weight) @ lm_head.weight.T
        //   4. return logits[-1, :]
        // mlx-rs APIs to use: ops::matmul, ops::softmax_axis, fast::rope,
        // nn::silu, ops::take_axis, ops::concatenate_axis, ops::reshape,
        // ops::transpose_axes, ops::tri_axes (causal mask).
        Err(Error::MlxUnavailable(
            "transformer forward pass not yet implemented; \
             see edge/mlx-runtime/src/mlx.rs forward_step TODO"
                .into(),
        ))
    }

    fn sample_argmax(logits: &[f32]) -> u32 {
        let mut best = 0u32;
        let mut best_val = f32::NEG_INFINITY;
        for (i, &v) in logits.iter().enumerate() {
            if v > best_val {
                best_val = v;
                best = i as u32;
            }
        }
        best
    }

    /// Greedy decode. Calls `on_token` per token (final call has `done=true`).
    /// Returns the full decoded string.
    pub fn generate(
        &self,
        prompt: &str,
        max_new: usize,
        mut on_token: impl FnMut(&str, bool),
    ) -> Result<String> {
        let encoding = self
            .tokenizer
            .encode(prompt, true)
            .map_err(|e| Error::MlxUnavailable(format!("tokenize: {e}")))?;
        let prompt_ids: Vec<u32> = encoding.get_ids().to_vec();

        let mut cache = KvCache::new(self.config.num_hidden_layers as usize);
        let mut acc = String::new();

        // Prefill with the entire prompt.
        let logits = self.forward_step(&prompt_ids, &mut cache)?;
        let mut next_id = Self::sample_argmax(&logits);

        let cap = max_new.min(self.config.max_position_embeddings as usize);
        for _ in 0..cap {
            if next_id == self.config.eos_token_id {
                break;
            }
            let piece = self
                .tokenizer
                .decode(&[next_id], true)
                .map_err(|e| Error::MlxUnavailable(format!("decode: {e}")))?;
            acc.push_str(&piece);
            on_token(&piece, false);

            let logits = self.forward_step(&[next_id], &mut cache)?;
            next_id = Self::sample_argmax(&logits);
        }
        on_token("", true);
        Ok(acc)
    }
}

// ── KV cache ─────────────────────────────────────────────────────────────────

/// Per-layer key/value cache. Each entry grows along the sequence axis as
/// new tokens stream in.
pub struct KvCache {
    pub layers: Vec<Option<(Array, Array)>>,
}

impl KvCache {
    pub fn new(n_layers: usize) -> Self {
        Self {
            layers: (0..n_layers).map(|_| None).collect(),
        }
    }

    pub fn reset(&mut self) {
        for slot in &mut self.layers {
            *slot = None;
        }
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

fn expected_keys(cfg: &LlamaConfig) -> Vec<String> {
    let mut keys = vec![
        "model.embed_tokens.weight".to_string(),
        "model.norm.weight".to_string(),
        "lm_head.weight".to_string(),
    ];
    for i in 0..cfg.num_hidden_layers {
        let p = format!("model.layers.{i}");
        for sfx in [
            "input_layernorm.weight",
            "post_attention_layernorm.weight",
            "self_attn.q_proj.weight",
            "self_attn.k_proj.weight",
            "self_attn.v_proj.weight",
            "self_attn.o_proj.weight",
            "mlp.gate_proj.weight",
            "mlp.up_proj.weight",
            "mlp.down_proj.weight",
        ] {
            keys.push(format!("{p}.{sfx}"));
        }
    }
    keys
}

fn require_keys(weights: &HashMap<String, Array>, keys: &[String]) -> Result<()> {
    let missing: Vec<&String> = keys.iter().filter(|k| !weights.contains_key(*k)).collect();
    if !missing.is_empty() {
        return Err(Error::MlxUnavailable(format!(
            "weights missing {} expected tensor(s); first: {}",
            missing.len(),
            missing[0]
        )));
    }
    Ok(())
}

// ── Inference impl ────────────────────────────────────────────────────────────

/// MLX-backed [`Inference`] implementation.
///
/// Constructed via [`MlxInference::new`] pointing at a model directory. The
/// model is loaded lazily on the first request and reused for the lifetime of
/// the handle, guarded by a [`Mutex`].
pub struct MlxInference {
    model_dir: PathBuf,
    model: Arc<Mutex<Option<MlxModel>>>,
}

impl MlxInference {
    pub fn new(model_dir: impl Into<PathBuf>) -> Self {
        Self {
            model_dir: model_dir.into(),
            model: Arc::new(Mutex::new(None)),
        }
    }

    fn ensure_loaded(&self) -> Result<()> {
        let mut guard = self
            .model
            .lock()
            .map_err(|e| Error::MlxUnavailable(format!("model lock poisoned: {e}")))?;
        if guard.is_none() {
            *guard = Some(MlxModel::load(&self.model_dir)?);
        }
        Ok(())
    }

    pub fn model_dir(&self) -> &Path {
        &self.model_dir
    }
}

#[async_trait]
impl Inference for MlxInference {
    fn name(&self) -> &'static str {
        "mlx"
    }

    async fn generate_stream(
        &self,
        _model: &str,
        prompt: &str,
    ) -> Result<std::pin::Pin<Box<dyn Stream<Item = Result<InferenceChunk>> + Send>>> {
        self.ensure_loaded()?;
        let prompt = prompt.to_string();
        let label = self
            .model_dir
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("mlx")
            .to_string();
        let model = Arc::clone(&self.model);

        let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<Result<InferenceChunk>>();
        tokio::task::spawn_blocking(move || {
            let guard = match model.lock() {
                Ok(g) => g,
                Err(e) => {
                    let _ = tx.send(Err(Error::MlxUnavailable(format!("lock poisoned: {e}"))));
                    return;
                }
            };
            let model_ref = match guard.as_ref() {
                Some(m) => m,
                None => {
                    let _ = tx.send(Err(Error::MlxUnavailable("model not loaded".into())));
                    return;
                }
            };
            let out = model_ref.generate(&prompt, DEFAULT_MAX_NEW_TOKENS, |piece, done| {
                let _ = tx.send(Ok(InferenceChunk {
                    model: label.clone(),
                    text: piece.to_string(),
                    done,
                    eval_count: None,
                }));
            });
            if let Err(e) = out {
                let _ = tx.send(Err(e));
            }
        });

        Ok(Box::pin(UnboundedReceiverStream::new(rx)))
    }
}
