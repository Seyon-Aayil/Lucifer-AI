# Lucifer AI — Architecture Delta (v0.2)

> **Scope:** Reconciles `ARCHITECTURE.md` (v0.1) and the shipped codebase (`main` @ Phase 4b)
> against the *2025–2026 Open-Source Practice Validation* report.
> **Status:** Proposed · **Date:** 2026-07-17 · **Owner:** Ramsanjiev
> **Companion:** [`UPDATE-PLAN.md`](./UPDATE-PLAN.md) — the sequenced work plan implementing this delta.

---

## 1. Executive summary

The codebase is **materially healthier than the validation report assumed**. Five of the report's
highest-severity findings were already absorbed before it was written:

| Report finding | Repo reality | Verdict |
|---|---|---|
| Kuzu archived Oct 2025 → replace | `edge-store` is **SQLite + sqlite-vec**; `ARCHITECTURE.md §14` already records "replaces Kuzu which was archived Oct 2025" | ✅ No action |
| Embed LiteLLM, don't hand-roll a gateway | `litellm` is a first-class dep; `infra/litellm/config.yaml` defines tiers + per-agent budgets; `ProviderRegistry` delegates to the proxy | ✅ No action |
| MCP has won the tool layer | `master/mcp/` is the mandated single entry point; 5 Dockerised MCP servers shipped; per-agent manifests enforce ACL | ✅ No action |
| Adopt Graphiti-style temporal memory | Zep/Graphiti wired in Phase 3 alongside Mem0 (episodic) + Neo4j (semantic) | ✅ No action |
| Prefer server-authoritative LWW over CRDTs | `sync/conflict_resolver.py` is vector-clock LWW + agent-priority tiebreak. **Exactly** the report's recommendation | ✅ No action |

**What remains is a smaller, sharper list: 3 defects, 4 dependency-truth problems, and 4 tooling
upgrades.** None require re-architecture. **The three most important are not on the report's list at
all** — they emerged from reading the code: a PII gap on the retrieval path (ADR-001), a memory-decay
bug that silently destroys personal data on an 8-day horizon (ADR-011), and cost accounting that
discards the cache-token fields it carefully collects (ADR-012).

**Net:** ~65% KEEP-as-built, ~30% MODIFY/FIX, ~5% REPLACE. No item touches the core topology.

> **The pattern worth noting:** the report's value here was not its verdicts — five of those were
> already obsolete against `main`. It was as a *checklist that sent us into the code*. Every P0 in
> this delta was found by reading an implementation against a claim the repo makes about itself.

---

## 2. Architecture Decision Records

### ADR-001 — Extend PII scanning to the retrieval path *(P0 — security defect)*

**Status:** Accepted · **Severity:** High · **Supersedes:** `ARCHITECTURE.md §8.2`

**Context.**
`PIIScanner` is instantiated in exactly one place: `master/api/routers/chat.py:38`, as
`PIIScanner(use_ner=False)`. It scans the **inbound user message only**.

Meanwhile `LibrarianAgent.ContextBuilder.build()` assembles a context package from
`_fetch_neo4j()` + `_fetch_mem0()` + `_fetch_zep()` and hands it to `ProviderRegistry.select()` →
`provider.complete()` → **a cloud LLM**. That path is never scanned.

The retrieved context is, by construction, the *most* PII-dense payload in the system: it is the
personal knowledge graph. The current mitigation — `hot_subgraph.py` excludes
`classification == 'secret'` and `localOnly == true` — protects the **edge sync** path, not the
**cloud inference** path. A `standard`-classified `Person` node carrying a real name, employer, and
email address is retrieved, injected, and shipped to Anthropic/OpenAI/Google with no scan.

This is precisely the failure the validation report names: *"run redaction on retrieved RAG chunks,
not just the user message — retrieved docs often contain more PII than the latest message."*

Two aggravating factors:
- `use_ner=False` is the default in the one place the scanner *is* used, so today only regex
  (email/phone/SSN/card/IP) fires. `PERSON`/`ORG`/`GPE` detection — the categories that matter most
  for a personal KG — is dormant. The comment says "NER enabled in prod via config"; no such config
  branch exists.
- `README.md` advertises *"PII scanned at API gateway; Restricted/Secret stays local"* as a **Core
  Property guarantee**. The guarantee is currently narrower than advertised.

**Decision.**
1. Move PII enforcement from the router to a **choke point on the dispatch path**, not the ingress
   path. The correct seam is `ProviderRegistry.complete_with_retry()` — every cloud call already
   funnels through it (AGENTS.md forbids calling provider SDKs directly, so this is airtight).
2. Scan the **fully-assembled `CompletionRequest`**, after context injection, before dispatch.
3. Make the policy **classification-aware**, not binary: local providers (Ollama) bypass; cloud
   providers enforce mask-or-refuse.
4. Enable NER by default for cloud dispatch (`use_ner=True`), config-gated for latency escape.

