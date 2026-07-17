# Lucifer AI — Update Plan: Phase 4.5 "Hardening"

> **Status:** Proposed · **Date:** 2026-07-17 · **Owner:** Ramsanjiev
> **Companion:** [`ARCHITECTURE-DELTA.md`](./ARCHITECTURE-DELTA.md) — the decisions this plan implements.
> **Position in roadmap:** inserted between **Phase 4c** (mobile go/no-go, decided) and **Phase 5** (mobile build).
> **Duration:** 3 weeks · **Blocking:** yes, for Phase 5 entry.

---

## 1. Why a phase, and why here

The validation pass produced 12 ADRs. Five of the report's findings were retired on contact with the
code — Kuzu, LiteLLM, MCP, Graphiti, and CRDT-vs-LWW were all already correct. But reading the
implementation against the repo's own claims surfaced **three defects the report could not see**
(ADR-001, ADR-011, ADR-012). What remains is real but bounded.

**Why now, and not "later" or "alongside Phase 5":**

1. **Two live defects are destroying or leaking data right now.** ADR-011: the Librarian
   soft-deletes any node not *written* to for 8 days — reads do not refresh it. ADR-001:
   personal-knowledge-graph context reaches cloud LLMs unscanned. Every day of Phase 5 is another
   day of both. Neither gets cheaper to fix, and the decay one is actively deleting the corpus that
   makes the product worth building.
2. **Phase 5 multiplies the cost of every item here.** Mobile adds a second inference path, a second
   keychain, and a second sync client. Removing AutoGen, correcting the Android record, and fixing
   the PII choke point are ~2× more expensive once mobile code depends on them.
3. **The docs are the Phase 5 spec.** `README.md` currently tells a Phase 5 engineer to build Android
   on MediaPipe — a deprecated path the team already voted against. Fix the map before the journey.
4. **It is small.** ~19 person-days. Against an 8-week Phase 5, this is cheap insurance.

**Explicitly deferred to Phase 6:** ADR-009 (lm-eval-harness) and ADR-010 (news/Miniflux — rejected
outright). Neither gates mobile.

---

## 2. Workstreams

### W1 — Close the PII gap `P0` · 5 d · **blocks Phase 5**

Implements **ADR-001** + **ADR-002**. The single most important workstream in this plan.

| # | Ticket | Files | Effort | DoD |
|---|---|---|---|---|
| W1-1 | Move PII enforcement to the dispatch choke point | `master/llm/registry.py` | 2 d | `complete_with_retry()` scans the assembled `CompletionRequest` before any cloud dispatch. Local (Ollama) providers bypass. Router-level scan in `chat.py` retained as defence-in-depth. |
| W1-2 | Reversible pseudonymisation | `master/api/middleware/pii_scanner.py` | 1 d | `PERSON_1`/`PERSON_2` tokens are consistent within a request and de-mapped on the response. No destructive `[PERSON]` masking on the cloud path. |
| W1-3 | Swap detection engine to Presidio | `pii_scanner.py`, `pyproject.toml` | 1.5 d | `presidio-analyzer` + `presidio-anonymizer` behind the **existing** `PIIScanner`/`ScanResult`/`PIIMatch` API. `test_pii_scanner.py` passes **unmodified**. |
| W1-4 | Enable NER on the cloud path + scan cache | `pii_scanner.py`, `core/config.py` | 0.5 d | `use_ner=True` default for cloud dispatch. Scan results cached by node-id + content hash (graph nodes are immutable between writes). p95 added latency **< 40 ms**. |

**Design note (W1-1).** The seam is `ProviderRegistry.complete_with_retry()`, **not** `ContextBuilder`.
Every cloud call funnels through the registry — `AGENTS.md` forbids calling provider SDKs directly,
which makes the choke point airtight and covers future agents, `model_upgrade/replay.py`, and the
benchmark grader for free. Scanning in `ContextBuilder` would leave all three exposed.

**Verification.** A new `test_pii_dispatch_gate.py` asserts: given a `Person` node carrying a real
name + email, a cloud `CompletionRequest` **cannot** be constructed containing the raw values; the
same request to an Ollama provider **is** unmodified.

**Exit gate.** `make test-unit` green; new test proves the property; p95 latency delta measured and
recorded.

---

### W2 — Dependency & CI truth-up `P0` · 2 d · parallel with W1

