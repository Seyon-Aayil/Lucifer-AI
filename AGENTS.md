# Lucifer AI — Agent Instructions

> Read this entire file before writing any code. It is the single source of truth
> for conventions, patterns, and non-negotiable rules in this repository.

---

## What This Project Is

**Lucifer AI** is a distributed, privacy-first, self-evolving personal AI OS.
It runs across a mesh of edge devices (macOS, Windows, iOS, Android, Apple Watch)
with a central master server. See `ARCHITECTURE.md` for the full system design.

**Key properties every contribution must preserve:**
- Privacy-by-default: data classified at creation; sensitive data never exits the
  device unencrypted and never reaches cloud LLMs without explicit user consent.
- LLM-agnostic: all inference goes through the `ProviderRegistry` + LiteLLM proxy.
  Never import `anthropic`, `openai`, or `google.*` SDK directly in agent code.
- Cost-aware: every LLM call must carry a `TokenBudget`. Hard caps are enforced by
  LiteLLM. No unbounded inference loops.
- Telemetry everywhere: every agent action, LLM call, and sync event emits an OTel
  span. Never swallow telemetry.

---

## Repository Layout

```
master/          Python — all server-side code (FastAPI, agents, LLM, memory)
  api/           FastAPI gateway, routers, middleware, schemas
  agents/        Agent implementations (base/, librarian/, personal/, ...)
  core/          config.py, logging.py, telemetry.py — touch rarely
  llm/           Provider abstraction, RouteLLM, LiteLLM integration
  token_optimizer/ GPTCache, LLMLingua-2, budget, compressor pipeline
  mcp/           MCP server registry, transport, Docker sandbox, audit
  news/          News sync engine (fetch → extract → score → cluster → digest)
  orchestrator/  LangGraph state graph + HitL checkpoints
  sync/          gRPC server (master side)
  model_upgrade/ Benchmark runner, shadow eval, OTA promoter
  tests/unit/    No external services. Run with: pytest -m "not integration"
  tests/integration/ Requires Docker stack (make up). Mark with @pytest.mark.integration

desktop/         Tauri 2.0 (Rust core + React frontend)
mobile/          iOS (Swift), Android (Kotlin), Watch (SwiftUI), WearOS
integrations/    MCP server implementations, each Dockerized
infra/           docker-compose.yml, migrations, proto, grafana, prometheus
```

---

## Python — Non-Negotiable Rules

### Style
- Python **3.12**. Always add `from __future__ import annotations` as first import.
- Line length: **100**. Enforced by Ruff.
- Type hints: **strict**. All functions must have full type annotations including
  return types. `Any` is only acceptable with an explanatory comment.
- No bare `except:`. Always catch specific exception types.
- f-strings only. No `.format()` or `%` strings.

### Module Docstrings
Every `.py` file must begin with a module-level docstring in this format:
```python
"""
master.module.submodule
========================
One-line summary.

Longer explanation if needed. Keep it factual, not promotional.
"""
```

### Class & Function Docs
- Every public class: docstring explaining purpose and invariants.
- Every public method / function: docstring if behaviour is non-obvious.
- Private helpers (`_name`): inline comment is sufficient.
- No docstrings for `__init__` if the class docstring is sufficient.

### Configuration
- Always use `get_settings()` from `master.core.config`. Never read `os.environ`
  directly in application code.
- Never hardcode URLs, keys, thresholds, or model names. They belong in `.env`.

### Logging
- Always use `get_logger(__name__)` from `master.core.logging`. Never use
  `print()` or the stdlib `logging.getLogger()` directly.
- Log at the correct level: `debug` for traces, `info` for milestones, `warning`
  for recoverable issues, `error` for failures. Never `critical` in library code.
- Structured log calls use keyword args: `log.info("event.name", key=value)`.

### Telemetry
- Import `get_tracer` / `get_meter` from `master.core.telemetry`.
- Wrap every non-trivial operation (LLM call, DB query, MCP tool call, sync) in
  a span: `with tracer.start_as_current_span("span.name"):`.
- Always close spans — use context managers, never manual `.end()`.
- Cost accounting: every LLM response must log `cost_usd` to the telemetry event.

### Async
- The entire master server is `async`/`await`. Never use `time.sleep()` — use
  `await asyncio.sleep()`. Never use blocking I/O in async functions.
- Use `asyncio.gather()` for concurrent independent operations.
- DB connections: use connection pools (asyncpg, neo4j async driver). Never open a
  new connection per request.

### Error Handling
- Raise domain-specific exceptions (define in `master/core/exceptions.py`).
- HTTP errors: raise `fastapi.HTTPException` at the router layer only.
  Never raise `HTTPException` inside agents or services.
- Agent failures: return `AgentResponse(status="error", ...)` — do not raise.
- Retries: use `tenacity` with exponential backoff. Max 3 attempts unless
  the architecture doc specifies otherwise.