**Consequences.**
- (+) Closes the gap for *every* caller, including future agents, without per-router discipline.
- (+) Makes the README's Core Property claim true.
- (−) spaCy NER on every cloud call adds latency. Mitigate: scan only the context block (cache the
  scan by node-id + content hash — nodes are immutable between writes), not the full prompt.
- (−) Over-masking may degrade answer quality ("[PERSON] emailed [PERSON]"). Mitigate: use
  **reversible pseudonymisation** (consistent `PERSON_1`/`PERSON_2` tokens per request, de-mapped on
  response) rather than destructive `[PERSON]` masking.

**Rejected alternative:** scanning inside `ContextBuilder`. Rejected because it leaves the direct
agent → registry path (used by `model_upgrade/replay.py` and future agents) unprotected.

---

### ADR-002 — Adopt Microsoft Presidio as the PII detection engine *(P1)*

**Status:** Accepted · **Modifies:** `ARCHITECTURE.md §8.2`

**Context.** `pii_scanner.py` is a hand-rolled regex table + optional spaCy NER (~200 LOC). It is
clean and well-tested (`test_pii_scanner.py`), but it re-implements a solved problem. The validation
report identifies **Presidio** (Microsoft, actively maintained through June 2026) as the standard;
it ships 30+ recognisers, context-aware confidence scoring, reversible anonymisation operators
(`encrypt`/`hash`/`replace`), and a spaCy/transformers NER backend Lucifer already pays for.

Per `AGENTS.md` rule 4 — *reuse existing functions/libraries; do not create new ones where a library
already does it* — the hand-rolled table is a standing violation of the project's own conventions.

**Decision.** Replace the `_PATTERNS` table and NER call with `presidio-analyzer` +
`presidio-anonymizer` **behind the existing `PIIScanner` / `ScanResult` / `PIIMatch` interface**.
The public API and all existing tests stay valid.

**Consequences.**
- (+) Better recall (IBAN, passport, medical licence, crypto wallets, per-locale IDs — all relevant
  to Financial/Health agents).
- (+) Presidio's `encrypt` operator gives reversible pseudonymisation for free (ADR-001 needs it).
- (+) Deletes ~150 LOC of bespoke regex.
- (−) `presidio-analyzer` pulls a spaCy model at build time (+~50 MB image). Acceptable — spaCy is
  already a dependency.
- (−) One new supply-chain dependency. Mitigate: pin exactly; it is MIT, Microsoft-maintained.

**Migration:** keep `PIIScanner` as a thin adapter class. Zero call-site changes.

---

### ADR-003 — Add MCP supply-chain scanning and treat tool output as untrusted *(P1)*

**Status:** Accepted · **Extends:** `ARCHITECTURE.md §7`

**Context.** Lucifer's MCP posture is already strong: Docker-isolated servers, per-agent manifest
ACLs, JSON-schema validation, HMAC-chained audit. This defends against a *compromised server*
escaping its sandbox.

It does **not** defend against the 2025–2026 MCP threat classes the report catalogues, which attack
the *trust* layer rather than the *isolation* layer:
- **Tool poisoning** (Invariant Labs, Apr 2025) — malicious instructions in a tool's `description`
  field, read by the LLM, never seen by the user.
- **Attractive-metadata attacks** — a tool advertises a name/description that lures the router.
- **Indirect prompt injection via tool results** — e.g. the WhatsApp-MCP message-history
  exfiltration exploit. Lucifer is exposed here: `MCPClient.invoke()` validates the *request* schema
  and returns `ToolInvocationResult`, whose content flows into the next LLM turn **unvalidated**.
  A hostile Gmail message body or Notion page is a live injection vector today.
- **Rug-pull updates** — a pinned-by-name server changes behaviour after approval.

**Decision.**
1. **Scan at CI:** add `mcp-scan` (Invariant Labs) over `infra/mcp_servers.yaml` as a required
   check. Fails the build on tool-description injection patterns.
2. **Pin by digest:** `mcp_servers.yaml` entries pin image `sha256:` digests, not tags. Rug-pull
   defence.
3. **Treat results as untrusted:** run `ToolInvocationResult.content` through the content-policy /
   injection classifier before it re-enters an LLM prompt. `content_policy.py` already exists and
   already carries prompt-injection patterns — wire it to the *egress of MCP*, not just the ingress
   of chat.
4. **Bound the blast radius:** tool results enter the prompt inside a delimited, clearly-labelled
   untrusted block.

**Consequences.**
- (+) Closes the highest-likelihood real-world attack on an agent with mailbox access.
- (+) Reuses `content_policy.py` — no new component (AGENTS.md rule 4).
- (−) One more CI gate; adds ~10s to lint workflow.
- (−) Classifier false-positives could block legitimate mail. Mitigate: log-and-flag mode first,
  enforce after a 2-week observation window.

---

### ADR-004 — Retire AutoGen; consolidate on LangGraph *(P1)*

