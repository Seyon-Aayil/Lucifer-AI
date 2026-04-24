# Lucifer AI

> **Distributed, privacy-first, self-evolving personal AI OS**
>
> Runs across a mesh of edge devices — macOS, Windows, iOS, Android, Apple Watch — with a central master server. All inference is LLM-agnostic, cost-aware, and can run fully offline.

[![CI — Lint](https://github.com/ramsanjiev/Lucifer/actions/workflows/lint.yml/badge.svg)](https://github.com/ramsanjiev/Lucifer/actions/workflows/lint.yml)
[![CI — Tests](https://github.com/ramsanjiev/Lucifer/actions/workflows/test.yml/badge.svg)](https://github.com/ramsanjiev/Lucifer/actions/workflows/test.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## Table of Contents

- [What It Is](#what-it-is)
- [Core Properties](#core-properties)
- [Architecture Overview](#architecture-overview)
- [Tech Stack](#tech-stack)
- [Repository Layout](#repository-layout)
- [Quick Start](#quick-start)
- [Development](#development)
- [Phase Roadmap](#phase-roadmap)
- [Key Design Decisions](#key-design-decisions)
- [Contributing](#contributing)

---

## What It Is

Lucifer AI is a **personal AI operating system** — not just a chatbot. It:

- Knows your calendar, emails, code, health data, and finances
- Runs specialist agents (Personal, Research, Financial, Health, Coding) that collaborate via a LangGraph orchestrator
- Keeps sensitive data **on-device** — Restricted/Secret data never reaches cloud LLMs
- Learns continuously: every interaction updates a personal knowledge graph
- Works **fully offline** on edge devices, syncing to master via gRPC when connected
- Self-upgrades its model weights via a nightly benchmark + OTA pipeline

---

## Core Properties

| Property | Guarantee |
|----------|-----------|
| **Privacy-first** | PII scanned at API gateway; data classified at creation; Restricted/Secret stays local |
| **LLM-agnostic** | All inference through `ProviderRegistry` + LiteLLM proxy — swap models without code changes |
| **Cost-aware** | Per-agent daily USD caps enforced by LiteLLM; Redis spend counter; BudgetExceededError hard stops |
| **Offline-capable** | Edge devices queue actions in SQLite when disconnected; replay deterministically on reconnect |
| **Observable** | Every LLM call, agent action, and sync event emits an OpenTelemetry span + cost metric |
| **Auditable** | All tool calls and graph writes appended to an HMAC-chained audit log |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        MASTER SERVER                            │
│                                                                  │
│  FastAPI Gateway ──► LangGraph Orchestrator ──► Agent Pool      │
│       │                      │                    │             │
│  PII Scanner           HitL Checkpoint     ┌──────┴──────┐      │
│  Auth Middleware        (risk ≥ HIGH)      │  Librarian  │      │
│                                            │   Agent     │      │
│  RouteLLM ──► LiteLLM Proxy               └──────┬──────┘      │
│     │              │                             │              │
│  Weak Model   Strong Model              Neo4j Knowledge Graph  │
│  (Haiku)      (Opus)                   Mem0 Episodic Memory    │
│                                        Zep Temporal Graph      │
│  Token Optimizer Pipeline:                                      │
│  Semantic Cache → Budget → Score → Dedup → Compress → Trim     │
│                                                                  │
│  MCP Layer: Gmail │ GCal │ Notion │ GitHub │ Slack              │
│             (each Dockerized, schema-validated, audited)        │
└─────────────────────┬───────────────────────────────────────────┘
                      │ gRPC (mTLS)
          ┌───────────┼───────────┐
          │           │           │
    macOS/Win      iOS/Android  Apple Watch
    (Tauri 2.0)   (SwiftUI/    (SwiftUI +
    Ollama/MLX    Foundation   HealthKit)
    sqlite-vec    Models)
```

**Sync model**: Edge devices maintain a hot subgraph in SQLite + sqlite-vec. Deltas sync bidirectionally via a vector-clock-based conflict resolver.

---

## Tech Stack

### Master Server (Python 3.12 / FastAPI)

| Layer | Technology |
|-------|-----------|
| API Gateway | FastAPI + Uvicorn |
| Orchestration | LangGraph (state graph + HitL interrupts) |
| Multi-agent | CrewAI (specialist teams), AutoGen (code sandbox) |
| LLM Routing | RouteLLM (complexity classifier) + LiteLLM Proxy |
| LLM Providers | Anthropic Claude, OpenAI GPT-4o, Google Gemini, Ollama (local) |
| Memory — Episodic | Mem0 (self-hosted) |
| Memory — Temporal | Zep / Graphiti |
| Knowledge Graph | Neo4j 5 (GDS + APOC + native vector index) |
| Token Optimizer | GPTCache → LLMLingua-2 → SimHash dedup → hierarchical trim |
| Storage | PostgreSQL 16 + TimescaleDB + pgvector |
| Cache / Pub-Sub | Redis 7 |
| Message Bus | NATS JetStream |
| Object Store | MinIO (S3-compatible) |
| Observability | OpenTelemetry → Jaeger (traces) + Prometheus (metrics) + Loki (logs) |
| Auth | JWT (HS256, 1h TTL) + single-use refresh tokens + Redis revocation |
| Crypto | Argon2id + AES-256-GCM + HMAC-chained audit log |
| Scheduling | APScheduler |
| gRPC Sync | grpcio + protobuf |

### Edge Devices

| Platform | Technology |
|----------|-----------|
| macOS/Windows | Tauri 2.0 (Rust core + React frontend) |
| iOS | SwiftUI + Apple Foundation Models Framework + Core ML |
| Android | Jetpack Compose + ONNX Runtime + MediaPipe |
| Apple Watch | SwiftUI + WatchConnectivity + HealthKit + Whisper.cpp |
| Edge Store | SQLite WAL + sqlite-vec (1536-dim float32) |
| Local LLM | Ollama (GGUF), MLX (Apple Silicon), ONNX (Android) |

### Integrations (MCP Servers — each Dockerized)

Gmail · Google Calendar · Notion · GitHub · Slack · Obsidian · Web Scraper · (more in Phase 2+)

---

## Repository Layout

```
Lucifer/
├── master/                     # Python — all server-side code
│   ├── api/                    # FastAPI: routers, middleware, schemas
│   │   ├── routers/            # auth.py, chat.py, health.py
│   │   ├── middleware/         # auth.py, pii_scanner.py, content_policy.py
│   │   └── schemas/            # Pydantic v2 request/response models
│   ├── agents/
│   │   ├── base/               # BaseAgent, AgentRequest/Response, TokenBudget, TelemetryEmitter
│   │   ├── librarian/          # Neo4j CRUD, context package, ACL, Mem0/Zep clients
│   │   ├── personal/           # PersonalAgent — scheduling, communication, briefings
│   │   ├── research/           # ResearchAgent — web research, papers, deep-dives
│   │   ├── financial/          # FinancialAgent — spending, budgets, investments
│   │   ├── health/             # HealthAgent — vitals, trends, medication
│   │   └── coding/             # CodingAgent — AutoGen sandbox, PR review, test gen
│   ├── llm/
│   │   ├── interfaces.py       # LLMProvider ABC, CompletionRequest/Response
│   │   ├── registry.py         # ProviderRegistry with RouteLLM + circuit breaker
│   │   ├── circuit_breaker.py  # CLOSED/OPEN/HALF_OPEN per-provider
│   │   └── providers/          # anthropic.py, openai.py, google.py, ollama.py
│   ├── token_optimizer/
│   │   ├── pipeline.py         # 6-stage TokenOptimizer (cache→score→dedup→compress→trim)
│   │   ├── semantic_cache.py   # GPTCache + Redis
│   │   ├── compressor.py       # LLMLingua-2 compressor
│   │   └── spend_tracker.py    # Redis counters + TimescaleDB flush
│   ├── mcp/
│   │   ├── client.py           # MCPClient: permission → schema → transport → audit
│   │   ├── audit.py            # HMAC-chained audit logger
│   │   ├── interfaces.py       # MCPTransport ABC, ToolSchema, AgentManifest
│   │   └── transport/          # sse.py (HTTP/SSE), stdio.py
│   ├── orchestrator/
│   │   ├── graph.py            # LangGraph: classify→context→budget→route→execute→hitl→synth→memory
│   │   └── state.py            # OrchestratorState TypedDict
│   ├── news/                   # RSS/Reddit/arXiv fetcher, dedup, scorer, HDBSCAN clusterer
│   ├── sync/                   # gRPC server (master side)
│   ├── model_upgrade/          # Benchmark runner, shadow eval, OTA promoter
│   ├── core/
│   │   ├── config.py           # Pydantic Settings — single source of truth
│   │   ├── logging.py          # structlog + OTel trace ID injection
│   │   ├── telemetry.py        # OTel SDK setup (traces + metrics → OTLP)
│   │   ├── crypto.py           # Argon2id + AES-256-GCM + HMAC chain
│   │   ├── exceptions.py       # Domain exception hierarchy
│   │   └── auth/               # jwt.py, revocation.py, device.py
│   └── tests/
│       ├── unit/               # No external services (pytest -m "not integration")
│       └── integration/        # Requires Docker stack (make up)
│
├── desktop/                    # Tauri 2.0 — macOS/Windows
├── mobile/                     # iOS (SwiftUI), Android (Compose), Watch, WearOS
├── integrations/               # Dockerized MCP servers (gmail, gcal, notion, github, slack)
│
├── infra/
│   ├── docker-compose.yml      # Full dev stack: 14 services
│   ├── litellm/config.yaml     # LiteLLM proxy — all model tiers + per-agent budgets
│   ├── otel/collector-config.yaml
│   ├── prometheus/prometheus.yml
│   ├── grafana/                # Auto-provisioned datasources + dashboards
│   ├── neo4j/migrations/       # Cypher migration scripts + runner
│   ├── postgres/migrations/    # SQL migrations (TimescaleDB, pgvector, audit log)
│   ├── proto/lucifer_sync.proto # gRPC sync service definition
│   └── mcp_servers.yaml        # All MCP server configs + tool JSON schemas
│
├── ARCHITECTURE.md             # Full system design (15 sections)
├── AGENTS.md                   # AI agent coding instructions
├── Dockerfile                  # Master server production image
├── Makefile                    # up / down / test / lint / migrate / proto
├── pyproject.toml              # Python deps + Ruff + mypy + pytest config
└── .env.example                # All env vars documented (no secrets)
```

---

## Quick Start

### Prerequisites

- Docker + Docker Compose
- Python 3.12+
- (Optional) Ollama for local model inference

### 1. Clone & configure

```bash
git clone git@github.com:ramsanjiev/Lucifer.git
cd Lucifer
cp .env.example .env
# Edit .env — fill in API keys (Anthropic / OpenAI / Google)
```

### 2. Start the dev stack

```bash
make up         # Starts all 14 Docker services
```

Services started:
| Service | Port | Purpose |
|---------|------|---------|
| PostgreSQL + TimescaleDB | 5432 | Telemetry hypertable, auth, artifacts |
| Neo4j | 7474 / 7687 | Personal knowledge graph |
| Redis | 6379 | Semantic cache, spend counters, revocation |
| NATS JetStream | 4222 | Agent task / memory / telemetry bus |
| MinIO | 9000 / 9001 | Artifact object store |
| LiteLLM Proxy | 8080 | Unified LLM gateway with budget caps |
| Mem0 | 8090 | Episodic memory API |
| Zep | 8091 | Temporal knowledge graph |
| OTel Collector | 4317 | Trace / metric / log ingestion |
| Prometheus | 9090 | Metrics scraping |
| Grafana | 3000 | Dashboards |
| Loki | 3100 | Log aggregation |
| Jaeger | 16686 | Distributed tracing UI |

### 3. Run database migrations

```bash
make migrate    # Applies Postgres (Alembic) + Neo4j (.cypher) migrations
```

### 4. Start the master API

```bash
make install    # pip install -e ".[dev]"
make dev        # uvicorn master.api.main:app --reload
```

API documentation: `http://localhost:8000/docs`
Health check: `http://localhost:8000/health/ready`

### 5. Run tests

```bash
make test-unit          # Unit tests only (no Docker required)
make test               # Full suite (requires make up)
make test-cov           # With HTML coverage report
```

---

## Development

### Code conventions

All conventions are codified in [`AGENTS.md`](AGENTS.md). Key rules:

```python
# ✅ Config — always via get_settings()
from master.core.config import get_settings
url = get_settings().litellm_proxy_url

# ✅ Logging — always structlog
from master.core.logging import get_logger
log = get_logger(__name__)
log.info("event.name", key=value)

# ✅ LLM — always through registry (never SDK directly)
provider = await registry.select(query=..., agent_id=..., max_budget_usd=0.05)
response = await provider.complete(request)

# ✅ Agent response — always return, never raise
return AgentResponse(task_id=..., agent_id=..., status="success", result={...})

# ✅ Telemetry — every non-trivial operation
with tracer.start_as_current_span("operation.name"):
    ...
```

### Makefile targets

```bash
make up              # Start Docker dev stack
make down            # Stop Docker dev stack
make reset           # Wipe all volumes (full reset)
make dev             # Start FastAPI in hot-reload mode
make test-unit       # pytest unit tests
make test            # Full test suite
make lint            # Ruff check + format
make lint-fix        # Auto-fix lint issues
make typecheck       # mypy strict
make migrate         # Run all pending migrations
make migrate-new MSG="description"  # Create new Alembic migration
make proto           # Regenerate gRPC stubs from .proto
make shell-postgres  # psql into dev DB
make shell-neo4j     # cypher-shell into dev Neo4j
```

### Pre-commit hooks

```bash
make install-pre-commit   # Install hooks (runs on every commit)
```

Enforces: Ruff lint, Ruff format, mypy, no secrets, no direct commits to `main`.

---

## Phase Roadmap

| Phase | Duration | Status | Goal |
|-------|---------|--------|------|
| **Phase 0** — Bootstrap | 2 weeks | ✅ **Done** | All Docker services running; CI green |
| **Phase 1** — Foundation | 6 weeks | ✅ **Done** | E2E chat: input → Librarian → LLM → memory write |
| **Phase 2** — Integrations | 6 weeks | 🔄 **In Progress** | Gmail/GCal/Notion/GitHub MCPs + budget hardening |
| **Phase 3** — Agents + News | 8 weeks | ⏳ Planned | All 5 specialist agents + news digest pipeline |
| **Phase 4** — Desktop | 6 weeks | ⏳ Planned | macOS offline app (Tauri 2.0 + Ollama + gRPC sync) |
| **Phase 5** — Mobile | 8 weeks | ⏳ Planned | iOS (Foundation Models) + Android (ONNX) + OTA upgrade |
| **Phase 6** — Watch + Polish | 6 weeks | ⏳ Planned | Apple Watch + security hardening + load testing |

**DoD to exit Phase 1:** Message via web UI → Librarian fetches context → RouteLLM selects model → response streamed → memory delta written → telemetry in TimescaleDB.

**DoD to exit Phase 2:** Agent executes "summarise today's unread emails and add to Notion" end-to-end through Gmail + Notion MCPs, within token budget, with full HMAC audit trail.

---

## Key Design Decisions

### Why LangGraph (not raw Python)?
State graph gives us: composable nodes, conditional HitL interrupt points, persistent conversation state across turns, and streaming support — without custom plumbing.

### Why LiteLLM + RouteLLM together?
- **RouteLLM** classifies **query complexity** → routes cheap queries to smaller models
- **LiteLLM** provides **unified API**, **budget enforcement**, **fallback chains**, and **Redis caching** across all providers
- Combined: ~60% cost reduction vs always using the strong model

### Why Neo4j + Mem0 + Zep (three stores)?
Each covers a different memory horizon:
- **Mem0** — episodic: "what did we discuss last Tuesday?" (session-scoped facts)
- **Zep/Graphiti** — temporal: "who did the user work with in Q1?" (time-bounded relationships)
- **Neo4j** — semantic: "how does concept X relate to Y?" (long-term personal knowledge)

### Why per-agent ACL enforcement?
Financial Agent cannot read HealthRecord nodes. Health Agent cannot read Financial nodes. Principle of least privilege applied to the knowledge graph — prevents one compromised agent from exfiltrating unrelated sensitive data.

### Why Docker-isolated MCP servers?
MCP servers execute arbitrary integration code (OAuth flows, API calls, file access). Docker isolation means a compromised MCP server cannot access host filesystem, other containers, or the master LLM process.

---

## Environment Variables

Copy `.env.example` to `.env`. All variables are documented inline with descriptions.

**Required to start:**
- `APP_SECRET_KEY` — `openssl rand -hex 32`
- `POSTGRES_PASSWORD` — any strong password
- `NEO4J_PASSWORD` — any strong password
- `LITELLM_MASTER_KEY` — `openssl rand -hex 16`

**Required for cloud LLMs (at least one):**
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `GOOGLE_API_KEY`

**Optional (local-only mode):**
- Install [Ollama](https://ollama.ai) and set `OLLAMA_BASE_URL=http://localhost:11434`

---

## Contributing

1. Read [`AGENTS.md`](AGENTS.md) — every rule is non-negotiable
2. Branch from `develop` — direct pushes to `main` are blocked
3. Run `make lint && make typecheck && make test-unit` before opening a PR
4. Coverage must stay ≥ 80% branch coverage on all `master/` code
5. Every new env var must be added to `.env.example` + `master/core/config.py`
6. Every new DB column must be added via migration (never ALTER TABLE in app code)

---

## License

MIT — see [LICENSE](LICENSE).
