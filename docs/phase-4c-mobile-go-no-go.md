# Phase 4c — Mobile Go/No-Go

**Status:** Decision draft · **Date:** 2026-06-08 · **Owner:** Ramsanjiev

## 1. Context

Phases 0–4b are shipped: a master FastAPI/gRPC backend and a Tauri 2.0 **desktop**
edge client (Rust workspace + React frontend) that pairs over mTLS gRPC, syncs a
hot-subgraph, runs local inference, and executes offline actions. Phase 4c must
decide **how to ship a mobile (iOS + Android) edge client** — and whether to do
it at all before Phase 5.

The question is not "can we render a UI on mobile" (any option can). It is: **what
maximises reuse of the existing edge stack while clearing the one hard
constraint — on-device inference — on both platforms?**

## 2. What the current edge stack gives us (reuse audit)

| Crate | Mobile-portable? | Notes |
|-------|------------------|-------|
| `sync-client` (tonic + prost, mTLS) | ✅ Yes | tonic/rustls compile for `aarch64-apple-ios` + Android NDK targets. The whole sync protocol ports. |
| `edge-store` (rusqlite + sqlite-vec) | ✅ Yes | SQLite is first-class on both platforms; sqlite-vec is C, cross-compiles. |
| `offline-queue` (SQLite WAL) | ✅ Yes | Portable. |
| `desktop-bindings` (IPC command logic) | ⚠️ Mostly | The async command bodies are portable; the `#[tauri::command]` glue + keychain are platform-specific. |
| `ollama-sidecar` (`spawn ollama serve`) | ❌ **No** | iOS forbids spawning child processes; Ollama has no iOS/Android build. **Dead on mobile.** |
| `mlx-runtime` (mlx-rs 0.25, Apple-Silicon Metal) | ⚠️ iOS-maybe / ❌ Android | iOS has Metal + Apple Silicon, but mlx-rs's iOS target support is unproven (Apple's blessed path is `mlx-swift`). Android has no Metal. |
| `desktop` (tray, `tauri-plugin-global-shortcut`) | ❌ No | Tray + global hotkey are desktop-only paradigms; mobile uses share-sheet / widget / notification. |
| keychain helper | ⚠️ Rewrite | Needs iOS Keychain + Android Keystore backends. |

**Takeaway:** the sync + storage + offline layers (~the protocol-heavy half) port
essentially for free. The desktop-shell + **inference** layers do not.

## 3. The pivotal constraint — on-device inference

Ollama (the desktop inference backend) **cannot run on mobile**. Every option below
must answer "what runs the model on-device", and that answer is mostly independent
of the UI framework:

| Engine | iOS | Android | Notes |
|--------|-----|---------|-------|
| **llama.cpp** | ✅ | ✅ | Cross-platform C/C++, Metal (iOS) + Vulkan/NNAPI (Android). The realistic common path. Rust binding: `llama-cpp-2`. |
| **MLX** | ✅ (via `mlx-swift`) | ❌ | Apple-only. Best iOS perf, but Swift-first; `mlx-rs` on iOS is unproven. |
| **MediaPipe LLM Inference** | ⚠️ | ⚠️ | **Maintenance-only** (Google, ~June 2026) — feature work has moved to **LiteRT-LM** (Kotlin/Swift/JS). Not a forward path; Google-model-centric. |
| **CoreML / ExecuTorch** | ✅ | ✅ (ExecuTorch) | ExecuTorch reached **1.0 GA (Oct 2025)** — cross-platform, production-ready; the sanctioned fallback if gate 3 (llama.cpp on-device) fails. CoreML remains iOS-only. |

**Recommendation for inference:** standardise on **llama.cpp** for mobile (one engine,
both platforms, GGUF models shared with desktop). Optionally use MLX on iOS later as
a perf upgrade behind the same `Inference` trait the edge already defines. This
decision is **orthogonal to the UI framework** and de-risks all three options.

## 4. Options

### A. Tauri 2.0 mobile (iOS + Android)
- **Reuse:** maximal — the entire Rust workspace (sync-client, edge-store,
  offline-queue) + the React frontend + the `desktop-bindings` command pattern carry
  over. One codebase, desktop + mobile.
- **Maturity:** Tauri 2.0 mobile GA'd Oct 2024; iOS + Android supported. Plugin
  ecosystem is thinner than desktop; some plugins are desktop-only (global-shortcut,
  tray — irrelevant on mobile). Mobile-specific plugins (biometric, NFC, push,
  share) exist but are younger.