**Status:** Accepted · **Supersedes:** `ARCHITECTURE.md §3` (Multi-agent row)

**Context.** `pyproject.toml` carries **three** agent frameworks: `langgraph`, `crewai`, and
`pyautogen>=0.3.0`. The validation report is unambiguous: **AutoGen/AG2 is legacy** — merged into
Microsoft Agent Framework 1.0 (April 2026); `pyautogen` is no longer the forward path. The report's
consolidated 2026 landscape is LangGraph (complexity) / Pydantic AI (stability) / CrewAI (speed).

In-repo, AutoGen's only claimed role is the CodingAgent's "code sandbox" (`README.md`,
`ARCHITECTURE.md`). Lucifer already runs Docker-isolated sandboxes for MCP servers
(`mcp/transport/docker.py`) — a second, weaker sandbox abstraction from a deprecated library is
redundant infrastructure.

**Decision.**
- Remove `pyautogen`. Re-express CodingAgent execution as an **MCP server** (`integrations/sandbox/`)
  using the existing `DockerTransport`, manifest ACL, and audit chain.
- Keep `crewai` **only if a shipped agent imports it**; audit shows none do → remove.
- LangGraph 1.0 remains the sole orchestrator. Confirmed correct by the report.

**Consequences.**
- (+) One orchestration model, not three. Fewer transitive deps (`pyautogen` + `crewai` pull large,
  overlapping trees and are a recurring resolver-conflict source).
- (+) Code execution inherits the audited, ACL'd MCP path for free — strictly better security than
  AutoGen's executor.
- (−) CodingAgent sandbox needs rewriting (est. 3 d). Contained: one agent, one new MCP server.

**Deferred:** Pydantic AI for typed sub-agents. Attractive (report: 1.0 in Apr 2026, FastAPI-native
ergonomics), but LangGraph + Pydantic v2 schemas already cover it. Revisit if agent count > 8.

---

### ADR-005 — Replace RouteLLM with LiteLLM Auto Router v2 *(P2)*

**Status:** Proposed · **Modifies:** `ARCHITECTURE.md §4`

**Context.** `ProviderRegistry` lazy-loads a RouteLLM controller and routes on a
`routellm_threshold` (default 0.5) between `strong_model_id` and `weak_model_id`. The report:
RouteLLM's headline result is real (*"95% of GPT-4 performance at 14% of GPT-4 calls"* — Ong et al.,
ICLR 2025) but the project is a **research framework, last substantively updated ~2024** —
"stable-not-evolving". Lucifer carries it as a live routing dependency (`mypy` even needs a
`routellm.*` `ignore_missing_imports` override — a small smell of an untyped, unmaintained dep).

LiteLLM — **already a core dependency and already the execution path** — ships Auto Router v2 with
complexity/semantic/adaptive tiers and, critically, **session affinity to preserve prompt-cache
prefixes**. Lucifer's 4-layer cache design makes cache-aware routing worth more here than the raw
router accuracy.

**Decision.** Move routing into LiteLLM Auto Router v2. Keep `ProviderRegistry.select()`'s signature
and the `ProviderSelection` return type; swap the internals. Raise the LiteLLM floor from
`>=1.40.0` (2024-era) to a current pin.

**Consequences.**
- (+) Deletes a stale research dep; one fewer untyped module.
- (+) Session affinity protects prompt-cache hit-rate — the single highest-leverage cost lever.
- (+) Routing decisions become visible in LiteLLM's own cost telemetry.
- (−) RouteLLM's matrix-factorisation router may outperform on some traffic. **Gate:** A/B for 2
  weeks against `test_modernization_b3_b5.py` fixtures + live shadow traffic; keep RouteLLM behind a
  feature flag until the router wins or ties on cost-per-satisfied-query.

**Explicitly not a blocker.** The current setup works. Do this when touching the registry anyway.

**Update (W6, delivered).** Pre-flight (plan §7) confirmed: LiteLLM Auto Router v2 **does** ship
session affinity (`session_affinity` + `session_affinity_ttl_seconds`, v1.94.x, 2026) — the entire
rationale, so ADR-005 is *not* dropped. But the highest-leverage part (session affinity preserving
prompt-cache prefixes) does not require the full proxy swap, and the RouteLLM-vs-Auto-Router A/B
(W6-2) is an operational decision, not code. So the delivered slice implements **session affinity
in `ProviderRegistry` itself**: a session's first-turn model is pinned (TTL-bounded) and follow-up
turns skip RouteLLM reclassification, preserving the cache prefix. `select()` gained an optional
`session_id`; the return type is unchanged; the behaviour is behind
`session_routing_affinity_enabled` (default off). RouteLLM stays as the classifier. The proxy-level
Auto Router swap + the 2-week A/B remain the open, non-blocking remainder of ADR-005.

---

### ADR-006 — Add Langfuse for LLM-native observability *(P2)*