Implements **ADR-008** + **ADR-004**.

| # | Ticket | Files | Effort | DoD |
|---|---|---|---|---|
| W2-1 | Remove `zep-cloud` | `pyproject.toml` | 0.25 d | Dep gone. `zep_client.py` (self-hosted `httpx`) untouched. Comment in `zep_client.py` records **why** the cloud SDK is banned. |
| W2-2 | Raise LiteLLM floor; pin sqlite-vec exactly | `pyproject.toml`, `edge/Cargo.toml` | 0.25 d | `litellm>=<current>`. `sqlite-vec = "=0.1.x"`. Dependabot watch on both. |
| W2-3 | Retire AutoGen + CrewAI | `pyproject.toml`, `README.md`, `ARCHITECTURE.md`, `AGENTS.md` | 0.5 d | `pyautogen` + `crewai` removed. Grep proves no imports. `mypy` overrides pruned. |
| W2-4 | CodingAgent sandbox → MCP server | `integrations/sandbox/`, `master/agents/coding/` | 1 d *(see note)* | Code execution runs via `DockerTransport` + manifest ACL + HMAC audit — the **same** path as every other tool. |
| W2-5 | Ratchet the coverage gate | `pyproject.toml`, `.github/workflows/test.yml` | 0.5 d | Measure actual; set `--cov-fail-under` to (actual − 2). Ratchet issue opened targeting the 80% that `AGENTS.md:231` already promises. Gate may never be lowered. |

**Note on W2-4.** Budgeted 1 d here, 3 d in ADR-004. The difference: this ticket delivers the
*seam* (an MCP `sandbox` server + manifest entry) using the existing `DockerTransport`; full
CodingAgent feature parity (multi-turn REPL, artefact capture) lands in Phase 6. If CodingAgent's
sandbox is not on the Phase 5 critical path — it is not — ship the seam, defer the polish.

**Exit gate.** `pip install -e ".[dev]"` resolves clean; `grep -r "autogen\|crewai\|zep_cloud"` returns
only historical doc references; CI gate reflects reality.

---

### W3 — MCP supply chain `P1` · 3 d

Implements **ADR-003**. Highest-likelihood real-world attack surface: an agent with mailbox access.

| # | Ticket | Files | Effort | DoD |
|---|---|---|---|---|
| W3-1 | `mcp-scan` in CI | `.github/workflows/lint.yml`, `infra/mcp_servers.yaml` | 0.5 d | Required check. Fails on tool-description injection patterns. Adds < 15 s. |
| W3-2 | Pin MCP images by digest | `infra/mcp_servers.yaml`, `mcp/transport/docker.py` | 0.5 d | `sha256:` digests, not tags. Rug-pull defence. |
| W3-3 | Treat tool results as untrusted | `master/mcp/client.py`, `master/api/middleware/content_policy.py` | 1.5 d | `ToolInvocationResult.content` passes through `content_policy` (which **already** carries injection patterns) before re-entering a prompt. Reuses the component — no new one (AGENTS.md rule 4). |
| W3-4 | Delimit untrusted blocks in prompts | `master/orchestrator/graph.py` | 0.5 d | Tool output enters prompts inside a labelled, delimited untrusted block. |

**Rollout discipline (W3-3).** Ship in **log-and-flag mode**. Enforce only after a 2-week observation
window — a false positive here silently drops a legitimate email, which is worse than the attack it
prevents. Enforcement flips via config, not a redeploy.

**Exit gate.** A crafted Notion page containing `IGNORE PREVIOUS INSTRUCTIONS…` is flagged in the
audit chain and does not alter agent behaviour. Test: `test_mcp_result_injection.py`.

---

### W4 — Correct the mobile record `P1` · 0.5 d · **blocks Phase 5 planning**

Implements **ADR-007**. Docs-only. Cheapest, highest-leverage item in the plan.

