# Phase 4c — Mobile Spike Runbook

Companion to [phase-4c-mobile-go-no-go.md](phase-4c-mobile-go-no-go.md). Run these
steps **on a Mac with Xcode + Android Studio installed** (the spike is
device-gated — none of it runs headless/CI). Record outcomes against the four
Go/No-Go gates at the bottom.

## 0. Prerequisites (one-time)

```bash
# macOS toolchains
xcode-select --install                 # or full Xcode from the App Store
xcodebuild -downloadComponent MetalToolchain   # for llama.cpp Metal build
# Android: install Android Studio, then SDK Platform + NDK + cmake via SDK Manager.
# Export (add to your shell profile):
export ANDROID_HOME="$HOME/Library/Android/sdk"
export NDK_HOME="$ANDROID_HOME/ndk/<version>"

# Rust mobile targets + Tauri CLI (automatable — see `make spike-mobile-prep`)
rustup target add aarch64-apple-ios aarch64-apple-ios-sim \
  aarch64-linux-android armv7-linux-androideabi \
  i686-linux-android x86_64-linux-android
cargo install tauri-cli --version '^2' --locked
```

`make spike-mobile-check` prints what's present/missing.
`make spike-mobile-prep` adds the rustup targets + installs the Tauri CLI.

## 1. Gate 1 — build a launchable mobile app

**Blocker first (one-time restructure):** `edge/desktop` is currently a `[[bin]]`.
Tauri mobile drives the app through a **library** entry, so the crate must expose:

- `Cargo.toml`: add a `[lib]` with `crate-type = ["staticlib", "cdylib", "rlib"]`
  and a `name = "lucifer_desktop_lib"`.
- `src/lib.rs`: move the builder into `pub fn run()` annotated with
  `#[cfg_attr(mobile, tauri::mobile_entry_point)]`; `src/main.rs` becomes a thin
  `fn main() { lucifer_desktop_lib::run() }`.

Then:

```bash
cd edge/desktop
cargo tauri ios init        # generates gen/apple Xcode project + mobile config
cargo tauri android init    # generates gen/android Gradle project

cargo tauri ios build       # or: cargo tauri ios dev   (simulator)
cargo tauri android build   # or: cargo tauri android dev
```

✅ **Gate 1 pass** = the app launches on an iOS simulator and an Android emulator.

## 2. Gate 2 — mTLS gRPC sync on-device

The whole `sync-client` (tonic + rustls) compiles for mobile targets unchanged.
Verify on-device:

1. Pair the device against the master (`/devices/pair`) and persist the bundle.
2. Open a `SyncStream` / `GetHotSubgraph` and render the node count on one screen
   (reuse the desktop Onboarding + ConnectionBar flow).

Watch-outs: iOS App Transport Security + the rustls root store for the master's CA;
background-execution limits for long-lived streams (use pull/`GetHotSubgraph` +
`GetPendingResults` polling rather than a held-open `SyncStream` on iOS).

✅ **Gate 2 pass** = a real hot-subgraph pulls over mTLS on both platforms.

## 3. Gate 3 — on-device generation (llama.cpp)

The seam is scaffolded: `edge/mlx-runtime/src/llama.rs`, feature `llama`,
`Backend::Llama`, opt-in via `LUCIFER_LLAMA_MODEL`. Remaining:

1. Add the dep: `llama-cpp-2 = "<latest>"` (build.rs compiles llama.cpp; needs
   cmake + the Metal toolchain on iOS, NDK + Vulkan on Android).
2. Implement `LlamaCppInference::generate_stream`: load the GGUF at `model_path`
   → build context → tokenize prompt → sampling loop → yield `InferenceChunk`s,
   final one `done = true`.
3. Smoke-test on macOS (`--features llama`, `LUCIFER_LLAMA_MODEL=/path/to.gguf`),
   then cross-compile + run on device with a small GGUF (e.g. Qwen2.5-0.5B).

✅ **Gate 3 pass** = a token stream from a GGUF model on both iOS and Android.

## 4. Gate 4 — secure storage

Replace the desktop keychain helper with platform backends: iOS Keychain
(`security-framework`) and Android Keystore (via a small Kotlin plugin or the
`tauri-plugin-stronghold`/biometric route). Persist the pairing JWT + client cert
+ key the same way the desktop `keychain.rs` does.

✅ **Gate 4 pass** = pairing material persists across app restarts on both platforms.

## Outcome

| Gate | iOS | Android | Notes |
|------|-----|---------|-------|
| 1 build | ✅ 2026-06-12 | 🟡 2026-06-12 | iOS: app **launched + rendered** on the iOS 26.5 simulator (`tauri ios dev`, vite at :1420). Android: 14.6 MB release APK built; launch pending an AVD/system image or a physical device. |
| 2 mTLS sync | ☐ | ☐ | |
| 3 on-device generate | ☐ | ☐ | |
| 4 secure storage | ☐ | ☐ | |

### Gate 1 field notes (2026-06-12)
- The desktop crate's bin→lib restructure + capability split landed in PR #24.
- **Xcode 26.5 componentized-install quirk:** `xcodebuild -downloadPlatform iOS`
  downloaded the 26.5 simulator runtime but a duplicate registration left the
  disk image mounted at a `_1`-suffixed path the record didn't point to — the
  runtime stayed invisible to `simctl list runtimes`, and tauri then refused to
  build ("Simulator SDK 26.5 is not installed", opening Xcode instead).
  **Recovery:** `xcrun simctl runtime delete <broken-uuid>`, then
  `xcodebuild -downloadPlatform iOS -exportPath <dir>` — the export path forces
  a clean download AND registers the runtime properly.
- Simulator devices created under an older runtime (iOS 18.6) do not satisfy
  tauri's SDK check; create one explicitly on the new runtime:
  `xcrun simctl create Lucifer-iPhone16 "iPhone 16" com.apple.CoreSimulator.SimRuntime.iOS-26-5`.
- Direct `xcodebuild` against `gen/apple` does NOT work standalone: the
  "Build Rust Code" phase connects back to a running tauri-CLI orchestration
  server. Always go through `cargo tauri ios dev/build`.

- **All pass** → flip Phase 4c to **GO** and scope the production mobile app.
- **2 or 3 fails on a platform** → fall back to RN + Rust-core-via-uniffi for that
  platform only (master unchanged).