**Status:** Accepted · **Extends:** `ARCHITECTURE.md §13`

**Context.** Lucifer's observability is a *classic distributed-systems* stack:
OTel → Jaeger (traces) + Prometheus (metrics) + Loki (logs) + Grafana. It answers *"is the system
healthy?"* excellently.

It cannot answer the questions that actually matter for an LLM product: *Which prompt version caused
this regression? What did the model actually see after 6 stages of token optimisation? Did the
LLMLingua-2 compression drop the fact the answer needed? Is v2 of the Librarian context template
better than v1?* Jaeger shows a span with a duration. It does not show the prompt.

This is a real operational gap given Lucifer's aggressive, lossy optimisation pipeline
(GPTCache → budget → BM25 → SimHash dedup → **LLMLingua-2 compress** → trim). When a compressed
prompt yields a bad answer, there is currently **no artefact to inspect**.

The report recommends self-hosted **Langfuse** (MIT core; ClickHouse acquisition Jan 2026): tracing,
prompt versioning, evals, cost tracking, **native LiteLLM integration**.

**Decision.** Add self-hosted Langfuse to `infra/docker-compose.yml`. Wire via LiteLLM's native
callback (`success_callback: ["langfuse"]` in `infra/litellm/config.yaml`) — **near-zero application
code**. Keep OTel/Prometheus/Grafana for infrastructure telemetry. The two are complements: Langfuse
for *"what did the model see and say"*, Grafana for *"is the box on fire"*.

**Consequences.**
- (+) Makes the token-optimiser debuggable and its trade-offs measurable.
- (+) Gives `model_upgrade/` a real eval substrate (currently a bespoke `judge.py` + `golden.py`).
- (+) Prompt versioning replaces hard-coded template strings.
- (−) Langfuse brings **ClickHouse + its own Postgres + Redis** → the dev stack grows from 14 to
  ~16 services. Non-trivial for a single-user box. **Mitigate:** ship it in a
  `docker-compose.observability.yml` overlay, opt-in via `make up-full`. Do **not** add to the
  default `make up`.
- (−) Langfuse does not auto-redact PII → traces would become the *least*-protected copy of the most
  sensitive data. **Hard dependency: ADR-001/002 must land first**, and Langfuse must be configured
  to ingest the **post-scrub** payload. Sequencing is non-negotiable.

**Rejected:** Arize Phoenix (single-process, lighter) — better footprint, but no prompt management
and no native LiteLLM callback. Revisit if the ClickHouse overhead proves intolerable.

---

### ADR-007 — Correct the mobile inference record; confirm llama.cpp *(P1 — docs)*

**Status:** Accepted · **Modifies:** `README.md`, `docs/phase-4c-mobile-go-no-go.md`

**Context.** The Phase 4c decision — *standardise mobile inference on **llama.cpp** behind the
existing `Inference` trait; MLX-on-iOS as a later perf option* — is **correct and independently
validated**. It is arguably better than the validation report's own suggestion, because it shares
GGUF artefacts with the desktop tier and keeps one engine across both platforms.

But the surrounding **documentation is stale** in three ways that will mislead the Phase 5 build:

| Doc claim | 2026 reality (per report) | Impact |
|---|---|---|
| `README.md`: Android = *"ONNX Runtime + MediaPipe"* | **MediaPipe LLM Inference is maintenance-only**; Google directs new work to **LiteRT-LM** | Contradicts the 4c decision (llama.cpp) *and* names a deprecated path |
| 4c doc: *"CoreML / executorch … executorch is cross-platform but early"* | **ExecuTorch 1.0 GA (Oct 2025)**; ships in Instagram/WhatsApp/Messenger/Quest 3/Ray-Ban Meta; 50 KB runtime, 12+ backends | Understates the strongest llama.cpp alternative |
| `README.md`: iOS = *"Apple Foundation Models + Core ML"* | True, **but** Foundation Models has a hard **4,096-token context (input + output)** and non-overridable guardrails | A 4K ceiling is an architectural constraint, not a footnote — it cannot be the general assistant |

**Decision.**
1. **Keep llama.cpp** as the mobile engine. Re-affirm; no change to the 4c verdict or its gates.
2. Correct `README.md`: Android → *llama.cpp (GGUF) via `llama-cpp-2`*; drop MediaPipe/ONNX.
3. Correct the 4c comparison table: MediaPipe → **maintenance-only, not recommended**;
   ExecuTorch → **1.0 GA**, listed as the sanctioned fallback if gate 3 fails.
4. Record the **Foundation Models 4K limit** in `ARCHITECTURE.md §14` and scope its use to
   on-device summarisation / extraction / tagging — **never** as the primary assistant.
5. Add a **model-tier reality table** (below) to `ARCHITECTURE.md`, replacing optimistic prose.

**2026 device-tier budget (from the report; treat as the design contract):**