- **Inference:** link llama.cpp via a Rust crate (`llama-cpp-2`) in a new
  `edge-inference` crate behind the existing `Inference` trait — no JS bridge needed.
- **Risk:** WebView-based UI (perf/feel vs native); mobile plugin gaps may need
  custom Swift/Kotlin plugins; iOS background execution limits for sync.

### B. React Native + native modules
- **Reuse:** React *concepts* reuse, but **not the components** (RN ≠ react-dom) and
  **not the Rust crates** without a uniffi/FFI bridge. The sync/storage stack would be
  re-exposed through a Rust-mobile FFI layer (real work) or rewritten in TS/native.
- **Maturity:** very mature, huge ecosystem, native feel.
- **Inference:** native module wrapping llama.cpp (community modules exist).
- **Risk:** **largest divergence from the existing codebase** — two sync clients to
  keep in lockstep, or a non-trivial Rust-FFI investment.

### C. Native SwiftUI + Jetpack Compose
- **Reuse:** none of the UI; the Rust core could be shared via uniffi.
- **Maturity / feel:** best native UX; most platform control.
- **Risk / cost:** **two full native apps** + a shared-core FFI. Highest effort, best
  ceiling. Justified only if mobile UX is a primary product differentiator.

## 5. Comparison

| Criterion | A. Tauri mobile | B. RN + native | C. Native |
|-----------|-----------------|----------------|-----------|
| Reuse of Rust sync/storage stack | ★★★★★ | ★★ (needs FFI) | ★★ (needs FFI) |
| Reuse of React UI | ★★★★ | ★★ (rewrite to RN) | ☆ |
| One codebase desktop+mobile | ★★★★★ | ★ | ☆ |
| Native feel / performance | ★★★ | ★★★★ | ★★★★★ |
| Mobile plugin ecosystem | ★★★ | ★★★★★ | ★★★★★ |
| Effort to first working build | Low–Med | Med–High | High |
| Inference path (llama.cpp) | Clean (Rust crate) | Native module | Native module |

## 6. Recommendation

**GO with Option A (Tauri 2.0 mobile), conditional on a de-risking spike.** It is the
only option that reuses the protocol-heavy half of the edge stack and the React UI as
one codebase, and the inference constraint is solved the same way (llama.cpp) under
any option — so A carries no extra inference risk.

Standardise mobile inference on **llama.cpp** behind the existing `Inference` trait;
treat MLX-on-iOS as a later perf option, not a dependency.

### Go/No-Go gates (must all pass in the spike before full commit)
1. **Build**: `cargo tauri ios build` + `android build` produce a launchable app from
   the existing workspace (sync-client + edge-store linked).
2. **Sync**: the iOS app opens an mTLS gRPC `SyncStream` to master and pulls a
   hot-subgraph (proves tonic/rustls + client-cert work on-device).
3. **Inference**: a small GGUF model generates a token stream on-device via
   `llama-cpp-2` on both iOS and Android.
4. **Secure storage**: pairing JWT + client cert persist in iOS Keychain / Android
   Keystore.

If gate 2 or 3 fails on either platform, fall back to **Option B** for that platform
only (RN shell + the same Rust core via uniffi), keeping the master side unchanged.

## 7. Spike plan (≈1–2 weeks, before any production commit)

1. **Inference seam — scaffolded.** The `llama` backend lives in
   `edge/mlx-runtime/src/llama.rs` (feature `llama`, off by default): `Backend::Llama`
   tag, `LlamaCppInference` implementing the `Inference` trait, and opt-in selection
   via `LUCIFER_LLAMA_MODEL`. The token loop returns `LlamaUnavailable` until wired.
   **Remaining:** add `dep:llama-cpp-2`, implement `generate_stream` (load GGUF →
   context → tokenize → sample loop), smoke-test on macOS, then `aarch64-apple-ios`
   + Android NDK targets.
2. `cargo tauri ios init` / `android init` on a throwaway branch; link sync-client +
   edge-store; get a blank app to launch.
3. Wire one screen: pair → `GetHotSubgraph` → render node count (gate 2).
4. Wire local generate via `edge-inference` (gate 3).
5. Keychain/Keystore persistence (gate 4).
6. Record outcomes here; flip status to **GO** or **PARTIAL (per-platform)**.

## 8. Out of scope / explicitly deferred
- Push notifications, share-extension capture, home-screen widget — post-GO.
- MLX-on-iOS perf path — optional follow-up behind the `Inference` trait.
- Windows desktop — separate track.