| # | Ticket | Files | Effort | DoD |
|---|---|---|---|---|
| W4-1 | Fix Android runtime claim | `README.md` | 0.1 d | Android = *llama.cpp (GGUF) via `llama-cpp-2`*. MediaPipe + ONNX removed — they contradict the team's own 4c decision **and** name a maintenance-only path. |
| W4-2 | Refresh the 4c engine table | `docs/phase-4c-mobile-go-no-go.md` | 0.2 d | MediaPipe → *maintenance-only, not recommended (→ LiteRT-LM)*. ExecuTorch → *1.0 GA (Oct 2025)*, named as the sanctioned fallback if gate 3 fails. **Verdict unchanged: llama.cpp.** |
| W4-3 | Record the Foundation Models ceiling | `ARCHITECTURE.md §14`, `README.md` | 0.1 d | **4,096-token context (input + output combined)** + non-overridable guardrails documented. Scope: on-device summarise/extract/tag **only** — never the primary assistant. |
| W4-4 | Add the device-tier budget table | `ARCHITECTURE.md §14` | 0.1 d | Table from ADR-007 §"2026 device-tier budget" lands as the design contract. Flags: **prefill**, not decode, is the phone constraint; Ollama's MLX backend needs **> 32 GB**; ORT GenAI is **0.x preview → pin exactly**. |

**Why this blocks Phase 5 planning.** A Phase 5 engineer reading `README.md` today is told to build
Android on MediaPipe. That is a deprecated path the team already voted against in `phase-4c`. The
map must match the territory before anyone walks it.

---

### W7 — Fix the memory decay model `P0` · 2 d · **blocks Phase 5**

Implements **ADR-011**. Found in code review; **not in the validation report**.

The Librarian — "the single source of truth for all personal context" — soft-deletes any node not
**written to** for **8 days**. Reads do not refresh it. This is live, ongoing personal-data loss.

| # | Ticket | Files | Effort | DoD |
|---|---|---|---|---|
| W7-1 | Stop the compounding decay | `librarian/decay_scheduler.py` | 0.5 d | `new_score = exp(-rate × days_since_ref)` computed from scratch — **not** `current_score × exp(-rate × days_since_write)`. Idempotent: a missed nightly run cannot corrupt scores. |
| W7-2 | Add `lastAccessedAt`; decay from it | `librarian/graph_client.py`, `context_builder.py`, Neo4j migration | 1 d | Reads stamp `lastAccessedAt` (debounced ≤1 write/node/hour). Decay reference = `max(updatedAt, lastAccessedAt)`. Makes it a *decay* model, not a write-recency model. |
| W7-3 | Pin the horizon with a test | `tests/unit/test_decay_scheduler.py` | 0.25 d | Asserts: untouched 29 d → survives; 31 d → soft-deleted; read on day 20 → survives to day 50. The docstring made a claim the code did not keep — a test cannot. |
| W7-4 | Audit + resurrect damaged nodes | Neo4j migration | 0.25 d | Count nodes with `deletedAt` in the last 90 d; resurrect those whose true horizon had not expired. **Report the count before purging anything.** |

**Why this outranks almost everything else.** `test_decay_scheduler.py` passes today — it asserts the
mechanics, not the horizon. The bug is invisible to CI, silent at runtime, and destroys exactly the
data the whole system exists to keep. Soft-delete (`deletedAt`, no `DETACH DELETE`) is the only
reason it is recoverable rather than a post-mortem — do not "optimise" that away.

**Exit gate.** W7-3 green; W7-4 audit count recorded in the PR description.

---

### W8 — Cache pricing and layer 2 `P1` · 2.5 d

Implements **ADR-012**.

| # | Ticket | Files | Effort | DoD |
|---|---|---|---|---|
| W8-1 | Price all four token classes | `llm/interfaces.py`, `llm/providers/anthropic.py` | 0.5 d | `TokenUsage.cost()` applies cache-read (0.1×) and cache-write (1.25×) rates. Test pins arithmetic against a fixture response, asserting LiteLLM's normalisation rather than assuming it. |
| W8-2 | Stable-order the context block | `librarian/context_builder.py` | 1 d | `ContextPackage` serialises deterministically (stable node sort). **This is the real work** — without a stable prefix the cache never hits. |
| W8-3 | Second `cache_control` breakpoint | `llm/providers/anthropic.py` | 0.5 d | Breakpoint after the Librarian context block. Anthropic allows 4; Lucifer uses 1. Cheapest cost win in the codebase. |
| W8-4 | Hit-rate metric + Gemini caching | `google.py`, telemetry | 0.5 d | `cache_read_tokens / input_tokens` charted. Gemini explicit context caching added. Record that OpenAI prefix caching is automatic — so the asymmetry is intentional, not an oversight. |

