# Lucifer Edge

Rust workspace for the edge clients (Phase 4b+). The first member is
`sync-client`, a `tonic` gRPC client for the master sync protocol defined in
`infra/proto/lucifer_sync.proto`.

## Layout

```
edge/
├── Cargo.toml             # workspace manifest
├── sync-client/           # gRPC client crate
│   ├── build.rs           # compiles proto via vendored protoc
│   └── src/
│       ├── lib.rs         # public API: SyncClient, ClientConfig, AuthInterceptor
│       ├── auth.rs        # JWT bearer interceptor
│       ├── config.rs      # connection settings + validation
│       ├── error.rs       # typed errors
│       └── transport.rs   # mTLS channel + RPC helpers
├── desktop-bindings/      # Tauri 2.0 IPC commands wrapping sync-client
│   └── src/
│       ├── lib.rs         # public API: ClientHandle, ConnectArgs, handlers!()
│       ├── commands.rs    # connect_master / disconnect / get_hot_subgraph / …
│       ├── state.rs       # tokio::Mutex-protected SyncClient handle
│       └── types.rs       # JS-friendly DTOs + serializable error
├── edge-store/            # SQLite + sqlite-vec edge KG mirror
│   └── src/
│       ├── lib.rs         # public API: EdgeStore, NodeDelta, EdgeDelta
│       ├── store.rs       # EdgeStore: open, apply_manifest, vector_search
│       ├── manifest.rs    # JSON-friendly hot-subgraph DTOs
│       └── error.rs       # typed errors
├── offline-queue/         # SQLite WAL-backed action queue + replay
│   └── src/
│       ├── lib.rs         # public API: OfflineQueue, QueuedAction, MAX_ACTIONS
│       ├── queue.rs       # enqueue / claim_batch / mark_completed / mark_failed
│       ├── types.rs       # ActionStatus + DTO
│       └── error.rs       # typed errors
├── ollama-sidecar/        # Local Ollama subprocess + HTTP client
│   └── src/
│       ├── lib.rs         # public API: OllamaProcess, OllamaClient
│       ├── process.rs     # spawn / wait_ready / shutdown (kill_on_drop)
│       ├── client.rs      # list / pull / generate / chat (streaming)
│       ├── types.rs       # ChatMessage / GenerateChunk / ModelInfo
│       └── error.rs       # typed errors
├── edge-cli/              # `lucifer-edge` integration-test CLI
│   └── src/main.rs        # subcommands: ping / store-stats / enqueue / claim / ollama-*
└── desktop/               # Tauri 2.0 binary shell (macOS first)
    ├── Cargo.toml         # depends on desktop-bindings, sync-client
    ├── tauri.conf.json    # window, identifier, icon paths, bundle targets
    ├── build.rs           # tauri_build::build()
    ├── src/main.rs        # builder + manage(ClientHandle) + handlers!()
    ├── capabilities/      # Tauri 2.0 ACL grants (default.json)
    ├── icons/             # 32/128/128@2x PNG (RGBA), icon.icns, icon.ico
    ├── dist/              # built frontend bundle (React/Vite output)
    └── frontend/          # Vite + React + TS source
        ├── package.json   # vite, react, react-router, @tauri-apps/api, tailwind
        ├── tailwind.config.js  # design tokens (Lucifer Dark)
        ├── src/lib/ipc.ts # typed wrappers around tauri invoke()
        ├── src/lib/agents.ts   # 7-agent registry (color, label, blurb)
        ├── src/components      # Sidebar, ConnectionBar
        └── src/screens         # Conversation, HotkeyOverlay, MenuBar,
                                # KnowledgeGraph, Approvals, Onboarding
```

## Build

```sh
make edge-build      # release build
make edge-test       # unit tests
make edge-lint       # fmt --check + clippy -D warnings
```

`protoc` is bundled via `protoc-bin-vendored`; no system install required.

## Wiring desktop-bindings into a Tauri 2.0 shell

```rust
fn main() {
    tauri::Builder::default()
        .manage(lucifer_desktop_bindings::ClientHandle::default())
        .invoke_handler(lucifer_desktop_bindings::handlers!())
        .run(tauri::generate_context!())
        .expect("failed to launch Lucifer desktop");
}
```

Call from the WebView:

```ts
import { invoke } from "@tauri-apps/api/core";

await invoke("connect_master", { args: {
    masterEndpoint: "https://lucifer.local:50051",
    deviceId: "device-mac-01",
    clientCertPath: "/path/to/client.pem",
    clientKeyPath:  "/path/to/client.key",
    caCertPath:     "/path/to/ca.pem",
    jwt:            "<short-lived JWT>",
}});

const manifest = await invoke("get_hot_subgraph", { deviceId: "device-mac-01", lastSyncAtMs: 0 });
```

## macOS production bundle

```sh
# Unsigned dev .app + .dmg (good for local install)
make edge-frontend-build
cd edge && cargo install --locked tauri-cli@^2 --quiet  # one-time
cd edge && cargo tauri build --no-bundle                 # binary only
cd edge && cargo tauri build                              # full .app + .dmg

# Signed + notarised (Apple Developer Account required)
export APPLE_CERTIFICATE="<base64 of .p12>"
export APPLE_CERTIFICATE_PASSWORD="<p12 password>"
export APPLE_SIGNING_IDENTITY="Developer ID Application: <your name> (<team-id>)"
export APPLE_ID="<apple-id>"
export APPLE_PASSWORD="<app-specific password>"
export APPLE_TEAM_ID="<team-id>"
cd edge && cargo tauri build
```

Bundles land in `edge/target/release/bundle/{macos,dmg}/`.

## CI

`.github/workflows/edge.yml` runs on every push touching `edge/**`:
fmt-check → clippy → test → build, then `pnpm install && tsc -b && vite build`
for the frontend. Locked to the same Rust 1.81 toolchain the workspace pins.

## What's next

- Tauri 2.0 binary crate (window, hotkey, menu-bar, icons) — host for the
  WebView and the Stitch-generated screens.
- `sqlite-vec` edge graph store crate.
- Offline action queue + deterministic replay (SQLite WAL).
- Hotkey overlay + menu-bar UI integration.
