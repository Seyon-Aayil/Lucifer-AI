//! Lucifer MLX runtime.
//!
//! Routes local-inference calls to one of two backends:
//!
//! - [`Backend::Mlx`] — Apple-Silicon-native MLX runtime (`mlx-rs`). Selected
//!   at runtime when the host is `aarch64-apple-darwin` AND the `mlx`
//!   compile-time feature is enabled. Until mlx-rs lands, the constructor
//!   returns the Ollama fallback so callers don't have to special-case.
//! - [`Backend::Ollama`] — wraps [`lucifer_ollama_sidecar::OllamaClient`].
//!   Always available.
//!
//! ```ignore
//! let backend = lucifer_mlx_runtime::select_backend(&endpoint);
//! let stream = backend.generate_stream("llama3.2", "hello").await?;
//! ```

pub mod backend;
pub mod error;
pub mod ollama;
pub mod platform;

#[cfg(feature = "mlx")]
pub mod mlx;

#[cfg(feature = "llama")]
pub mod llama;

pub use backend::{Backend, Inference, InferenceChunk};
pub use error::{Error, Result};
pub use platform::{is_apple_silicon, select_backend};