**Gate (W8-3).** Enable the second breakpoint by default only at **measured hit-rate ≥ 30%**. Cache
writes cost 1.25× — a context block that churns every turn makes cost *worse*, not better. W8-2 is
the prerequisite, not the nice-to-have.

---

### W5 — LLM-native observability `P2` · 2 d · **hard-sequenced after W1**

Implements **ADR-006**.

| # | Ticket | Files | Effort | DoD |
|---|---|---|---|---|
| W5-1 | Langfuse as an opt-in overlay | `infra/docker-compose.observability.yml`, `Makefile` | 1 d | `make up-full` starts it; **default `make up` unchanged at 14 services**. Langfuse brings ClickHouse + its own Postgres + Redis — too heavy for the default single-user box. |
| W5-2 | Wire via LiteLLM callback | `infra/litellm/config.yaml` | 0.5 d | `success_callback: ["langfuse"]`. **Near-zero application code** — this is why Langfuse over Phoenix. |
| W5-3 | Verify post-scrub ingestion | `master/llm/registry.py` | 0.5 d | Traces contain **pseudonymised** payloads only. Test asserts a raw email address never reaches Langfuse. |

> **⚠ Hard sequencing.** W5 **must not start before W1 ships.** Langfuse stores full prompt payloads
> and does **not** auto-redact. Landing it first would create the least-protected copy of the most
> sensitive data in the system — a strictly worse posture than today. This ordering is not
> negotiable.

**Why it earns its keep.** The token optimiser is 6 lossy stages (GPTCache → budget → BM25 → SimHash
dedup → **LLMLingua-2** → trim). When a compressed prompt produces a bad answer there is currently
**no artefact to inspect** — Jaeger shows a span with a duration, not a prompt. Langfuse makes the
pipeline's trade-offs measurable for the first time, and gives `model_upgrade/` a real eval substrate.

---

### W6 — Routing modernisation `P2` · 2 d · **optional, non-blocking**

Implements **ADR-005**.

| # | Ticket | Files | Effort | DoD |
|---|---|---|---|---|
| W6-1 | Auto Router v2 behind a flag | `master/llm/registry.py`, `core/config.py` | 1.5 d | `select()` signature + `ProviderSelection` return type **unchanged**; internals swapped. RouteLLM stays behind a flag. |
| W6-2 | A/B and decide | — | 0.5 d | 2-week A/B vs `test_modernization_b3_b5.py` fixtures + live shadow traffic. Auto Router wins or ties on **cost-per-satisfied-query** → remove RouteLLM + its `mypy` override. Loses → keep RouteLLM, close ADR-005 as rejected, record why. |

**Do this only if the registry is being touched anyway.** The current setup works. The real prize is
not router accuracy — it is **session affinity preserving prompt-cache prefixes**, which is the
single highest-leverage cost lever in a system with a 4-layer cache design.

**Pre-flight:** confirm Auto Router v2 session affinity in current LiteLLM docs. It is the entire
rationale; if it is not there, drop W6.

---

## 3. Schedule

```
Week 1  ├─ W7-1..4 ──────────────────┐  ⛔ DECAY BUG — data loss, stop the bleeding first
        ├─ W1-1 W1-2 ────────────────┤  PII dispatch gate + pseudonymisation
        ├─ W2-1 W2-2 W2-3 ───────────┤  dep truth-up        (parallel)
        └─ W4-1..4 ──────────────────┘  docs                (parallel, 0.5 d)

Week 2  ├─ W1-3 W1-4 ────────────────┐  Presidio + NER + scan cache
        ├─ W2-4 W2-5 ────────────────┤  sandbox seam + coverage ratchet
        ├─ W3-1 W3-2 ────────────────┤  mcp-scan + digest pinning
        └─ W8-1 W8-2 ────────────────┘  cache pricing + stable context prefix

Week 3  ├─ W3-3 W3-4 ────────────────┐  untrusted tool results (log-and-flag)
        ├─ W8-3 W8-4 ────────────────┤  2nd breakpoint (gated ≥30% hit-rate)
        ├─ W5-1..3 ──────────────────┤  Langfuse  ⚠ gated on W1 complete
        └─ W6-1 (optional) ──────────┘  Auto Router behind flag

        ── Phase 5 entry review ──
```

**Critical path:** W1 → W5. W7 is small, urgent, and independent — start it day 1.
**Fastest useful cut:** W7 + W1 + W2 + W4 ≈ 9.5 d — clears all three P0s plus the Phase 5 blocker.

