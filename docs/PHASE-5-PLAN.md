# Lucifer AI — Phase 5 Plan: Mobile (iOS + Android)

> **Status:** Proposed · **Date:** 2026-07-21 · **Owner:** Ramsanjiev
> **Predecessors:** [`phase-4c-mobile-go-no-go.md`](./phase-4c-mobile-go-no-go.md) (Option A — Tauri
> 2.0, GO conditional on the spike) · [`phase-4c-spike-runbook.md`](./phase-4c-spike-runbook.md)
> (the four device gates) · [`UPDATE-PLAN.md`](./UPDATE-PLAN.md) (Phase 4.5 hardening — complete).
> **Duration:** ~8 weeks · **Entry:** all Phase 5 entry gates met (below).

---

## 1. Entry state

Phase 4.5 is merged: all nine Phase 5 entry gates from `UPDATE-PLAN.md §4` are satisfied — the PII
cloud-dispatch gate, the dependency/CI truth-up, the MCP supply-chain checks, the corrected mobile
record (llama.cpp for both platforms), the memory-decay fix, and cache pricing are all in `main`.

**The map is accurate.** Per Phase 4c the decision is **Option A: Tauri 2.0 mobile**, one Rust
workspace + React frontend across desktop and mobile, with **llama.cpp (GGUF via `llama-cpp-2`)** as
the single on-device inference engine. iOS Foundation Models is a narrow summarise/extract path only
(4,096-token ceiling), never the primary assistant. Sizing follows the device-tier prefill budget in
`ARCHITECTURE.md §8.2`.

