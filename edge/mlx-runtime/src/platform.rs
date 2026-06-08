use std::sync::Arc;

use crate::{
    backend::{Backend, Inference},
    ollama::OllamaInference,
};

/// True when the current binary was compiled for `aarch64-apple-darwin`.
/// Runtime check is unnecessary because the cfg is fixed at compile time.
pub const fn is_apple_silicon() -> bool {
    cfg!(all(target_arch = "aarch64", target_os = "macos"))
}

/// Pick the best available local-inference backend.
///
/// - On Apple Silicon with the `mlx` feature compiled in AND the
///   `LUCIFER_MLX_MODEL_DIR` env var pointing at a Llama-style model
///   directory → [`Backend::Mlx`].
/// - Otherwise → [`Backend::Ollama`].
///
/// Always returns a working `Inference` impl; never panics.
pub fn select_backend(ollama_endpoint: &str) -> (Backend, Arc<dyn Inference>) {
    // Opt-in llama.cpp backend (Phase 4c spike): explicit GGUF model path.
    #[cfg(feature = "llama")]
    {
        if let Ok(model) = std::env::var("LUCIFER_LLAMA_MODEL") {
            if !model.is_empty() {
                tracing::info!(backend = "llama", model = %model, "llama.cpp backend selected");
                let inference = crate::llama::LlamaCppInference::new(model);
                return (Backend::Llama, Arc::new(inference));
            }
        }
    }

    #[cfg(feature = "mlx")]
    {
        if is_apple_silicon() {
            if let Ok(dir) = std::env::var("LUCIFER_MLX_MODEL_DIR") {
                if !dir.is_empty() {
                    tracing::info!(
                        backend = "mlx",
                        model_dir = %dir,
                        "MLX backend selected for Apple Silicon host"
                    );
                    let inference = crate::mlx::MlxInference::new(std::path::PathBuf::from(dir));
                    return (Backend::Mlx, Arc::new(inference));
                }
            }
            tracing::info!(
                "Apple Silicon detected but LUCIFER_MLX_MODEL_DIR is unset — \
                 falling back to Ollama backend"
            );
        }
    }

    tracing::info!(
        backend = "ollama",
        apple_silicon = is_apple_silicon(),
        "Ollama backend selected"
    );
    let inference = OllamaInference::from_endpoint(ollama_endpoint.to_string());
    (Backend::Ollama, Arc::new(inference))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_apple_silicon_consistently() {
        // The function is const + compile-time; this just locks in that it
        // returns the same value the cfg-gates use.
        let detected = is_apple_silicon();
        let direct = cfg!(all(target_arch = "aarch64", target_os = "macos"));
        assert_eq!(detected, direct);
    }

    #[test]
    fn select_backend_returns_working_impl() {
        let (tag, inference) = select_backend("http://127.0.0.1:11434");
        assert!(matches!(tag, Backend::Mlx | Backend::Ollama));
        assert!(matches!(inference.name(), "mlx" | "ollama"));
    }

    #[cfg(not(feature = "mlx"))]
    #[test]
    fn ollama_is_default_without_mlx_feature() {
        let (tag, inference) = select_backend("http://127.0.0.1:11434");
        assert_eq!(tag, Backend::Ollama);
        assert_eq!(inference.name(), "ollama");
    }

    #[test]
    fn backend_label_round_trip() {
        assert_eq!(Backend::Mlx.label(), "mlx");
        assert_eq!(Backend::Ollama.label(), "ollama");
        assert_eq!(Backend::Llama.label(), "llama");
    }
}
