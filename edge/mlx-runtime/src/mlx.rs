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
//! Gated on the `mlx` feature by `lib.rs` (`#[cfg(feature = "mlx")] pub mod mlx`).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use futures::Stream;
use mlx_rs::{fast, ops, Array, Dtype};
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
        // safetensors gives raw little-endian bytes + a runtime dtype; build the
        // Array directly from the byte buffer rather than via the dtype-inferred
        // `from_slice` (which would mis-tag the bytes as Uint8).
        let array = unsafe {
            Array::from_raw_data(
                view.data().as_ptr() as *const std::ffi::c_void,
                &shape,
                dtype,
            )
        };
        out.insert(name.to_string(), array);
    }
    Ok(out)
}

// ── KV cache ─────────────────────────────────────────────────────────────────

/// Per-layer key/value cache. Each entry grows along the time axis.
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

    /// Number of cached k/v positions in any layer (they're kept in sync).
    pub fn position(&self) -> i32 {
        self.layers
            .first()
            .and_then(|slot| slot.as_ref())
            .map(|(k, _)| k.shape()[2])
            .unwrap_or(0)
    }
}

// ── Math helpers ─────────────────────────────────────────────────────────────

fn mlx_err(e: mlx_rs::error::Exception) -> Error {
    Error::MlxUnavailable(format!("mlx op: {e}"))
}

/// Llama-style RMSNorm: `x * rsqrt(mean(x², -1) + eps) * weight`.
fn rms_norm(x: &Array, weight: &Array, eps: f32) -> Result<Array> {
    let sq = ops::multiply(x, x).map_err(mlx_err)?;
    let mean = ops::mean_axis(&sq, -1, true).map_err(mlx_err)?;
    let denom = ops::add(&mean, Array::from_f32(eps)).map_err(mlx_err)?;
    let rstd = ops::rsqrt(&denom).map_err(mlx_err)?;
    let normed = ops::multiply(x, &rstd).map_err(mlx_err)?;
    ops::multiply(&normed, weight).map_err(mlx_err)
}

/// HuggingFace Linear: y = x @ w.T  (w stored as [out, in]).
fn linear(x: &Array, weight: &Array) -> Result<Array> {
    ops::matmul(x, weight.t()).map_err(mlx_err)
}

/// Causal mask over (T_q, T_k) where queries can attend keys at positions
/// `0..=offset+i` for query position `i`. For prefill T_q == T_k; for
/// per-token decoding T_q == 1 and the mask is a no-op.
fn build_causal_mask(t_q: i32, t_k: i32, dtype: Dtype) -> Result<Array> {
    if t_q <= 1 {
        // 1×T_k row is fully visible — no masking required.
        return Array::from_f32(0.0).as_dtype(dtype).map_err(mlx_err);
    }
    // tri(M, N, k) yields a (M, N) matrix with 1s where j <= i + k, else 0.
    let tri = ops::tri::<f32>(t_q, Some(t_k), Some(t_k - t_q)).map_err(mlx_err)?;
    // Convert {0, 1} → {-inf, 0}: where(tri == 0, -inf, 0)
    let zeros = ops::zeros_like(&tri).map_err(mlx_err)?;
    let neg_inf =
        ops::full::<f32>(tri.shape(), &Array::from_f32(f32::NEG_INFINITY)).map_err(mlx_err)?;
    let mask = ops::r#where(&tri, &zeros, &neg_inf).map_err(mlx_err)?;
    mask.as_dtype(dtype).map_err(mlx_err)
}

/// Greatest common divisor — helper for GQA expansion shape math.
fn repeat_kv(arr: &Array, n_repeat: i32) -> Result<Array> {
    if n_repeat == 1 {
        return Ok(arr.clone());
    }
    // arr: (B, n_kv_heads, T, head_dim)
    // Expand head axis from n_kv_heads → n_kv_heads * n_repeat
    let s = arr.shape();
    let (b, n_kv, t, d) = (s[0], s[1], s[2], s[3]);
    let arr5 = ops::reshape(arr, &[b, n_kv, 1, t, d]).map_err(mlx_err)?;
    let bcast = ops::broadcast_to(&arr5, &[b, n_kv, n_repeat, t, d]).map_err(mlx_err)?;
    ops::reshape(&bcast, &[b, n_kv * n_repeat, t, d]).map_err(mlx_err)
}

