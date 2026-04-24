// ── Initial Neo4j Schema Migration ──────────────────────────────────────────
// Migration 0001: Constraints and Indexes
// Run via: ./infra/neo4j/migrate.sh

// ── Uniqueness Constraints (also create backing index) ───────────────────────
CREATE CONSTRAINT node_id_unique IF NOT EXISTS
  FOR (n:Node) REQUIRE n.id IS UNIQUE;

CREATE CONSTRAINT person_id IF NOT EXISTS
  FOR (p:Person) REQUIRE p.id IS UNIQUE;

CREATE CONSTRAINT place_id IF NOT EXISTS
  FOR (p:Place) REQUIRE p.id IS UNIQUE;

CREATE CONSTRAINT event_id IF NOT EXISTS
  FOR (e:Event) REQUIRE e.id IS UNIQUE;

CREATE CONSTRAINT concept_id IF NOT EXISTS
  FOR (c:Concept) REQUIRE c.id IS UNIQUE;

CREATE CONSTRAINT artifact_id IF NOT EXISTS
  FOR (a:Artifact) REQUIRE a.id IS UNIQUE;

CREATE CONSTRAINT artifact_content_hash IF NOT EXISTS
  FOR (a:Artifact) REQUIRE a.contentHash IS UNIQUE;

CREATE CONSTRAINT health_record_id IF NOT EXISTS
  FOR (h:HealthRecord) REQUIRE h.id IS UNIQUE;

CREATE CONSTRAINT financial_id IF NOT EXISTS
  FOR (f:Financial) REQUIRE f.id IS UNIQUE;

CREATE CONSTRAINT task_id IF NOT EXISTS
  FOR (t:Task) REQUIRE t.id IS UNIQUE;

CREATE CONSTRAINT news_id IF NOT EXISTS
  FOR (n:News) REQUIRE n.id IS UNIQUE;

// ── Range Indexes (for temporal queries) ─────────────────────────────────────
CREATE INDEX node_created_at IF NOT EXISTS
  FOR (n:Node) ON (n.createdAt);

CREATE INDEX node_last_accessed IF NOT EXISTS
  FOR (n:Node) ON (n.lastAccessedAt);

CREATE INDEX node_decay_score IF NOT EXISTS
  FOR (n:Node) ON (n.decayScore);

CREATE INDEX node_classification IF NOT EXISTS
  FOR (n:Node) ON (n.classification);

CREATE INDEX task_due_date IF NOT EXISTS
  FOR (t:Task) ON (t.dueDate);

CREATE INDEX event_start_time IF NOT EXISTS
  FOR (e:Event) ON (e.startTime);

CREATE INDEX financial_date IF NOT EXISTS
  FOR (f:Financial) ON (f.date);

// ── Edge Indexes ─────────────────────────────────────────────────────────────
CREATE INDEX edge_valid_from IF NOT EXISTS
  FOR ()-[r:RELATES_TO]-() ON (r.validFrom);

CREATE INDEX edge_valid_until IF NOT EXISTS
  FOR ()-[r:RELATES_TO]-() ON (r.validUntil);

// ── Vector Index (Neo4j 5.x native vector index) ─────────────────────────────
// Requires Neo4j 5.11+ for native vector support
CREATE VECTOR INDEX node_embedding IF NOT EXISTS
  FOR (n:Node) ON (n.embeddingVector)
  OPTIONS {indexConfig: {
    `vector.dimensions`: 1536,
    `vector.similarity_function`: 'cosine'
  }};