---

## LLM & Agent Rules

### Calling LLMs
```python
# CORRECT — always go through the registry
provider = await registry.select(query=..., tier=..., agent_id=..., max_budget_usd=...)
response = await provider.complete(req)

# WRONG — never import provider SDKs directly
import anthropic  # ❌
client = anthropic.Anthropic()  # ❌
```

### Token Budget
- Every agent method receives an `AgentRequest` with `token_budget: TokenBudget`.
- Never exceed `request.token_budget.input_limit` when building context.
- Call `TokenOptimizer.prepare(context, budget)` before dispatch — it handles
  GPTCache check, LLMLingua-2 compression, and hierarchical trim in one call.

### Memory — Librarian Pattern
- Agents **do not** maintain their own memory stores.
- Read context: `await librarian.get_context_package(agent_id, task_type)`
- Write: include `memory_deltas` in `AgentResponse` — the orchestrator applies them.
- Never call `mem0` or `zep` clients directly from agent code. Only LibrarianAgent
  holds those clients.

### Agent Response Contract
```python
# Always return AgentResponse — never raise from execute()
return AgentResponse(
    task_id=request.task_id,
    agent_id=self.agent_id,
    status="success",         # "success" | "partial" | "escalate" | "error"
    result={...},
    artifacts=[],
    memory_deltas=[],
    token_usage=TokenUsage(...),
    escalation_reason=None,
    error_message=None,
    cost_usd=0.0,
)
```

### HitL (Human-in-the-Loop)
- `risk_tier >= HIGH`: the orchestrator's HitL node intercepts. Agents must set
  `status="escalate"` and populate `escalation_reason`. Never auto-execute
  high-risk actions.
- Risk classification lives in `master/agents/base/risk.py`. Do not duplicate it.

### MCP Tools
- Agents declare permitted tools in their `agent_manifest.json`.
- Always call tools via `MCPClient.invoke(tool_name, validated_input)`.
- Input is schema-validated before invocation — the MCP layer handles this.
  Do not re-validate inside agent code.
- Every MCP call is appended to the HMAC audit log automatically.

---

## Security Rules (Non-Negotiable)

1. **PII before dispatch**: Any content going to a cloud LLM must pass through
   `PIIScanner.scan()` first. This is enforced at the API router layer, but agents
   building their own prompts must call it explicitly on dynamic content.

2. **Data classification**: Never pass `Restricted` or `Secret` nodes to a
   cloud LLM. Check `node.classification` before including in context.
   Local models only for Restricted; no LLM for Secret.

3. **No secrets in code**: All credentials come from `get_settings()`.
   The pre-commit hook `detect-private-key` will block commits with embedded keys.

4. **Audit log**: All writes to the knowledge graph, all MCP tool calls, all
   agent executions are appended to the HMAC audit chain via `AuditLogger`.
   Never suppress audit writes.

5. **Docker isolation for MCP**: Remote/third-party MCP servers run in Docker.
   Never execute remote MCP server code on the host without sandbox.

6. **JWT**: Access tokens expire in 1h. Refresh tokens are single-use (rotated
   on each refresh). Revoked device IDs propagate to Redis within 60s.

---

## Database Conventions

### PostgreSQL / TimescaleDB
- Use `asyncpg` directly for performance-critical queries.
- Use `SQLAlchemy` async ORM for CRUD operations in services.
- Migrations: Alembic. Never write raw DDL unless creating a new migration file.
- Add new columns via migration, never via `ALTER TABLE` in application code.
- All timestamps: `TIMESTAMPTZ` (UTC). Never store local time.
- UUIDs as primary keys (`uuid_generate_v4()`). Never use serial integers.

### Neo4j
- Use the official `neo4j` Python async driver. Never use the HTTP REST API.
- All reads go through `LibrarianClient` — do not open direct Neo4j sessions in
  agent code.
- Cypher parameters always use `$param` syntax. Never f-string Cypher queries
  (injection risk).
- The vector index is named `node_embedding`. Query via:
  `CALL db.index.vector.queryNodes('node_embedding', $k, $embedding)`

### Redis
- Use as: semantic cache backend (GPTCache), daily spend counters, JWT revocation
  store, NATS pub/sub for alerts.
- Key naming: `{namespace}:{entity_type}:{id}` e.g. `lucifer:spend:financial-agent:2026-04-24`.
- Always set TTLs. No unbounded keys.

---

## Testing Rules

### Unit Tests (`master/tests/unit/`)
- No real network, DB, or filesystem I/O. Mock everything external.
- Use `pytest-mock` (`mocker` fixture). Prefer `AsyncMock` for async callables.
- File naming: `test_{module_name}.py`. Test naming: `test_{function}_{scenario}`.
- Coverage target: **≥ 80% branch coverage** on all `master/` code.