| Tier | Realistic model | Throughput | Binding constraint |
|---|---|---|---|
| Watch | *none* — relay only | — | No local LLM. Route via paired phone. Confirms current design. |
| Phone | 2–4B, int4 | ~50 tok/s GPU | **Prefill latency**, not decode, is the real UX limit |
| Mac (32–48 GB) | 30–35B MoE, 4-bit | ~100+ tok/s | MLX ≫ llama.cpp on M-series; Ollama's MLX backend needs **>32 GB** |
| Windows (16 GB+ VRAM) | 7–14B | varies | ONNX Runtime GenAI still **0.x preview** → pin exactly |

**Consequences.**
- (+) Phase 5 starts from an accurate map; no wasted MediaPipe spike.
- (+) The 4K Foundation Models ceiling gets designed around, not discovered in week 6.
- (−) Docs-only churn. Cheap. Do it now, before Phase 5 planning locks.

---

### ADR-008 — Truth-up dependencies and CI gates *(P0 — hygiene)*

**Status:** Accepted

**Context.** Four discrepancies between what the repo *claims* and what it *enforces*:

1. **`zep-cloud>=2.0.0` is a declared dependency and is never imported.** `zep_client.py` is a
   hand-rolled `httpx` client against the **self-hosted** Zep v2 REST API at `localhost:8091`. The
   dep is dead weight — and worse, it is the **cloud** SDK sitting in a project whose first Core
   Property is *"privacy-first"*. A future contributor reaching for `from zep_cloud import ...`
   would silently ship personal memory to a third party. Remove it.
2. **Coverage gate is `--cov-fail-under=10`.** `AGENTS.md:231` and `README.md:399` both state
   **"≥ 80% branch coverage, non-negotiable"**. CI enforces 10%. With 350 Python tests the real
   number is almost certainly far above 10 — the gate is simply not doing its job, and the stated
   standard is unenforced.
3. **`litellm>=1.40.0`** — a 2024-era floor on the most load-bearing dependency in the system.
   Blocks Auto Router v2 (ADR-005) and current prompt-cache support.
4. **`sqlite-vec = "0.1"`** — Cargo `^0.1` correctly pins to 0.1.x, which is adequate. But the
   report flags sqlite-vec as **pre-v1 alpha with possible breaking changes**. Pin `=0.1.x` exactly
   and add a renovate/dependabot watch; the edge KG mirror is not a place for surprise upgrades.

**Decision.** Remove `zep-cloud` and `pyautogen`/`crewai` (per ADR-004). Raise the LiteLLM floor.
Pin sqlite-vec exactly. Ratchet the coverage gate: measure actual, set the gate 2 points below,
then step it toward 80% one PR at a time (never lower it).

**Consequences.**
- (+) The README stops making claims CI does not enforce — the whole point of a Core Property table.
- (+) Removes a live footgun (`zep-cloud`) from a privacy-first codebase.
- (−) Raising the coverage gate may block PRs. That is the intent.

---

### ADR-009 — Adopt lm-evaluation-harness as the benchmark backend *(P3)*

**Status:** Proposed · **Extends:** `ARCHITECTURE.md §11`

**Context.** `model_upgrade/benchmark.py` is well-built: provider-agnostic, injected `grade` callable,
unit-testable without network. But `BenchmarkTask` is a bespoke `(prompt, expected, suite)` triple
and the suites are hand-authored. The report identifies **EleutherAI lm-evaluation-harness** as the
de-facto backend for exactly this (used in published on-device quantised-model evaluations, e.g.
arXiv:2505.15030 with harness v0.4.8).

**Decision.** Keep the `BenchmarkRunner` / `GradeFn` / `BenchmarkResult` architecture — it is good and
the seam is already correct. Add an **adapter** that sources standard suites (MMLU subset,
HumanEval-mini) from lm-eval-harness, while keeping hand-authored domain tasks (drawn from real
usage history) as a parallel suite. Composite scoring is unchanged.

**Consequences.**
- (+) Model-upgrade decisions become comparable to published numbers instead of self-referential.
- (+) `golden.py` / `judge.py` keep owning the domain-task path where Lucifer's real signal lives.
- (−) Harness pulls a heavy dep tree. Mitigate: `[project.optional-dependencies] bench` — it is a
  scheduled-job dep, not a runtime one; keep it out of the master image.

**Priority note:** this is a **Phase 6** concern. The model-upgrade system does not gate Phase 5.

---

### ADR-011 — Fix the memory decay model *(P0 — data loss)*

**Status:** Accepted · **Severity:** High · **Modifies:** `ARCHITECTURE.md §6`
**Origin:** Found in code review, prompted by the report's §2 advice — *"adopt Graphiti-style
temporal validity edges rather than inventing your own decay model."* The invented one has a bug.

**Context.**
`master/agents/librarian/decay_scheduler.py` runs nightly:

