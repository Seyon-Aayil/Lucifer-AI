# Lucifer Edge

Rust workspace for the edge clients (Phase 4b+). The first member is
`sync-client`, a `tonic` gRPC client for the master sync protocol defined in
`infra/proto/lucifer_sync.proto`.

## Layout

```
edge/
├── Cargo.toml         # workspace manifest
└── sync-client/       # gRPC client crate
    ├── build.rs       # compiles proto via vendored protoc
    └── src/
        ├── lib.rs       # public API: SyncClient, ClientConfig, AuthInterceptor
        ├── auth.rs      # JWT bearer interceptor
        ├── config.rs    # connection settings + validation
        ├── error.rs     # typed errors
        └── transport.rs # mTLS channel + RPC helpers
```

## Build

```sh
make edge-build      # release build
make edge-test       # unit tests
make edge-lint       # fmt --check + clippy -D warnings
```

`protoc` is bundled via `protoc-bin-vendored`; no system install required.

## What's next

- Tauri 2.0 app crate that links `sync-client` and exposes commands to the
  WebView frontend.
- `sqlite-vec` edge graph store crate.
- Offline action queue + deterministic replay (SQLite WAL).
- Hotkey overlay + menu-bar UI.