### Integration Tests (`master/tests/integration/`)
- Mark with `@pytest.mark.integration`.
- Require `make up` running. Use the real Postgres, Neo4j, Redis from Docker.
- Use a dedicated test database/keyspace — never the dev namespace.
- Clean up all created records in `yield` fixtures (use `autouse=True` for teardown).

### Fixtures
- Shared fixtures live in `master/tests/conftest.py`.
- Agent fixtures: use `AsyncMock(spec=LibrarianClient)` — always spec mocks to
  catch interface drift.

---

## Infra & DevOps

### Docker Compose
- Dev stack: `make up` / `make down`.
- Never modify `infra/docker-compose.yml` for local overrides — use
  `infra/docker-compose.override.yml` (gitignored).
- Service health: all services have Docker healthchecks. Wait for `healthy`
  status before running integration tests.

### Protobuf
- All gRPC definitions live in `infra/proto/`.
- Regenerate stubs with `make proto` after any `.proto` change.
- Never hand-edit generated `*_pb2.py` or `*_pb2_grpc.py` files.

### Migrations
- Postgres: `make migrate-new MSG="description"` → Alembic autogenerates.
- Neo4j: add a new `.cypher` file in `infra/neo4j/migrations/` with next sequence
  number → `make migrate` applies it.
- Migrations are append-only. Never edit an applied migration.

### Environment
- `.env` is gitignored. `.env.example` is the canonical reference — keep it in sync.
- New env vars: add to `.env.example` + `master/core/config.py` field + docstring.

---

## What NOT To Do

| Don't | Do instead |
|-------|-----------|
| `import anthropic` in agent code | Use `ProviderRegistry.select()` |
| `os.environ["KEY"]` anywhere | `get_settings().key` |
| `print(...)` anywhere | `log.info(...)` / `log.debug(...)` |
| `time.sleep(n)` in async code | `await asyncio.sleep(n)` |
| Open a new DB connection per request | Use the shared connection pool |
| Write memory in an agent directly | Return `memory_deltas` in `AgentResponse` |
| Add a bare `except Exception:` | Catch specific types; log + re-raise or return error response |
| Call MCP tools without schema validation | Use `MCPClient.invoke()` — it validates |
| Store secrets in `.env.example` | Use `<change-me>` placeholders only |
| Include `Restricted`/`Secret` data in cloud prompt | Check classification; local-model only |
| Edit generated proto stubs | Run `make proto` |
| Commit directly to `main` | Open a PR; pre-commit hook blocks direct pushes |

---

## Phase Context

**Current phase: Phase 2 complete → Phase 3 in progress.**

---

### Phase 1 — Foundation (✅ Complete)

1. `master/core/auth/` — JWT issue, refresh, device revocation ✅
2. `master/api/middleware/` — PII scanner (router layer), AuthMiddleware ✅
3. `master/llm/` — `LLMProvider` interface + Anthropic, OpenAI, Google, Ollama adapters ✅
4. `master/llm/registry.py` — `ProviderRegistry` with RouteLLM + LiteLLM + circuit breaker + budget tracking ✅
5. `master/token_optimizer/` — GPTCache → budget → BM25 scoring → SimHash dedup → LLMLingua-2 → trim ✅
6. `master/agents/librarian/` — Neo4j CRUD, ACL enforcement, graph_client ✅ (Mem0/Zep stubs: Phase 3)
7. `master/agents/base/` — `BaseAgent`, `AgentRequest`, `AgentResponse`, `RiskTier` ✅
8. `master/orchestrator/` — LangGraph graph: classify → inject → budget → route → execute → hitl → synthesise → write ✅
9. `master/agents/personal/` — `PersonalAgent` wired into orchestrator with real LLM dispatch ✅

**Phase 1 DoD met:** Message via web UI → Librarian context → RouteLLM model selection → response streamed → memory delta written → telemetry emitted.

---

### Phase 2 — MCP Integrations + Budget Hardening (✅ Complete)

1. `master/mcp/transport/docker.py` — DockerTransport: spawn container, health-check, delegate to SSE, destroy on disconnect ✅
2. `master/mcp/registry.py` — `MCPServerRegistry`: loads `infra/mcp_servers.yaml`, creates transports, vends `MCPClient` per agent manifest ✅
3. `integrations/gmail/` — Gmail MCP server (read, search, draft, send, label) + Dockerfile ✅
4. `integrations/gcal/` — Google Calendar MCP server (list, create, find_slot, update, delete) + Dockerfile ✅
5. `integrations/notion/` — Notion MCP server (search, read, create, update block, query DB) + Dockerfile ✅
6. `integrations/github/` — GitHub MCP server (PRs, diff, issues, repo summary, workflows) + Dockerfile ✅
7. `master/agents/*/agent_manifest.json` — Tool ACL manifests for personal, coding, research agents ✅
8. `master/llm/registry.py` — `complete_with_retry()` integrates `SpendTracker`: pre-flight budget check + post-call cost recording ✅
9. `master/api/main.py` — Full lifespan: Postgres pool → Redis → SpendTracker → MCPServerRegistry → NATS, clean shutdown ✅

