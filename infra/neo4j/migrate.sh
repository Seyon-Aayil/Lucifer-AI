#!/usr/bin/env bash
# =============================================================================
# Neo4j Migration Runner
# Applies all *.cypher files in migrations/ in lexicographic order.
# Tracks applied migrations in a :__Migration node to avoid re-runs.
# =============================================================================
set -euo pipefail

NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-changeme}"
MIGRATIONS_DIR="$(dirname "$0")/migrations"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

run_cypher() {
  local file="$1"
  cypher-shell \
    -a "$NEO4J_URI" \
    -u "$NEO4J_USER" \
    -p "$NEO4J_PASSWORD" \
    --file "$file" \
    --format plain
}

# Ensure migration tracking node exists
run_cypher <(echo "
  CREATE CONSTRAINT migration_name_unique IF NOT EXISTS
    FOR (m:__Migration) REQUIRE m.name IS UNIQUE;
") 2>/dev/null || true

# Apply each migration file in order
for migration_file in $(ls "$MIGRATIONS_DIR"/*.cypher | sort); do
  migration_name=$(basename "$migration_file")

  # Check if already applied
  applied=$(cypher-shell \
    -a "$NEO4J_URI" -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" \
    --format plain \
    "MATCH (m:__Migration {name: '$migration_name'}) RETURN count(m) AS cnt" \
    | tail -1 | tr -d ' ')

  if [[ "$applied" == "1" ]]; then
    log "SKIP  $migration_name (already applied)"
    continue
  fi

  log "APPLY $migration_name"
  run_cypher "$migration_file"

  # Mark as applied
  cypher-shell \
    -a "$NEO4J_URI" -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" \
    "CREATE (:__Migration {name: '$migration_name', appliedAt: datetime()})" \
    > /dev/null

  log "DONE  $migration_name"
done

log "All Neo4j migrations applied."