```python
current_score = float(node.get("decayScore") or _INITIAL_SCORE)   # already decayed last night
days_elapsed  = (now - updated_at).total_seconds() / 86400.0      # total days since WRITE
new_score     = max(0.0, current_score * math.exp(-_DECAY_RATE * days_elapsed))
```

The docstring states the intent: *"score halves roughly every 7 days (ln(2)/7 ≈ 0.099)"*. With
`_DELETE_THRESHOLD = 0.05`, that intent gives a node **~30 days** before soft-deletion
(`exp(-0.099 × 30) = 0.0513`, just above threshold).

**The implementation does not do that.** It multiplies the *already-decayed* `current_score` by a
factor derived from the *total* elapsed time since write. The decay compounds:

| Night | Days since write | Factor | Score | |
|---|---|---|---|---|
| 1 | 1 | 0.9057 | 0.9057 | |
| 3 | 3 | 0.7430 | 0.5521 | |
| 5 | 5 | 0.6096 | 0.2265 | |
| 7 | 7 | 0.5001 | 0.0625 | |
| **8** | **8** | 0.4529 | **0.0283** | **soft-deleted** |

Effective decay is `exp(-rate × N(N+1)/2)` — **quadratic in time, not exponential**. A node not
*written to* for **8 days** is soft-deleted. The intended budget was 30.

**It is worse than that.** The reference timestamp is `updatedAt` — the **write** time.
`lastAccessedAt` **does not exist anywhere in the codebase**, despite `ARCHITECTURE.md`'s `GraphNode`
schema specifying it and the whole design rationale ("low-access nodes fade") depending on it.
**Reads do not refresh the score.** So a node that is written once and read every single day still
soft-deletes on night 8.

This is in the **Librarian** — the component `ARCHITECTURE.md` calls *"the single source of truth for
all personal context."* "My mother's birthday" is written once, read often, and destroyed in a week.
`test_decay_scheduler.py` passes because it asserts the mechanics, not the horizon.

**Decision.**
1. **Stop compounding.** Either compute from scratch — `new_score = exp(-rate × days_since_ref)` —
   or decay by the *inter-run* interval, `current_score × exp(-rate × days_since_last_run)`. Prefer
   the former: it is idempotent and self-healing, so a missed nightly run cannot corrupt the score.
2. **Add `lastAccessedAt`**, written on read by `graph_client` / `ContextBuilder`. Decay from
   `max(updatedAt, lastAccessedAt)`. This is what makes it a *decay* model rather than a
   write-recency model.
3. **Pin the horizon with a test**, not a docstring: assert a node untouched for 29 days survives and
   at 31 days does not. The docstring made a claim the code did not keep; a test cannot.
4. **Never hard-delete.** Current soft-delete (`deletedAt`, no `DETACH DELETE`) is correct — keep it.
   It is the only reason this is recoverable rather than a post-mortem.

**Consequences.**
- (+) Stops silent, ongoing personal-data loss in the Librarian.
- (+) Idempotent decay tolerates missed runs — relevant, since the scheduler is in-process
  APScheduler with no durable run-ledger.
- (−) `lastAccessedAt` adds a write on every graph read. Mitigate: batch/debounce to ≤1 write per
  node per hour; the value is a coarse recency signal, not an audit record.
- (−) **Existing graphs may already be damaged.** Any node soft-deleted before the fix was deleted on
  the buggy horizon. Ship a migration that resurrects nodes whose `deletedAt` is within the last
  90 days and whose true horizon had not expired; audit the count before purging anything.

**Relation to the report.** The report is right that Graphiti-style bi-temporal validity edges are
the mature approach — and the repo already has them (`graph_client.py` writes `validFrom`/`validUntil`
on relationships; `hot_subgraph.py` and the sync proto carry `valid_from`/`valid_until`). But they
answer a *different* question: validity edges model **"was this true then?"**; decay models
**"is this still worth keeping?"** Both are legitimate. Keep both — just make the decay one correct.

---

### ADR-012 — Price cache tokens; extend prompt caching beyond layer 1 *(P1 — cost correctness)*

**Status:** Accepted · **Modifies:** `ARCHITECTURE.md §4`, §5

**Context — two related defects.**

**(a) Cache tokens are captured but never priced.** `AnthropicProvider.complete()` carefully reads
`cache_read_input_tokens` and `cache_creation_input_tokens` off the response into `TokenUsage`, which
plumbs them through to `master/api/schemas`. Then:

```python
def cost(self, cost_per_input: float, cost_per_output: float) -> float:
    return (self.input_tokens * cost_per_input) + (self.output_tokens * cost_per_output)
```

`cache_read_tokens` and `cache_write_tokens` are **never referenced**. The fields exist precisely so
the two rates can be applied differentially — and are not. Anthropic prices cache reads at **0.1×**
base input and cache writes at **1.25×**. Whichever way LiteLLM normalises `prompt_tokens`, the
result is wrong: either cached tokens are billed at full input price (over-billing reads ~10×), or
they are excluded and billed at zero (under-billing the 1.25× write premium entirely).