// ── Block forward ────────────────────────────────────────────────────────────

#[allow(clippy::too_many_arguments)]
fn block_forward(
    x: &Array,
    weights: &HashMap<String, Array>,
    layer: usize,
    config: &LlamaConfig,
    cache: &mut Option<(Array, Array)>,
    offset: i32,
) -> Result<Array> {
    let p = format!("model.layers.{layer}");
    let in_norm_w = get(weights, &format!("{p}.input_layernorm.weight"))?;
    let post_norm_w = get(weights, &format!("{p}.post_attention_layernorm.weight"))?;
    let q_w = get(weights, &format!("{p}.self_attn.q_proj.weight"))?;
    let k_w = get(weights, &format!("{p}.self_attn.k_proj.weight"))?;
    let v_w = get(weights, &format!("{p}.self_attn.v_proj.weight"))?;
    let o_w = get(weights, &format!("{p}.self_attn.o_proj.weight"))?;
    let gate_w = get(weights, &format!("{p}.mlp.gate_proj.weight"))?;
    let up_w = get(weights, &format!("{p}.mlp.up_proj.weight"))?;
    let down_w = get(weights, &format!("{p}.mlp.down_proj.weight"))?;

    let n_heads = config.num_attention_heads;
    let n_kv = config.n_kv_heads();
    let head_dim = config.head_dim();
    let n_repeat = n_heads / n_kv;

    // ── Self-attention ────────────────────────────────────────────────
    let h = rms_norm(x, in_norm_w, config.rms_norm_eps)?;

    // Project to Q/K/V: (B, T, n_heads*head_dim) etc.
    let q = linear(&h, q_w)?;
    let k = linear(&h, k_w)?;
    let v = linear(&h, v_w)?;

    let (b, t) = (q.shape()[0], q.shape()[1]);

    // Reshape to (B, T, n_heads, head_dim) → transpose to (B, n_heads, T, head_dim)
    let q = ops::reshape(&q, &[b, t, n_heads, head_dim]).map_err(mlx_err)?;
    let q = ops::transpose_axes(&q, &[0, 2, 1, 3]).map_err(mlx_err)?;
    let k = ops::reshape(&k, &[b, t, n_kv, head_dim]).map_err(mlx_err)?;
    let k = ops::transpose_axes(&k, &[0, 2, 1, 3]).map_err(mlx_err)?;
    let v = ops::reshape(&v, &[b, t, n_kv, head_dim]).map_err(mlx_err)?;
    let v = ops::transpose_axes(&v, &[0, 2, 1, 3]).map_err(mlx_err)?;

    // Apply RoPE at the cache offset.
    let q = fast::rope(
        &q,
        head_dim,
        false,
        Some(config.rope_theta),
        1.0,
        offset,
        None::<&Array>,
    )
    .map_err(mlx_err)?;
    let k = fast::rope(
        &k,
        head_dim,
        false,
        Some(config.rope_theta),
        1.0,
        offset,
        None::<&Array>,
    )
    .map_err(mlx_err)?;

    // Concatenate with KV cache.
    let (k, v) = match cache.take() {
        Some((prev_k, prev_v)) => {
            let new_k = ops::concatenate_axis(&[&prev_k, &k], 2).map_err(mlx_err)?;
            let new_v = ops::concatenate_axis(&[&prev_v, &v], 2).map_err(mlx_err)?;
            (new_k, new_v)
        }
        None => (k, v),
    };
    *cache = Some((k.clone(), v.clone()));

    // GQA expansion to match n_heads.
    let k = repeat_kv(&k, n_repeat)?;
    let v = repeat_kv(&v, n_repeat)?;

    // Scaled dot-product attention.
    let scale = (head_dim as f32).sqrt().recip();
    let kt = ops::transpose_axes(&k, &[0, 1, 3, 2]).map_err(mlx_err)?;
    let scores = ops::matmul(&q, &kt).map_err(mlx_err)?;
    let scores = ops::multiply(&scores, Array::from_f32(scale)).map_err(mlx_err)?;

    // Causal mask (additive). Uses the score dtype so addition broadcasts.
    let t_k = scores.shape()[3];
    let mask = build_causal_mask(t, t_k, scores.dtype())?;
    let scores = ops::add(&scores, &mask).map_err(mlx_err)?;

    let attn = ops::softmax_axis(&scores, -1, false).map_err(mlx_err)?;
    let attn = ops::matmul(&attn, &v).map_err(mlx_err)?;
    let attn = ops::transpose_axes(&attn, &[0, 2, 1, 3]).map_err(mlx_err)?;
    let attn = ops::reshape(&attn, &[b, t, n_heads * head_dim]).map_err(mlx_err)?;
    let attn_out = linear(&attn, o_w)?;

    let x = ops::add(x, &attn_out).map_err(mlx_err)?;

    // ── SwiGLU MLP ────────────────────────────────────────────────────
    let h2 = rms_norm(&x, post_norm_w, config.rms_norm_eps)?;
    let gate = linear(&h2, gate_w)?;
    // SiLU = x * sigmoid(x)
    let gate_sig = ops::sigmoid(&gate).map_err(mlx_err)?;
    let gate_silu = ops::multiply(&gate, &gate_sig).map_err(mlx_err)?;
    let up = linear(&h2, up_w)?;
    let mlp = ops::multiply(&gate_silu, &up).map_err(mlx_err)?;
    let mlp_out = linear(&mlp, down_w)?;

    ops::add(&x, &mlp_out).map_err(mlx_err)
}