**Spike status** (from the runbook's outcome table):

| Gate | iOS | Android | Remaining |
|------|-----|---------|-----------|
| 1 build | ✅ launched (sim) | 🟡 APK built | Android on-device/AVD launch |
| 2 mTLS sync | ☐ | ☐ | pull hot-subgraph over mTLS on-device |
| 3 on-device generate | ☐ | ☐ | wire `llama-cpp-2` token loop |
| 4 secure storage | ☐ | ☐ | iOS Keychain / Android Keystore |

**Everything below the spike is device- and host-gated** — it needs a Mac with Xcode + Android
Studio + physical/simulated devices, and cannot be validated headless/CI (§7).

---

## 2. Phase gate — finish the spike first

Phase 5 production work **does not start** until spike gates 2–4 pass on both platforms
(runbook §2–4). Sequence them as the first ~2 weeks:

- **S2 — mTLS sync on-device.** `sync-client` (tonic + rustls) already cross-compiles. Verify a real
  `GetHotSubgraph` pull renders a node count. Watch-out: on iOS prefer pull + `GetPendingResults`
  polling over a held-open `SyncStream` (background-execution limits) — the proto already has
  `GetPendingResults`.
- **S3 — on-device generation.** Add `llama-cpp-2`, implement `LlamaCppInference::generate_stream`
  in `edge/mlx-runtime/src/llama.rs` (load GGUF → context → tokenize → sample loop → `InferenceChunk`s,
  final `done=true`). Smoke-test on macOS, then cross-compile with a small GGUF (Qwen2.5-0.5B).
- **S4 — secure storage.** iOS Keychain (`security-framework`) + Android Keystore backends behind the
  desktop `keychain.rs` interface; pairing JWT + client cert persist across restarts.

**If S2 or S3 fails on a platform** → per the 4c decision, fall back to **Option B (React Native +
Rust-core-via-uniffi) for that platform only**; the master side is unchanged. Record the decision in
the runbook outcome table before proceeding.

---

## 3. Production workstreams

Numbered P5-*; W-style ticket tables to be filled once the spike clears. Each names its primary
crates/paths so the work is scoped to the existing architecture.

### P5-1 — Inference productionisation `P0`
Beyond the spike's token loop: model lifecycle + the tier budget as an enforced contract.
- Model manager: download/verify (checksum) a GGUF into app storage; lazy-load; unload on memory
  pressure. Files: `edge/mlx-runtime` (new `edge-inference` split if the crate grows), `edge-store`.
- Enforce the **device-tier prefill budget** (`ARCHITECTURE.md §8.2`): cap the injected context by
  tier before generate; **prefill, not decode, is the constraint**. Trim the Librarian block hard on
  mid/low tiers; offload to master via gRPC when over budget.
- iOS Foundation Models path (summarise/extract/tag only, 4K ceiling) behind the `Inference` trait
  as a *secondary* backend — never the assistant.

### P5-2 — Mobile app shell `P0`
- Land the desktop `bin → lib` restructure (runbook §1) on `main` as the shared entry point.
- Screens: reuse React frontend; adapt Onboarding/ConnectionBar; replace tray/global-hotkey
  (desktop-only) with share-sheet / notification / widget entry points.
- Background sync discipline: pull-based on iOS (BackgroundTasks), WorkManager on Android.

### P5-3 — Secure storage + pairing `P0`
Productionise S4 across the offline-queue and sync-client credential paths; biometric gate on unlock.

### P5-4 — Model OTA + upgrade `P1`
- Delta model delivery: Play Asset Delivery (Android) / app-update + OTA delta (iOS), per the
  §8.4 model-management table. Rollback path. Ties into the existing `model_upgrade/` cadence.

### P5-5 — Mobile-native surfaces `P1` (post-GO in 4c §8)
- Share-extension capture (any app → Lucifer), home-screen widget (WidgetKit / Jetpack Glance),
  push relay (APNs / FCM), App Intents / SiriKit + Assistant actions.

### P5-6 — Offline queue on mobile `P1`
`offline-queue` (SQLite WAL) ports as-is; wire replay-on-reconnect to the mobile lifecycle and the
conflict resolver (server-authoritative LWW, already shipped).

---

## 4. Sequencing

```
Wk 1–2  Spike gates 2–4 (S2 mTLS · S3 llama.cpp · S4 keychain)  ── device-gated; go/no-go
Wk 3–4  P5-1 inference productionisation · P5-2 app shell        (parallel once S2/S3 pass)
Wk 5–6  P5-3 secure storage · P5-6 offline queue · P5-4 OTA
Wk 7    P5-5 mobile-native surfaces (share / widget / push)
Wk 8    Hardening, field test on real devices, store-submission dry run
```

**Critical path:** S3 (on-device generate) → P5-1. S3 is the single highest-risk item; it is why the
4c gate exists. **Fastest useful cut:** S2 + S3 + P5-2 = a syncing app that answers locally.

---

## 5. Verification

| What | How | Where |
|------|-----|-------|
| Spike gates 1–4 | On-device, per the runbook; record in its outcome table | Mac + Xcode + Android Studio |
| Inference correctness | Token stream from a small GGUF; latency vs. the device-tier budget (TTFT) | real device |
| Sync on-device | mTLS `GetHotSubgraph` renders node count; offline replay on reconnect | real device |
| Secure storage | Pairing material survives restart; biometric gate | real device |
| Regression | `cargo test -p mlx-runtime` for the seam; existing master suite unaffected | CI (headless) |

**Only the seam-level Rust tests and the master suite run in CI.** Build, launch, inference, sync,
and keychain are all device-bound (runbook preamble) — schedule them on a hardware runner or a dev
Mac. Do not report these as verified until exercised on a device.

---

## 6. Explicitly out of scope

| Item | Why |
|------|-----|
| MLX-on-iOS perf path | Optional later upgrade behind the `Inference` trait; llama.cpp is the baseline |
| Windows desktop | Separate track (ONNX Runtime / Ollama on desktop is fine) |
| watchOS / WearOS | Phase 6 (no local model; relays to phone) |
| New master-side features | Phase 5 is a client; the master API is frozen for this phase |

---

## 7. Environment note

This plan is authored in a headless cloud session with **no Xcode, Android SDK, devices, or a GGUF
model** — so none of the device gates can be executed or verified here. The plan, the seam, and the
Rust-level tests are what a headless session can produce; the spike itself must run on a Mac +
Android toolchain per the runbook. Committing the plan lets a device-equipped engineer (or a
suitably provisioned runner) execute it directly.