That figure feeds `SpendTracker`, which enforces the per-agent daily USD caps that `README.md` lists
as a **Core Property**: *"Cost-aware | Per-agent daily USD caps enforced."* The cap is enforced
against a number that is provably wrong on every cached call. Worse, the whole point of prompt
caching is cost reduction, and the system currently **cannot see its own savings** — the one metric
that would justify the feature is discarded three lines after being fetched.

**(b) Prompt caching is layer 1 only.** `_build_litellm_messages()` sets
`cache_control: {"type": "ephemeral"}` on **system messages only**, in the **Anthropic provider
only**. `openai.py`, `google.py` set no caching at all.

The original design specified four cache layers: **static** (system/personas/tool schemas, ~40% of
every prompt) / **user-profile** (Librarian context, ~20%) / **session** / **document**. Only the
static layer shipped. The **user-profile layer is the single biggest semi-static block in the
system** — it is the Librarian context package, and `ARCHITECTURE.md` correctly identifies it as
invalidated only on memory-graph write, i.e. ideal cache material. It is re-sent uncached on every
turn.

The report is unambiguous that this is the wrong thing to leave on the floor: provider-native prompt
caching is *"the highest-leverage move and should be layer 1"* — and Lucifer's expensive stages
(LLMLingua-2 compression, BM25 scoring) run *after* it in the pipeline.

**Decision.**
1. `TokenUsage.cost()` takes cache rates and prices all four token classes. Add a test that pins the
   arithmetic against Anthropic's published multipliers (0.1× read, 1.25× write) with a fixture
   response, so the semantics of LiteLLM's normalisation are asserted rather than assumed.
2. Add a **second `cache_control` breakpoint** after the Librarian context block. Anthropic allows up
   to 4; Lucifer uses 1. This is the cheapest available cost win in the codebase.
3. Add Gemini explicit context caching in `google.py`. OpenAI's prefix caching is automatic — no code
   needed, but record that so the asymmetry is intentional rather than an oversight.
4. Surface `cache_read_tokens / input_tokens` as a hit-rate metric. It is already on the response
   schema; it just needs to reach a dashboard.

**Consequences.**
- (+) Budget enforcement becomes true — the Core Property claim becomes accurate.
- (+) Caching the ~20% user-profile layer at 0.1× read is a direct, mechanical cost reduction.
- (+) Gives ADR-006 (Langfuse) a real number to chart: cache hit-rate over time.
- (−) A second breakpoint requires the context block to be **prefix-stable** across turns —
  ordering must be deterministic, or the cache never hits. `ContextBuilder` must emit nodes in a
  stable sort order. This is the actual work; the `cache_control` line is trivial.
- (−) Cache writes cost 1.25×. A context block that changes every turn makes things **worse**, not
  better. Gate on measured hit-rate ≥ 30% before enabling by default; ADR-005's session affinity
  matters here for the same reason.

---

### ADR-010 — Keep the news pipeline; defer Miniflux *(P3 — no action now)*

**Status:** Rejected for now · **Revisit:** Phase 6

**Context.** The report suggests sourcing feed ingestion from **Miniflux + RSSHub** rather than
writing feed parsing. Lucifer already ships `master/news/` with `feedparser` + `trafilatura` +
`praw` + `arxiv` + HDBSCAN clustering, **and it works** (Phase 3 DoD met).

**Decision.** **No change.** The report's advice is sound *ex ante* — but it is advice for a
greenfield build, and this ground is already taken. Rewriting a working, tested pipeline to adopt
Miniflux buys nothing today.

**Revisit if** either trigger fires: (a) feed-parsing bugs become a recurring maintenance cost, or
(b) the source count exceeds ~50 and per-source scheduling/backoff/ETag handling starts being
re-invented — that is exactly Miniflux's value, and the day it appears, adopt it. `RSSHub` remains
worth adding standalone (it generates feeds for sites that have none) **without** touching the
existing pipeline — it is just another URL.

**Consequences.** (+) Zero work. (−) Carrying ~600 LOC of feed handling that a mature service does
better. Accepted, consciously, until a trigger fires.

---

## 3. Consolidated decision table