fn get<'a>(map: &'a HashMap<String, Array>, key: &str) -> Result<&'a Array> {
    map.get(key)
        .ok_or_else(|| Error::MlxUnavailable(format!("missing weight {key}")))
}

// ── Model ─────────────────────────────────────────────────────────────────────

pub struct MlxModel {
    pub config: LlamaConfig,
    pub tokenizer: Tokenizer,
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

    /// One forward pass at `offset` cached tokens. `input_ids` has shape `[T]`;
    /// returns the logits for the **last** position as `Vec<f32>` of length
    /// `vocab_size`.
    fn forward_step(
        &self,
        input_ids: &[u32],
        cache: &mut KvCache,
        offset: i32,
    ) -> Result<Vec<f32>> {
        // Build (1, T) i32 input.
        let ids_i32: Vec<i32> = input_ids.iter().map(|&v| v as i32).collect();
        let tokens = Array::from_slice(&ids_i32, &[1, ids_i32.len() as i32]);

        // Embed: take rows from embed_tokens.weight indexed by tokens.
        let embed_w = get(&self.weights, "model.embed_tokens.weight")?;
        let mut x = ops::indexing::take_axis(embed_w, &tokens, 0).map_err(mlx_err)?;
        // take_axis along axis 0 with index shape (1, T) yields (1, T, hidden).
        // Confirm shape: should already be (1, T, hidden_size); no reshape needed.

        // Cast embed_w-derived x to match weight dtype if necessary. For F32
        // weights everything stays F32. For F16/BF16, mlx does the cast in
        // each op as needed; we keep x in the model's native dtype.

        for layer in 0..self.config.num_hidden_layers as usize {
            x = block_forward(
                &x,
                &self.weights,
                layer,
                &self.config,
                &mut cache.layers[layer],
                offset,
            )?;
        }

        let final_norm_w = get(&self.weights, "model.norm.weight")?;
        let x = rms_norm(&x, final_norm_w, self.config.rms_norm_eps)?;

        let lm_head_w = get(&self.weights, "lm_head.weight")?;
        let logits = linear(&x, lm_head_w)?;
        // logits: (1, T, vocab_size) — take last position.
        let last_idx = Array::from_int(logits.shape()[1] - 1);
        let last = ops::indexing::take_axis(&logits, &last_idx, 1).map_err(mlx_err)?;
        // Result is (1, vocab_size); cast to f32 + flatten.
        let last_f32 = last.as_dtype(Dtype::Float32).map_err(mlx_err)?;
        let flat = ops::reshape(&last_f32, &[self.config.vocab_size]).map_err(mlx_err)?;
        Ok(flat.as_slice::<f32>().to_vec())
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

        // Prefill
        let logits = self.forward_step(&prompt_ids, &mut cache, 0)?;
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

            let offset = cache.position();
            let logits = self.forward_step(&[next_id], &mut cache, offset)?;
            next_id = Self::sample_argmax(&logits);
        }
        on_token("", true);
        Ok(acc)
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
