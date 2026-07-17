# Lucifer AI — Detailed System Architecture

> **Version:** 0.6 (Phases 0–4b shipped; Phase 4c Gate 1 cleared)  
> **Last Updated:** 2026-07-17  
> **Status:** Phase 4c — mobile edge client in progress (iOS simulator launch ✅;
> Android release APK built, on-device launch pending). Phase 3 closed — agent
> swarm, news pipeline, and live web search are wired against real graph data.

---

## Table of Contents

1. [System Philosophy](#1-system-philosophy)
2. [High-Level Topology](#2-high-level-topology)
3. [LLM Integration & Routing Layer](#3-llm-integration--routing-layer)
4. [Agent Orchestration Framework](#4-agent-orchestration-framework)
5. [Memory & Knowledge Architecture](#5-memory--knowledge-architecture)
6. [Token Optimization Pipeline](#6-token-optimization-pipeline)
7. [News Sync Engine](#7-news-sync-engine)
8. [Edge Device Architecture](#8-edge-device-architecture)
9. [MCP Server Integration](#9-mcp-server-integration)
10. [Telemetry & Observability](#10-telemetry--observability)
11. [Data Classification & Security](#11-data-classification--security)
12. [Sync Protocol & Message Bus](#12-sync-protocol--message-bus)
13. [Model Upgrade System](#13-model-upgrade-system)
14. [Build Phase Roadmap](#14-build-phase-roadmap)
15. [Tech Stack Reference](#15-tech-stack-reference)

---

## 1. System Philosophy

Lucifer AI is a **distributed, privacy-first, self-evolving personal AI operating system**. It runs across a mesh of heterogeneous edge devices with a central master server, treating every request as a first-class opportunity to minimize cost, protect privacy, and maximize relevance.

### Core Tenets

| Tenet | Description |
|-------|-------------|
| **Privacy-by-default** | Sensitive data is classified at creation time and never leaves the device unencrypted. Health and financial data can be set to `local-only`. |
| **LLM-agnostic** | All provider interactions go through a unified adapter interface. Swapping from Claude to Gemini requires no upstream changes. |
| **Cost-aware at every call** | Every inference call has an explicit budget. RouteLLM routes cheap queries to small models. LiteLLM enforces hard caps. |
| **Agent autonomy + HitL** | Agents act autonomously for low-risk tasks. Human-in-the-loop (HitL) checkpoints are mandatory for actions above a configured risk tier. |
| **Offline-capable edge** | Every edge client degrades gracefully with no network. Queued actions sync deterministically on reconnect. |
| **Memory as infrastructure** | The Librarian Agent is the system's source of truth. No agent builds its own memory. Memory is a shared service with explicit access control. |

---

## 2. High-Level Topology

```
┌──────────────────────────────────────────────────────────────────────┐
│                         MASTER SERVER                                │
│                                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐   │
│  │  FastAPI      │  │  LangGraph   │  │   LiteLLM Proxy          │   │
│  │  Gateway      │  │  Orchestrator│  │   + RouteLLM Router      │   │
│  └──────┬───────┘  └──────┬───────┘  └────────────┬─────────────┘   │
│         │                 │                        │                 │
│  ┌──────▼─────────────────▼──────────────────────▼──────────────┐   │
│  │                     NATS JetStream Message Bus                 │   │
│  └──────┬────────────────────────────────────────────────────────┘   │
│         │                                                            │
│  ┌──────▼──────────────────────────────────────────────────────┐     │
│  │  Agent Pool                                                  │     │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐       │     │
│  │  │ Financial│ │  Health  │ │  Coding  │ │ Research │  ...  │     │
│  │  └──────────┘ └──────────┘ └──────────┘ └──────────┘       │     │
│  └─────────────────────────────────────────────────────────────┘     │
│                                                                      │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────────────────┐    │
│  │  Neo4j KG   │  │  Mem0 + Zep  │  │  MinIO Artifact Store    │    │
│  │  (Master)   │  │  (Memory)    │  │  + pgvector Index        │    │
│  └─────────────┘  └──────────────┘  └──────────────────────────┘    │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │  TimescaleDB (Telemetry) + Prometheus + Grafana              │     │
│  └─────────────────────────────────────────────────────────────┘     │
└──────────────────────────────── ▲ ▲ ▲ ───────────────────────────────┘
                                  │ │ │  gRPC over mTLS
          ┌───────────────────────┘ │ └───────────────────┐
          │                         │                     │
┌─────────▼──────────┐   ┌──────────▼─────────┐  ┌───────▼──────────┐
│   EDGE: DESKTOP    │   │   EDGE: MOBILE     │  │   EDGE: WATCH    │
│                    │   │                    │  │                  │
│  Tauri 2.0 App     │   │  iOS / Android     │  │  watchOS /       │
│  Ollama (GGUF)     │   │  Core ML + Apple   │  │  WearOS          │
│  MLX (Apple Si.)   │   │  Foundation Models │  │  Sensor Bus      │
│  Local MCP tools   │   │  ONNX Runtime      │  │  Relay → Mobile  │
│  SQLite + vec      │   │  SQLite + vec      │  │  Haptic + Voice  │
└────────────────────┘   └────────────────────┘  └──────────────────┘
```

### Network Segmentation

| Zone | Protocol | Auth | Notes |
|------|----------|------|-------|
| Edge → Master | gRPC over mTLS | Short-lived JWT + device fingerprint (TPM/Secure Enclave) | Bidirectional streaming |
| Agent → Agent | NATS JetStream (internal) | NATS token auth | within master only |
| Device → MCP servers | Streamable HTTP/SSE (remote) or stdio (local) | OAuth 2.0 / signed local IPC | per-server |
| Watch → Mobile | WatchConnectivity API | Paired device trust | Apple ecosystem |

---

## 3. LLM Integration & Routing Layer

### 3.1 Architecture Overview

```
User Request
     │
     ▼
┌─────────────┐     Cache Hit?      ┌──────────────┐
│  GPTCache   │ ──────────────────► │ Return Cache  │
│  (Semantic) │                     └──────────────┘
└──────┬──────┘
       │ Cache Miss
       ▼
┌─────────────────┐
│  RouteLLM       │  Classifies: simple / medium / complex
│  Controller     │  Routes to: weak / strong model
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────────┐
│              LiteLLM Proxy                   │
│                                              │
│  ┌──────────────┐   ┌─────────────────────┐ │
│  │ Budget Guard │   │  Fallback Chain      │ │
│  │ (per agent,  │   │  primary → fallback  │ │
│  │  per day)    │   │  → local model       │ │
│  └──────────────┘   └─────────────────────┘ │
│                                              │
│  Provider dispatch: Anthropic / OpenAI /     │
│  Google / vLLM (local) / Ollama             │
└─────────────────────────────────────────────┘
         │
         ▼
┌─────────────────┐
│  Response       │
│  Validator      │  Schema check + hallucination heuristics + safety filter
└─────────────────┘
         │
         ▼
    LLM Response → Agent → Surface
```

### 3.2 Provider Abstraction Interface

```typescript
/**
 * LLMProvider — universal adapter interface.
 * All implementations MUST satisfy this contract.
 * Enables hot-swap of providers without upstream changes.
 */
interface LLMProvider {
  id: string;                          // e.g. "anthropic-claude-opus-4"
  tier: "master" | "desktop" | "mobile";
  maxContextTokens: number;
  costPerInputToken: number;           // USD, for budget planning
  costPerOutputToken: number;
  supportsStreaming: boolean;
  supportsVision: boolean;
  supportsFunctionCalling: boolean;

  complete(req: CompletionRequest): Promise<CompletionResponse>;
  stream(req: CompletionRequest): AsyncIterable<StreamChunk>;
  countTokens(text: string): Promise<number>;
  healthCheck(): Promise<ProviderHealth>;
}

/**
 * ProviderRegistry — runtime registry with RouteLLM-aware selection.
 * RouteLLM makes the routing decision; LiteLLM proxy executes the call.
 */
class ProviderRegistry {
  private providers: Map<string, LLMProvider>;
  private routeLLMController: RouteLLMController;
  private liteLLMProxy: LiteLLMProxy;

  /**
   * Select provider via RouteLLM complexity score, then enforce via LiteLLM.
   * Falls back through chain on failure or budget exceed.
   */
  async select(opts: {
    query: string;
    tier: Tier;
    requiredCapabilities: Capability[];
    agentId: string;
    maxBudgetUSD: number;
  }): Promise<LLMProvider> { ... }

  /** Mark provider unhealthy; triggers failover to next in chain */
  async reportFailure(providerId: string, error: ProviderError): Promise<void> { ... }
}
```

### 3.3 Provider Matrix

| Provider | Models | Tier | Vision | Function Calling | Streaming | On-Device |
|----------|--------|------|--------|-----------------|-----------|-----------|
| Anthropic | Claude Opus/Sonnet/Haiku | master / desktop | ✓ | ✓ | ✓ | ✗ |
| OpenAI | GPT-4o / o1 / o3-mini | master / desktop | ✓ | ✓ | ✓ | ✗ |
| Google | Gemini Ultra/Pro/Flash | master / desktop | ✓ | ✓ | ✓ | ✗ |
| Ollama (local) | Llama 3.x, Mistral, Phi-4 | desktop | partial | partial | ✓ | ✓ |
| MLX (Apple Silicon) | Llama, Gemma, Qwen | desktop | ✗ | partial | ✓ | ✓ |
| Apple Foundation Models | ~3B on-device + PCC | iOS / macOS | ✓ | ✓ | ✓ | ✓ |
| Core ML / ONNX | Phi-4 mini, Gemma 2B | mobile | ✗ | ✗ | ✓ | ✓ |
| vLLM (self-hosted) | Llama 3.1 70B+ | master | ✓ | ✓ | ✓ | ✗ |

### 3.4 Request Lifecycle

| Step | Component | Detail |
|------|-----------|--------|
| 1 | PII Scanner | Regex + lightweight NER model. Names, emails, phone numbers, SSN patterns flagged before any external dispatch. |
| 2 | Content Policy | Safety classification. Re-prompts or blocks if threshold exceeded. |
| 3 | Semantic Cache | GPTCache checks embedding similarity against recent responses (threshold: 0.92). Returns cached result with 0 LLM cost. |
| 4 | RouteLLM Classify | Scores query complexity (0–1). Below threshold → weak (cheap) model. Above → strong model. |
| 5 | Budget Check | LiteLLM enforces per-agent daily budget. Hard-stops if exceeded. |
| 6 | Provider Select | Routes to healthiest provider in tier with capability match. |
| 7 | Context Compress | LLMLingua-2 compresses RAG/document sections. Provider prefix caching applied for static prompt sections. |
| 8 | Dispatch + Retry | Exponential backoff (3 attempts). Circuit breaker at 5 consecutive failures. |
| 9 | Stream Processing | SSE chunks parsed + forwarded. Partial content cached for disconnect resume. |
| 10 | Response Validate | Output schema check, hallucination heuristics, safety filter. Re-prompt if invalid (max 2 retries). |
| 11 | Cost Accounting | Token counts logged to TimescaleDB. OpenTelemetry span closed. |

---

## 4. Agent Orchestration Framework

### 4.1 Framework Role Assignment

```
┌─────────────────────────────────────────────────────────┐
│              LangGraph (Top-Level Orchestrator)          │
│                                                         │
│  Stateful directed graph. Every node = agent action.    │
│  HitL checkpoints at risk tier boundaries.              │
│  Full audit log of every state transition.              │
│                                                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │  CrewAI Sub-Graphs (Specialist Teams)             │  │
│  │                                                   │  │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────┐  │  │
│  │  │ Research   │  │ Financial  │  │  Health    │  │  │
│  │  │ Crew       │  │ Crew       │  │  Crew      │  │  │
│  │  └────────────┘  └────────────┘  └────────────┘  │  │
│  └───────────────────────────────────────────────────┘  │
│                                                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │  AutoGen Conversation (Code Execution Sandbox)    │  │
│  │                                                   │  │
│  │  Coding Agent ↔ Code Reviewer ↔ Test Runner      │  │
│  │  Isolated Docker execution environment            │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### 4.2 Agent Interface Contract

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncGenerator

@dataclass
class AgentRequest:
    """Immutable request payload passed to every agent."""
    task_id: str
    agent_id: str
    intent: str
    context_package: ContextPackage   # From Librarian Agent
    token_budget: TokenBudget
    risk_tier: RiskTier               # low / medium / high / critical
    surface: Surface                  # watch / mobile / desktop / master / cli
    trace_id: str                     # OpenTelemetry trace ID


@dataclass
class AgentResponse:
    """Structured response from any agent."""
    task_id: str
    agent_id: str
    status: Literal["success", "partial", "escalate", "error"]
    result: dict                      # Agent-specific payload
    artifacts: list[ArtifactRef]      # Any generated files/docs
    memory_deltas: list[MemoryDelta]  # Writes back to Librarian
    token_usage: TokenUsage
    escalation_reason: str | None     # If status == "escalate"


class BaseAgent(ABC):
    """
    Base class for all Lucifer agents.
    Enforces: memory isolation, token budget adherence, telemetry emission.
    """

    def __init__(self, agent_id: str, librarian: LibrarianClient,
                 llm_registry: ProviderRegistry, telemetry: TelemetryEmitter):
        self.agent_id = agent_id
        self._librarian = librarian
        self._llm = llm_registry
        self._telemetry = telemetry

    @abstractmethod
    async def execute(self, request: AgentRequest) -> AgentResponse:
        """Core agent logic. Must respect request.token_budget."""
        ...

    async def stream_execute(
        self, request: AgentRequest
    ) -> AsyncGenerator[AgentStreamChunk, None]:
        """Optional streaming variant for surfaces that support it."""
        raise NotImplementedError

    def _emit(self, event_type: str, metadata: dict) -> None:
        """Emit telemetry event. Always called; never swallowed."""
        self._telemetry.emit(TelemetryEvent(
            event_type=event_type,
            agent_id=self.agent_id,
            task_id=...,
            metadata=metadata
        ))
```

### 4.3 Conversation Flow

```
[User Input: device context + raw text/voice/image]
        │
        ▼
[Intent Classifier]  ← lightweight on-device model
  simple / complex / agent-task / escalation
        │
        ▼
[Context Injector]  ← Librarian.get_context_package(agent_type, task_type)
  Returns: relevant graph nodes + recent artifacts + prefs (token-budgeted)
        │
        ▼
[Token Budget Planner]
  Calculates: budget = min(tier_cap, daily_remaining, task_priority_factor)
  Selects: model tier (local / mid / master)
        │
        ▼
[Agent Router]  ← LangGraph orchestrator fans out to agent pool
  Watch/mobile: local inference if budget allows
  Complex: escalate to master
        │
        ▼
[Agent Execution]  ← CrewAI / AutoGen sub-tasks as needed
        │
        ▼
[Response Synthesizer]
  Merges outputs → formats for surface (card/markdown/voice/haptic)
        │
        ▼
[Memory Writer]  ← Librarian.apply_memory_deltas(deltas)
  Graph updated, artifact stored, telemetry emitted
```

---

## 5. Memory & Knowledge Architecture

### 5.1 Three-Layer Memory Model

```
┌──────────────────────────────────────────────────────────────────┐
│  Layer 1: EPISODIC MEMORY                                        │
│  Tool: Mem0 (self-hosted)                                        │
│  Purpose: What happened in past agent interactions               │
│  Store: Vector DB (qdrant) + KG overlay                         │
│  Scope: user_id + agent_id isolation (memory poisoning guard)   │
│  Decay: Automated cleanup on low-confidence stale entries        │
└──────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────┐
│  Layer 2: TEMPORAL KNOWLEDGE GRAPH                               │
│  Tool: Zep / Graphiti engine (self-hosted)                       │
│  Purpose: Facts that change over time ("what was true then")     │
│  Queries: "What was my mortgage rate in Jan 2025?"               │
│           "Who was my doctor before I moved?"                    │
│  Store: Temporal graph with validFrom / validUntil on all edges  │
└──────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────┐
│  Layer 3: PERSONAL KNOWLEDGE GRAPH                               │
│  Tool: Neo4j 5 (master) / SQLite + sqlite-vec (edge)            │
│  Purpose: Core personal ontology: people, places, events, goals  │
│  Access: Governed by Librarian Agent access control matrix       │
└──────────────────────────────────────────────────────────────────┘
```

### 5.2 Knowledge Graph Schema

```typescript
/**
 * Node types in Lucifer's personal knowledge graph.
 * Master: Neo4j 5.  Edge hot-subgraph: SQLite + sqlite-vec.
 * All sensitive nodes: AES-256-GCM encrypted at rest with user-derived key.
 */
enum NodeType {
  Person       = "Person",       // contacts, family, colleagues
  Place        = "Place",        // home, office, frequent locations
  Event        = "Event",        // meetings, milestones, memories
  Concept      = "Concept",      // topics of interest, skills, goals
  Artifact     = "Artifact",     // documents, code, reports, images
  HealthRecord = "HealthRecord", // vitals, conditions, medications
  Financial    = "Financial",    // accounts, transactions, goals
  Task         = "Task",         // todos, projects, deadlines
  News         = "News",         // consumed articles, topics, sources
}

interface GraphNode {
  id: string;                    // UUID v4
  type: NodeType;
  label: string;
  attributes: Record<string, unknown>;
  classification: "public" | "standard" | "restricted" | "secret";
  createdAt: Date;
  lastAccessedAt: Date;
  decayScore: number;            // 0–1, nightly update; drives pruning
  embeddingVector?: Float32Array; // 1536-dim, for semantic retrieval
  sourceAgent: string;
}

interface GraphEdge {
  id: string;
  fromNodeId: string;
  toNodeId: string;
  relation: string;              // "WORKS_WITH", "AUTHORED", "ABOUT"
  weight: number;                // 0–1
  validFrom: Date;
  validUntil?: Date;             // temporal: ephemeral or historical edges
  attributes: Record<string, unknown>;
}

/** Context package returned to requesting agents */
interface ContextPackage {
  requestingAgent: string;
  taskType: string;
  nodes: GraphNode[];            // filtered by agent's classification access
  edges: GraphEdge[];
  summary: string;               // LLM-generated 200-token summary
  tokenEstimate: number;
  generatedAt: Date;
  ttlSeconds: number;
}
```

### 5.3 Librarian Agent Access Control Matrix

| Agent | Person nodes | Health nodes | Financial nodes | Artifacts | Tasks | News |
|-------|-------------|-------------|----------------|-----------|-------|------|
| Financial Agent | read: name/org | ✗ | read+write | read: financial | read+write | read: financial topics |
| Health Agent | read: name | read+write | ✗ | read: health | read+write | read: health topics |
| Coding Agent | read: name/org | ✗ | ✗ | read+write: code | read+write | read: tech topics |
| Personal Agent | read+write | read | read: summary | read+write | read+write | read+write |
| Research Agent | read: name | ✗ | ✗ | read+write | read | read+write |
| News Agent | ✗ | ✗ | ✗ | write: articles | ✗ | read+write |
| Librarian (self) | full | full | full | full | full | full |

### 5.4 Edge Hot-Subgraph Sync

```
Master Neo4j
    │
    │  [Librarian: build hot-subgraph manifest]
    │  - nodes accessed in last 30d by this device
    │  - restricted nodes: local-only flag check
    │  - size target: ≤ 50MB per device
    ▼
gRPC sync payload (Protobuf, encrypted AES-256-GCM)
    │
    ▼
Edge SQLite + sqlite-vec
    │
    ├── nodes.db          ← adjacency + attributes
    ├── embeddings.db     ← 1536-dim float32 vectors via sqlite-vec
    └── sync_manifest.db  ← last sync ts, version vector, hash
```

---

## 6. Token Optimization Pipeline

### 6.1 Multi-Layer Optimization Stack

```
Incoming Request
       │
       ▼
Layer 1: SEMANTIC CACHE (GPTCache, Redis backend)
  └─ Cosine similarity check on query embedding
  └─ Hit threshold: 0.92
  └─ Return cached → skip all further layers (0 cost)
       │ Miss
       ▼
Layer 2: BUDGET ALLOCATION
  └─ Budget = f(task_priority, user_tier, daily_remaining, model_tier)
  └─ Hard cap enforced by LiteLLM
       │
       ▼
Layer 3: CONTEXT PROFILING
  └─ Each chunk scored: recency × relevance (BM25 + embedding cosine)
  └─ Score stored for compression decision
       │
       ▼
Layer 4: PROMPT COMPRESSION (LLMLingua-2)
  └─ Applied to: RAG document sections, old conversation turns
  └─ Target: 8–20x compression on document chunks, <3% accuracy drop
  └─ NOT applied to: system prompt (cached), current query
       │
       ▼
Layer 5: HIERARCHICAL TRIM
  └─ Stage A: Drop items with score < 0.2
  └─ Stage B: Summarise items with score 0.2–0.5
  └─ Stage C: Extract key facts only from score < 0.7 docs
       │
       ▼
Layer 6: PROVIDER PREFIX CACHING
  └─ Static system prompt: cached via Anthropic prompt caching / OpenAI prefix caching
  └─ ~90% cost reduction on ~40% of every prompt's tokens
       │
       ▼
Layer 7: SEMANTIC DEDUPLICATION
  └─ SimHash for bulk near-duplicate detection in context chunks
  └─ Merge or drop redundant entries
       │
       ▼
Layer 8: DISPATCH (via LiteLLM)
       │
       ▼
Layer 9: POST-CALL CALIBRATION
  └─ Actual vs estimated token counts → feeds future estimate model
  └─ Per-provider, per-model calibration table updated
```

### 6.2 Token Budget Tiers

| Task Type | Input Budget | Output Budget | Compression Allowed | Escalation Trigger |
|-----------|-------------|--------------|---------------------|-------------------|
| Watch quick action | ≤ 512 | ≤ 128 | Aggressive | Always escalate to mobile |
| Mobile quick reply | ≤ 2K | ≤ 512 | Moderate | If > 1.5K after compress |
| Desktop task | ≤ 8K | ≤ 2K | Light | If > 6K after compress |
| Agent sub-task | ≤ 16K | ≤ 4K | Light | If > 12K after compress |
| Master complex | ≤ 128K | ≤ 8K | None | Manual review |
| Batch / research | ≤ 200K | ≤ 16K | None | Async job queue |

---

## 7. News Sync Engine

### 7.1 Ingestion Pipeline

| Step | Tool | Detail |
|------|------|--------|
| Source Registry | User config + auto-suggest from Librarian | RSS, newsletters, Reddit (PRAW), arXiv (arxiv-py), GitHub trending, SEC EDGAR, Yahoo Finance RSS |
| Scheduled Fetch | aiohttp + feedparser | Async, rate-limited. ETags + Last-Modified for bandwidth efficiency. Per-source cadence: 15min–24h. |
| Article Extraction | **Trafilatura** (primary) + PyMuPDF (PDFs) | Full-text extraction. Trafilatura outperforms Readability for precision. PyMuPDF + marker for research paper layout. |
| Deduplication | **SimHash** (bulk) + URL hash | Near-duplicate detection before scoring. Content hash for exact match. |
| Relevance Scoring | BM25 + embedding cosine vs Librarian interest vector | Scored against: active projects, financial holdings, known people, topic preferences. |
| Clustering | **HDBSCAN** | Better than DBSCAN for variable cluster sizes. Same-event articles grouped; best source kept per cluster. |
| Digest Compilation | Librarian Agent | Top 5–10 stories per category. Briefing vs deep-dive separation. |
| Delivery + Feedback | Push to surface at preferred time | Read/skip/save signals update Librarian interest model. |

### 7.2 Feed Categories

| Category | Default Sources | Agent Consumer | Refresh Cadence |
|----------|----------------|----------------|-----------------|
| Tech & AI | arXiv, HN, TechCrunch, GitHub trending | Research, Coding | 1h |
| Financial | Yahoo Finance, Bloomberg RSS, SEC EDGAR | Financial | 15min (market hours) |
| Health & Science | PubMed, Nature, WHO, NIH | Health | Daily |
| Personal interests | User-defined RSS, Reddit, newsletters | Personal | 2h |
| World news | AP, Reuters, BBC RSS | Personal | 1h |
| Industry-specific | Auto-detected from job/projects in Librarian | Coding/Research | 4h |

---

## 8. Edge Device Architecture

### 8.1 macOS / Windows Desktop

- **App Framework**: Tauri 2.0 (Rust core + WebView frontend)
  - Sidecar: bundles Ollama binary + Python FastAPI core — no separate install needed for users
  - Mobile eval: Tauri 2.0 targets iOS/Android from the same Rust backend; evaluate before committing to React Native
- **Local Inference**: Ollama (GGUF, model management CLI) + MLX (Apple Silicon optimized)
- **Local Storage**: SQLite + sqlite-vec (replaces Kuzu which was archived Oct 2025)
- **Offline Queue**: SQLite WAL. Max 10K queued actions. Deterministic replay on reconnect.
- **OS Integrations**:

| Feature | macOS Technology | Windows Technology |
|---------|-----------------|-------------------|
| Hotkey overlay | SwiftUI system panel or Tauri global shortcut | WPF / Win32 global hotkey |
| Active app context | Accessibility API (AXUIElement) | UIAutomation API |
| File system ops | Security-scoped bookmarks (sandboxed) | Win32 file dialogs + UAC |
| Notifications | UNUserNotificationCenter | Windows Notification Platform |
| Clipboard watcher | NSPasteboard observer (opt-in) | Clipboard Viewer Chain (opt-in) |
| Menu bar / Tray | NSStatusItem | System Tray Icon |

### 8.2 iOS / Android Mobile

- **iOS Runtime**: Core ML + **Apple Foundation Models Framework** (WWDC 2025)
  - ~3B on-device parameter model with LoRA adapter support
  - Heavy tasks: offloaded to Apple Private Cloud Compute (PCC) — stateless, encrypted
  - Fallback: Lucifer master via gRPC if PCC is insufficient
- **Android Runtime**: ONNX Runtime + MediaPipe LLM (INT4 GGUF ≤ 400 MB)
- **Storage**: SQLite + sqlite-vec (≤ 500 MB model + ≤ 200 MB graph cache)
- **Model Format**: Core ML `.mlpackage` (iOS) / INT4 GGUF (Android)
- **OTA Upgrade**: Play Asset Delivery (Android) / App update + OTA delta (iOS)

| Feature | iOS | Android |
|---------|-----|---------|
| Voice assistant | App Intents + SiriKit handoff | Google Assistant Actions |
| Share extension | Share Extension (any app → Lucifer) | Android Intent Filters |
| Home screen widgets | WidgetKit | Jetpack Glance |
| Background model | BackgroundTasks framework | WorkManager |
| Push relay | APNs | FCM |

### 8.3 Apple Watch / WearOS

- **No local model** — all inference offloaded to paired iPhone via WatchConnectivity
  - Apple Intelligence on watchOS 26+ offloads to iPhone Neural Engine
- **Sensor Bus**: HR, HRV, SpO2, activity, noise, location-tier — sampled every 30s, compressed, synced every 5min
- **Intent Capture**: Whisper.cpp tiny model for local transcription of short voice commands
- **Action Queue**: All actions queued to SQLite if phone unavailable; replayed on reconnect
- **Threshold Rules** (no LLM needed):
  - HR > 120 at rest → push notification
  - Sleep < 5h → morning nudge
  - Irregular rhythm detected → escalate to Health Agent

### 8.4 Edge Device Model Management

| Platform | Runtime | Model Format | Storage Budget | Auto-Upgrade | Rollback |
|----------|---------|-------------|---------------|-------------|---------|
| macOS (Apple Silicon) | MLX / Ollama | GGUF / MLX weights | ≤ 8 GB | ✓ WiFi + charging | Symlink swap; old weights 7d |
| Windows | ONNX Runtime / Ollama | GGUF / ONNX | ≤ 6 GB | ✓ Background service | Version registry, rollback cmd |
| iOS | Core ML + Foundation Models | `.mlpackage` + LoRA | ≤ 500 MB | ✓ App update + OTA delta | App Store rollback |
| Android | ONNX / MediaPipe LLM | INT4 GGUF | ≤ 400 MB | ✓ Play Asset Delivery | Asset versioning, delta patch |
| watchOS | No model | N/A | N/A | N/A | N/A — relays to iPhone |

---

## 9. MCP Server Integration

### 9.1 Security Architecture

Per MCP security best practices (2025):

1. **Transport**: `stdio` for local short-lived servers (Mac Task Executor, Web Scraper, Chrome Extension bridge). `Streamable HTTP/SSE` for persistent, remote servers (Notion, Gmail, GitHub).
2. **Isolation**: All remote/third-party MCP servers run in **Docker containers** with no host volume mounts. Capability-scoped networking only.
3. **Validation**: Every tool input schema-validated before execution. Reject unexpected fields.
4. **Manifest enforcement**: Agent capability manifests (`agent_manifest.json`) declare permitted MCP tools. Runtime enforces — cross-capability calls raise `PermissionDenied`.
5. **Audit**: Every MCP tool invocation appended to the HMAC-chained audit log.

### 9.2 Integration Map

| Integration | Transport | MCP Server | Agents | Capabilities | Auth |
|-------------|-----------|------------|--------|-------------|------|
| Notion | HTTP/SSE | mcp.notion.com | Librarian, Personal, Research | Read/write pages, DBs, search | OAuth 2.0 |
| Gmail | HTTP/SSE | googleapis.com/gmail-mcp | Personal, Financial | Read, compose, label, search | OAuth 2.0 |
| Google Calendar | HTTP/SSE | googleapis.com/calendar-mcp | Personal, Health | Events CRUD, scheduling | OAuth 2.0 |
| Google Drive | HTTP/SSE | googleapis.com/drive-mcp | Librarian, Coding | File CRUD, search, share | OAuth 2.0 |
| GitHub | HTTP/SSE | github.com/mcp | Coding | PRs, issues, code, CI | GitHub App / PAT |
| Slack | HTTP/SSE | slack.com/mcp | Personal | Read/send messages, channels | OAuth 2.0 |
| Linear / Jira | HTTP/SSE | linear.app/mcp | Coding, Personal | Issues, projects, sprints | OAuth 2.0 |
| Obsidian | stdio | local MCP bridge | Librarian, Research | Vault read/write, search | Local socket |
| Chrome Extension | stdio | local MCP bridge | Research, Coding | Active tab, bookmarks, history | Local socket |
| Mac Task Executor | stdio | local daemon | All agents | Shell, AppleScript, Shortcuts | Signed local IPC |
| Web Scraper | stdio | local MCP | Research, News | Fetch, render JS, extract | Local process |
| HomeKit | stdio | local HomeKit bridge | Personal | Device state, scenes, automations | Local network |

---

## 10. Telemetry & Observability

### 10.1 Stack

```
[Every Agent / LLM Call / Sync Event]
         │
         │ OpenTelemetry SDK (Python otel-sdk)
         ▼
┌────────────────────────────────────────────────────────┐
│              OpenTelemetry Collector                    │
│                                                        │
│  Traces → Jaeger (distributed tracing)                 │
│  Metrics → Prometheus → Grafana dashboards             │
│  Logs → Loki → Grafana Loki panels                     │
└───────────────────────────────┬────────────────────────┘
                                │
                                ▼
                    TimescaleDB (time-series)
                    ├── Raw events (90d retention)
                    └── Materialized aggregates (2yr)
```

### 10.2 Telemetry Event Schema

```python
@dataclass
class TelemetryEvent:
    """
    Structured event emitted by every agent action, LLM call,
    sync operation, and benchmark result.
    Schema is append-only; new fields added via migration only.
    """
    event_type: str               # "llm.complete" | "agent.execute" | "sync.push"
    timestamp: datetime           # UTC, microsecond precision
    device_id: str                # hashed device identifier
    agent_id: str | None
    trace_id: str                 # OpenTelemetry W3C trace ID
    span_id: str
    metrics: dict[str, float]     # {"latency_ms": 234, "input_tokens": 1200, ...}
    metadata: dict[str, str]      # {"provider": "anthropic", "model": "claude-opus-4"}
    cost_usd: float | None        # None if local model
    error: bool
    error_code: str | None
```

### 10.3 Dashboard Sections

| Dashboard | Key Metrics |
|-----------|------------|
| Cost & Token | Daily spend by provider/agent, cost per task type, projected month-end, budget burn rate |
| Performance | P50/P95/P99 latency per agent+provider, error taxonomy, circuit breaker states |
| Model Health | Benchmark scores (30d trend), upgrade history, shadow eval results, rollback events |
| Agent Activity | Requests/hr by agent, escalation rate, avg task complexity, top intents |
| Memory Graph | Node count by type, edge density, growth rate, decay distribution, sync lag |
| News & Briefing | Source health, relevance ratio, digest open rate, topic trends |
| Privacy & Security | PII scrub count, failed auth attempts, data classification breakdown, audit log |
| Edge Device Health | Per-device: battery at sync, model loaded, last heartbeat, queue depth, app version |

---

## 11. Data Classification & Security

### 11.1 Classification Tiers

| Class | Examples | Encryption | Sync to Edge | Sent to LLM | Retention |
|-------|---------|-----------|-------------|------------|---------|
| Public | News digests, general Q&A | Transit only | ✓ plaintext | ✓ | 90d |
| Standard | Tasks, calendar, preferences | At-rest AES-256 | ✓ encrypted | ✓ with consent | 2yr |
| Restricted | Health records, private notes | At-rest + field-level | Hot-subgraph only | Local model only | User-defined |
| Secret | Financial accounts, passwords | At-rest + field-level + key isolation | Never | Never | User-defined |

### 11.2 Security Threat Model

| Threat | Surface | Mitigation |
|--------|---------|-----------|
| Memory Injection Attack | Agent episodic memory | Mem0 memory isolation by `user_id` + `agent_id`; automated decay; confidence threshold |
| Prompt Injection via tools | MCP tool input | Input sanitization layer before every tool invocation; schema validation |
| MCP server compromise | Remote servers | Docker isolation; signed tool manifests; capability whitelist enforcement |
| PII exfiltration to LLM | Outbound requests | Local NER PII scanner; user confirmation gate before any external dispatch |
| Budget exhaustion (agent loop) | LLM calls | LiteLLM hard caps per agent/day; RouteLLM call rate limiter |
| Hallucination cascade | Agent→agent trust | Output schema validation; hallucination heuristics; re-prompt max 2x |
| Sync payload interception | gRPC channel | mTLS + AES-256-GCM payload encryption; device key rotation |
| Unauthorized device | Device auth | Short-lived JWT (1h TTL) + device fingerprint; revocation propagates <60s |
| Tampered audit log | Audit trail | Append-only HMAC chain; any break detected on read |
| Data residency violation | Egress | API gateway egress filter; per-data-class allowed origins enforced at runtime |

### 11.3 Key Derivation & Management

```
User Master Password
       │
       │  Argon2id (never transmitted)
       ▼
User Root Key (device-local, Secure Enclave / TPM-backed)
       │
       ├── Memory Encryption Key (AES-256-GCM, per-node for Secret class)
       ├── Sync Payload Key (rotated per sync session)
       └── Artifact Encryption Key (content-addressed)

Master server holds: encrypted blobs only. Zero key knowledge.
```

---

## 12. Sync Protocol & Message Bus

### 12.1 gRPC Sync Protocol

```protobuf
syntax = "proto3";

service LuciferSync {
  // Bidirectional streaming sync: edge pushes deltas, master resolves conflicts
  rpc SyncStream(stream SyncMessage) returns (stream SyncMessage);
  
  // Pull hot-subgraph manifest for device
  rpc GetHotSubgraph(SubgraphRequest) returns (SubgraphResponse);
  
  // Push telemetry batch (fire-and-forget, at-least-once)
  rpc PushTelemetry(TelemetryBatch) returns (PushAck);
}

message SyncMessage {
  string device_id = 1;
  string sync_session_id = 2;
  repeated NodeDelta node_deltas = 3;
  repeated EdgeDelta edge_deltas = 4;
  repeated ArtifactRef new_artifacts = 5;
  bytes payload_hmac = 6;           // AES-GCM auth tag
  int64 vector_clock = 7;           // for conflict detection
}
```

### 12.2 NATS JetStream Internal Bus

NATS is chosen for the master-internal message bus. Single binary (~15MB), sub-ms latency, no external dependencies.

| Subject | Publisher | Consumers | Delivery |
|---------|-----------|-----------|---------|
| `agent.task.>` | Orchestrator | Agent workers | Work queue (at-least-once) |
| `memory.delta.>` | All agents | Librarian | Work queue |
| `telemetry.event.>` | All components | TimescaleDB sink | Pub/sub |
| `news.fetch.>` | Scheduler | News fetch workers | Work queue |
| `sync.edge.>` | gRPC bridge | Sync workers | Pub/sub |
| `alert.>` | Monitoring | Alert dispatcher | Pub/sub |

### 12.3 Conflict Resolution

Vector clocks used for last-write-wins with agent-type priority:

```
Priority (descending): Librarian > HealthAgent > FinancialAgent > Others
If same agent, same field: last timestamp wins.
Conflict log: every resolved conflict appended to audit trail.
```

---

## 13. Model Upgrade System

### 13.1 Benchmark & Upgrade Pipeline

| Step | Trigger | Detail |
|------|---------|--------|
| Scheduled Benchmark | Master: nightly 2am. Edge: weekly on charge+WiFi | Suite: MMLU-subset, HumanEval-mini, custom domain tasks from last 30d usage history |
| Performance Scoring | Post-benchmark | Scored on: accuracy, p50/p95 latency, memory footprint, battery draw (mobile), cost-efficiency index. Weighted by user's actual usage pattern. |
| Regression Detection | Post-scoring | >5% regression on primary metric → alert. >15% → immediate rollback flag. Baseline: 30-day rolling average. |
| Candidate Discovery | Weekly | Queries: HuggingFace Hub API, Ollama hub, provider release RSS feeds. Filters by device constraints (VRAM, storage, quantization). |
| Shadow Evaluation | On candidate discovery | New model runs against last 100 real queries (cached responses). Zero user impact. Metrics collected. |
| Promotion Decision | Post-shadow | Auto-promote if shadow score > current by >8%. Human confirmation required if gain 3–8% or model >1GB larger. |
| OTA Rollout | Post-promotion | Canary: 10% of requests for 24h. Full rollout if error rate stable. Model binary streamed in background, hot-swapped on idle. |

---

## 14. Build Phase Roadmap

### Phase 0 — Infrastructure Bootstrap (2 weeks)
- Docker Compose dev stack: Postgres 16 + pgvector + TimescaleDB + Redis + NATS JetStream
- LiteLLM Proxy + RouteLLM setup with initial routing thresholds
- Mem0 self-hosted deployment
- OpenTelemetry Collector + Prometheus + Grafana baseline
- CI/CD pipeline (GitHub Actions): lint, test, type-check

### Phase 1 — Foundation (6 weeks)
- FastAPI gateway (async, OpenAPI schema)
- LangGraph orchestrator + BaseAgent interface
- Librarian Agent: Neo4j schema, CRUD, context package generation
- LLM Provider abstraction + RouteLLM + LiteLLM integration
- Token Optimizer: GPTCache + LLMLingua-2 pipeline
- Basic chat UI (web)

### Phase 2 — Integrations (6 weeks)
- MCP server registry + Docker isolation framework
- Integrations: Gmail, Google Calendar, Notion, GitHub (Phase 1 of table)
- Prompt caching (Anthropic + OpenAI prefix caching)
- Full token budget management with hard caps

### Phase 3 — Agents + News (8 weeks)
- All 5 agents: Financial, Coding (with AutoGen sandbox), Personal, Health, Research
- News Sync Engine: feedparser + Trafilatura + HDBSCAN + Librarian digest
- TimescaleDB telemetry pipeline + Grafana dashboards v1
- CrewAI sub-graphs for Research + Financial crews

### Phase 4 — Edge Desktop (6 weeks)
- Tauri 2.0 desktop app (macOS + Windows)
- Ollama sidecar bundling + MLX for Apple Silicon
- SQLite + sqlite-vec edge graph (replaces Kuzu)
- Sync protocol (gRPC + mTLS)
- Hotkey overlay, menu bar, offline queue
- Tauri 2.0 mobile evaluation (go/no-go for React Native decision)

### Phase 5 — Mobile (8 weeks)
- iOS: Core ML + Apple Foundation Models Framework + App Intents
- Android: ONNX Runtime + MediaPipe LLM
- Model upgrade system: OTA pipeline
- Widgets, Share Extension, Dynamic Island
- Benchmark runner per platform

### Phase 6 — Watch + Polish (6 weeks)
- watchOS: Sensor bus, intent capture, WatchConnectivity relay
- Full Grafana dashboard suite
- Security hardening: audit log HMAC chain, egress filter, PII scanner v2
- Performance tuning: latency p95 < 2s for desktop tasks
- Load testing: concurrent agent simulations

---

## 15. Tech Stack Reference

| Layer | Technology | Version | Rationale |
|-------|-----------|---------|-----------|
| Master API | Python + FastAPI + asyncio | 3.12 / 0.110+ | Rich AI/ML ecosystem, async-native |
| Agent Orchestrator | LangGraph | 0.2+ | Deterministic state machine, HitL, audit log |
| Agent Teams | CrewAI | 0.70+ | Role-based specialist crews; fast to define |
| Code Sandbox | AutoGen | 0.3+ | Multi-agent code execution, Docker sandbox |
| LLM Routing | RouteLLM (LMSYS) | latest | Complexity-based model routing; 60–85% cost reduction |
| LLM Gateway | LiteLLM Proxy | latest | 100+ providers, budget caps, fallback chains |
| Episodic Memory | Mem0 (self-hosted) | latest | Dual-store, memory poisoning guards, SOC2-ready |
| Temporal Memory | Zep / Graphiti | latest | Time-aware facts; "what was true then" queries |
| Master Graph DB | Neo4j 5 Community | 5.x | Mature, Cypher, excellent Python client |
| Edge Graph Store | SQLite + sqlite-vec | 3.45+ / latest | Zero-dep, proven, replaces archived Kuzu |
| Vector Index (Master) | pgvector | 0.7+ | Postgres-native; ties into TimescaleDB |
| Prompt Compression | LLMLingua-2 (Microsoft) | latest | 20x compression, <3% accuracy loss |
| Semantic Cache | GPTCache (Redis) | latest | 0-cost responses for semantically similar queries |
| Time-series / Telemetry | TimescaleDB | 2.x | Postgres extension; SQL; excellent compression |
| OTel Stack | OpenTelemetry + Prometheus + Grafana + Loki | latest | Industry standard observability |
| Message Bus | NATS JetStream | 2.x | Single binary, sub-ms, exactly-once, embeddable |
| Artifact Storage | MinIO (master) / SQLite blob (edge) | latest | S3-compatible, self-hosted |
| Sync Protocol | gRPC + Protobuf over mTLS | latest | Typed, streaming, bidirectional |
| Desktop App | Tauri 2.0 (Rust + WebView) | 2.x | Native perf, small binary, iOS/Android eval |
| Desktop Inference | Ollama (GGUF) + MLX (Apple Si.) | latest | Best local DX; MLX for Apple Silicon efficiency |
| Master Inference | vLLM (self-hosted) | latest | PagedAttention; production-grade throughput |
| iOS Runtime | Core ML + Apple Foundation Models Framework | iOS 18+ | WWDC 2025; 3B on-device, LoRA, PCC offload |
| Android Runtime | ONNX Runtime + MediaPipe LLM | latest | Proven; INT4 quantization for mobile |
| Watch | SwiftUI + WatchConnectivity | watchOS 11+ | Only native gives Watch API access; relay to iPhone |
| Article Extraction | Trafilatura + PyMuPDF + marker | latest | Outperforms Readability; PDF layout-aware |
| News Clustering | HDBSCAN | latest | Variable cluster sizes; no cluster-count param |
| Duplicate Detection | SimHash | latest | Bulk near-duplicate; faster than cosine at scale |
| CI/CD | GitHub Actions + Ruff + mypy + pytest | latest | Standard Python toolchain |