| ADR | Area | Current | Verdict | Priority | Effort |
|---|---|---|---|---|---|
| 001 | PII on retrieval path | Ingress-only, `use_ner=False` | **MODIFY** | **P0** | 3 d |
| 002 | PII engine | Hand-rolled regex + spaCy | **REPLACE** | P1 | 2 d |
| 003 | MCP supply chain | Sandbox + ACL + audit | **EXTEND** | P1 | 3 d |
| 004 | AutoGen / CrewAI | 3 agent frameworks | **REPLACE** | P1 | 3 d |
| 005 | RouteLLM | Stale research dep | **REPLACE** | P2 | 2 d |
| 006 | Langfuse | OTel/Jaeger only | **EXTEND** | P2 | 2 d |
| 007 | Mobile inference docs | Stale (MediaPipe/ExecuTorch) | **MODIFY** | P1 | 0.5 d |
| 008 | Deps + CI gates | `zep-cloud`, `cov=10` | **MODIFY** | **P0** | 1 d |
| 009 | Benchmark suites | Bespoke tasks | **EXTEND** | P3 | 3 d |
| **011** | **Memory decay** | **Compounding → 8-day deletion; reads don't refresh** | **FIX** | **P0** | 2 d |
| **012** | **Cache pricing + layers** | **Cache tokens unpriced; layer 1 only** | **MODIFY** | P1 | 2.5 d |
| 010 | News ingestion | Custom `feedparser` | **KEEP** | — | 0 |
| — | Kuzu → SQLite+sqlite-vec | Already done | **KEEP** | — | 0 |
| — | LiteLLM gateway | Already done | **KEEP** | — | 0 |
| — | MCP as tool layer | Already done | **KEEP** | — | 0 |
| — | Mem0 + Zep + Neo4j | Already done | **KEEP** | — | 0 |
| — | Vector-clock LWW (no CRDT) | Already correct | **KEEP** | — | 0 |
| — | LangGraph 1.0 orchestrator | Already correct | **KEEP** | — | 0 |
| — | Tauri 2.0 desktop + mobile | Already correct | **KEEP** | — | 0 |
| — | gRPC/mTLS + NATS JetStream | Already correct | **KEEP** | — | 0 |
| — | llama.cpp for mobile | Already correct | **KEEP** | — | 0 |

---

## 4. What this delta does **not** change

Recorded explicitly so Phase 5 planning does not relitigate settled ground:

- **Topology.** Master + edge mesh, hot-subgraph sync — unchanged and validated.
- **Sync.** gRPC/Protobuf over mTLS + NATS JetStream. The report explicitly validates JetStream's
  leaf-node offline queue/replay for exactly this shape. **No CRDT.** `conflict_resolver.py`'s
  vector-clock LWW + agent-priority is the recommended server-authoritative model. Introduce a CRDT
  (Loro) *only* if concurrent multi-device editing of the same artefact becomes a real requirement.
- **Three-store memory** (Mem0 episodic / Zep temporal / Neo4j semantic). The report's own comparison
  puts Zep/Graphiti ahead on temporal retrieval (63.8% vs Mem0's 49.0% on LongMemEval) and Mem0
  ahead on lightweight personalisation — Lucifer uses each for what it is best at. Keep. **But** keep
  the layer swappable: the report's standing warning is that memory is being absorbed by platform
  vendors (Apple Foundation Models, Cloudflare Agent Memory, Apr 2026).
- **Per-agent ACL on the knowledge graph.** Genuinely differentiated; no framework gives it for free.
- **Token optimiser ordering.** Native prompt cache → semantic cache → routing → compression is the
  report's recommended priority and matches the shipped pipeline.
- **Data classification tiers** and the `secret`/`localOnly` sync exclusions.

---

## 5. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| PII reaches cloud LLM via retrieved context **today** | **Certain** | **High** | ADR-001, P0, ship first |
| Librarian soft-deletes personal nodes on an 8-day horizon **today** | **Certain** | **High** | ADR-011, P0; audit `deletedAt` backlog before any purge |
| Per-agent budget caps enforced against a wrong cost figure | **Certain** | Medium | ADR-012 |
| Indirect prompt injection via Gmail/Notion tool results | Medium | High | ADR-003 |
| Langfuse becomes an unprotected PII sink | Medium | **High** | Hard-sequence after ADR-001/002; ingest post-scrub only |
| Over-masking degrades answer quality | Medium | Medium | Reversible pseudonymisation, not destructive masking |
| Coverage ratchet blocks feature PRs | High | Low | Step gradually; never lower |
| ClickHouse pushes the dev box over budget | Medium | Medium | Opt-in overlay; Phoenix as fallback |
| Zep/Mem0 absorbed by a platform vendor | Low | Medium | Keep memory behind the Librarian interface |
| sqlite-vec pre-v1 breaking change | Low | High | Exact pin + dependabot watch |

---

## 6. Source-quality caveat

Per the validation report's own disclaimer: much of the 2026 tooling landscape it surveys comes from
vendor blogs with commercial incentives, and its benchmark figures (LongMemEval scores, tok/s,
RouteLLM savings, LLMLingua compression ratios) are **directional**. Every claim that gates a
decision here has been cross-checked against the codebase itself. Where the report and the code
disagree, **the code wins** — five findings were retired that way (§1).

Three report claims are load-bearing and should be re-verified before acting:
- **MediaPipe LLM maintenance-only** → check Google AI Edge release notes before ADR-007 lands.
- **RouteLLM stale** → check last commit date before ADR-005 lands.
- **LiteLLM Auto Router v2 session affinity** → confirm in current LiteLLM docs; it is the entire
  rationale for ADR-005.