**Phase 2 DoD met:** Agents securely invoke remote Dockerized MCP tools with strict schema validation, manifest ACL enforcement, and HMAC audit logging.

---

### Phase 3 — Full Agent Swarm + News Pipeline (🔄 In Progress)

**DoD to exit Phase 3:** All 5 specialist agents operational; news digest pipeline running on schedule; Mem0/Zep memory fully wired; AsyncPostgresSaver replacing MemorySaver in LangGraph.

Priority order:

1. **Agent Pool Registry** (`master/orchestrator/agent_pool.py`) ✅
   - `AgentPool.build_default()` auto-registers all agents; silently skips modules not yet importable
   - `AgentPool.resolve(agent_id, **deps)` instantiates the correct agent; falls back to `personal-agent`
   - `AgentPool.reload()` for hot-reload without process restart
   - `execute_node` now uses module-level `_agent_pool` singleton instead of hardcoded `PersonalAgent`
   - `BaseAgent.AGENT_ID: ClassVar[str]` added to enforce the registry contract on all subclasses
   - Status: **Done**

2. **Remaining Specialist Agents** ✅
   - `master/agents/coding/agent.py` — PR review, repo summary, issue creation via GitHub MCP ✅
   - `master/agents/financial/agent.py` — Spend tracking; write intents always escalate for HitL ✅
   - `master/agents/health/agent.py` — Wellness queries; enforces Ollama-only (no cloud LLM) ✅
   - `master/agents/research/agent.py` — Notion search + summarisation + GitHub repo context ✅
   - Agent manifests created for financial and health agents ✅
   - Status: **Done**

3. **LibrarianAgent Full Wiring** (`master/agents/librarian/`)
   - `mem0_client.py` — Real Mem0 API integration (episodic memory)
   - `zep_client.py` — Real Zep/Graphiti API integration (temporal graph)
   - `context_builder.py` — Interface wired; async `build()` returns `ContextPackage`; Neo4j/Mem0/Zep queries pending ✅ (interface done)
   - `memory_writer.py` — Apply `MemoryDelta` list to all three stores
   - `decay_scheduler.py` — APScheduler job for nightly decay pass
   - Status: **Stubs in place — logic missing**

4. **Orchestrator Real LibrarianClient** (`master/orchestrator/graph.py`)
   - `context_inject_node`: wired to `ContextBuilder(GraphClient)` with graceful fallback ✅
   - `memory_write_node`: replace direct GraphClient call with `librarian.apply_deltas(memory_deltas)` to write across all three stores
   - Status: **Partially done — memory_write_node still uses direct GraphClient**

5. **AsyncPostgresSaver Checkpointer** (`master/orchestrator/graph.py`)
   - Replace `MemorySaver()` with `AsyncPostgresSaver` for persistent multi-turn conversation state
   - Requires `langgraph-checkpoint-postgres` dependency
   - Status: **In-memory only — must be upgraded**

6. **News Sync Engine** (`master/news/`)
   - Fetch → extract → score → cluster → digest pipeline
   - Sources: RSS, HackerNews API, Reddit API
   - Scheduled via APScheduler; writes `NewsItem` nodes to Neo4j
   - Status: **Directory empty — must be built**

7. **Intent Classifier Upgrade** (`master/orchestrator/graph.py`)
   - `_ollama_intent_classifier()` calls Ollama `mistral` with structured JSON prompt; falls back to rule-based on timeout/error ✅
   - Status: **Done**

8. **Content Policy Middleware** (`master/api/middleware/content_policy.py`)
   - Implement actual safety classification beyond the current stub
   - Status: **Stub — must be implemented**

9. **Token Optimizer — context_profiler.py / deduplicator.py stubs**
   - These are already implemented inline in `pipeline.py` (BM25 scoring + SimHash dedup)
   - The standalone class files are redundant stubs — they can be removed or given proper implementations if needed as standalone components
   - Status: **Low priority cleanup**

10. **MCP Integration for Agents** ✅
    - `BaseAgent` now accepts `mcp_client` parameter stored as `self._mcp`
    - `PersonalAgent._summarise_calendar` and `_schedule_meeting` invoke `gcal` MCP tools with graceful fallback
    - `execute_node` extracts `mcp_registry` from LangGraph config and passes a wired `MCPClient` to the agent
    - `chat.py` REST and WebSocket handlers inject `app.state.mcp_registry` into graph config
    - Status: **Done**

Do not start Phase 4 work (Desktop Tauri app) until Phase 3 DoD is met.