---

## 4. Phase 5 entry gates

Phase 5 (mobile) begins when **all** of the following are true:

| # | Gate | Owner |
|---|---|---|
| 1 | No raw PII can reach a cloud provider from the retrieval path — proven by `test_pii_dispatch_gate.py` | W1 |
| 2 | `pyautogen`, `crewai`, `zep-cloud` are gone; `pip install -e ".[dev]"` resolves clean | W2 |
| 3 | Coverage gate reflects measured reality and is ratcheting toward the 80% `AGENTS.md` already promises | W2-5 |
| 4 | `mcp-scan` is a required CI check; MCP images pinned by digest | W3 |
| 5 | `README.md` + `phase-4c` name **llama.cpp** for both mobile platforms; no MediaPipe/ONNX references | W4 |
| 6 | Foundation Models' 4K ceiling + the device-tier budget table are in `ARCHITECTURE.md` | W4 |
| 7 | Decay horizon is test-pinned (29 d survives / 31 d does not); `deletedAt` backlog audited and reported | W7 |
| 8 | `TokenUsage.cost()` prices cache reads/writes; a test pins the arithmetic | W8 |
| 9 | Phase 4c spike gates 1–4 pass (unchanged from `phase-4c-mobile-go-no-go.md` §6) | Phase 4c |

Gates 5–7 together mean Phase 5 starts with an accurate map and a proven inference path — the two
things the 4c doc correctly identified as pivotal.

---

## 5. Explicitly out of scope

| Item | Why | Revisit |
|---|---|---|
| lm-eval-harness (ADR-009) | Model-upgrade does not gate mobile; harness is a heavy scheduled-job dep | Phase 6, as `[bench]` extra |
| Miniflux / RSSHub (ADR-010) | The news pipeline **works** (Phase 3 DoD met). The report's advice is for greenfield; this ground is taken | Phase 6, only if feed bugs recur **or** sources > 50 |
| Pydantic AI | LangGraph + Pydantic v2 already cover typed agents | If agent count > 8 |
| CRDTs (Loro/Automerge) | `conflict_resolver.py` vector-clock LWW is **already** the recommended server-authoritative model | Only if concurrent multi-device editing of one artefact becomes real |
| Rewriting the memory stack | Mem0/Zep/Neo4j each used for what it is best at | Keep behind the Librarian interface; watch for platform absorption |
| Arize Phoenix | Lighter than Langfuse, but no prompt management, no native LiteLLM callback | If ClickHouse overhead proves intolerable |

---

## 6. Effort summary

| Workstream | Priority | Effort | Blocks Phase 5 |
|---|---|---|---|
| W7 — Decay bug | **P0** | 2 d | ✅ |
| W1 — PII gap | **P0** | 5 d | ✅ |
| W2 — Deps + CI | **P0** | 2 d | ✅ |
| W3 — MCP supply chain | P1 | 3 d | ✅ (gate 4) |
| W4 — Mobile docs | P1 | 0.5 d | ✅ |
| W5 — Langfuse | P2 | 2 d | ✗ |
| W8 — Cache pricing | P1 | 2.5 d | ✅ (gate 8) |
| W6 — Auto Router | P2 | 2 d | ✗ |
| **Total** | | **19 d** *(15 d blocking)* | |

Three-week calendar at ~70% utilisation, single engineer. Two engineers → ~2 weeks (W7, W1, and
W2/W3/W4 are fully parallel; only W5's dependency on W1 and W8-3's on W8-2 force joins).

---

## 7. Verification before acting

Three claims from the validation report are load-bearing and rest on vendor-adjacent sources.
**Verify each before its ticket starts** — the report's own caveat notes its figures are
directional:

| Claim | Gates | Check |
|---|---|---|
| MediaPipe LLM Inference is maintenance-only | W4-2 | Google AI Edge release notes / LiteRT-LM migration guide |
| RouteLLM is stale (last substantive update ~2024) | W6 | GitHub commit history |
| LiteLLM Auto Router v2 has session affinity | W6 | Current LiteLLM docs — **the entire rationale for ADR-005**; if absent, drop W6 |

Where the report and the code have disagreed, **the code has won** — five findings were retired that
way (see `ARCHITECTURE-DELTA.md` §1). Keep that habit.
