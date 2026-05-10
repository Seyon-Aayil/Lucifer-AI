# Lucifer AI — Dev Stack Makefile
# Usage: make <target>

.PHONY: up down reset test lint typecheck migrate proto clean help edge-build edge-test edge-lint edge-cli

# ─── Colours ────────────────────────────────────────────────────────────────
CYAN  := \033[0;36m
RESET := \033[0m

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "$(CYAN)%-18s$(RESET) %s\n", $$1, $$2}'

# ─── Infrastructure ─────────────────────────────────────────────────────────
up: ## Start all Docker Compose services
	@echo "$(CYAN)Starting Lucifer dev stack...$(RESET)"
	docker compose -f infra/docker-compose.yml up -d
	@echo "$(CYAN)Waiting for services...$(RESET)"
	@sleep 5
	@docker compose -f infra/docker-compose.yml ps

down: ## Stop all Docker Compose services
	docker compose -f infra/docker-compose.yml down

reset: ## Stop and remove all volumes (full reset)
	docker compose -f infra/docker-compose.yml down -v --remove-orphans
	@echo "$(CYAN)All volumes removed.$(RESET)"

logs: ## Tail all service logs
	docker compose -f infra/docker-compose.yml logs -f

# ─── Database Migrations ────────────────────────────────────────────────────
migrate: ## Run all pending migrations (Postgres + Neo4j)
	@echo "$(CYAN)Running Postgres migrations...$(RESET)"
	cd master && python -m alembic upgrade head
	@echo "$(CYAN)Running Neo4j migrations...$(RESET)"
	./infra/neo4j/migrate.sh

migrate-new: ## Create a new Alembic migration (usage: make migrate-new MSG="description")
	cd master && python -m alembic revision --autogenerate -m "$(MSG)"

# ─── Code Generation ────────────────────────────────────────────────────────
proto: ## Generate gRPC stubs from proto files
	@echo "$(CYAN)Generating gRPC Python stubs...$(RESET)"
	@mkdir -p master/sync
	@touch master/sync/__init__.py
	python -m grpc_tools.protoc \
		-I infra/proto \
		--python_out=master/sync \
		--grpc_python_out=master/sync \
		infra/proto/lucifer_sync.proto
	@# Rewrite the absolute import in the generated _grpc.py to a package-relative one
	@sed -i.bak -E 's/^import (lucifer_sync_pb2) as (.*)$$/from . import \1 as \2/' \
		master/sync/lucifer_sync_pb2_grpc.py
	@rm -f master/sync/lucifer_sync_pb2_grpc.py.bak
	@echo "$(CYAN)Stubs generated in master/sync/$(RESET)"

proto-check: ## Verify generated proto stubs are up to date (CI)
	@echo "$(CYAN)Checking proto stubs are current...$(RESET)"
	@$(MAKE) proto >/dev/null
	@if ! git diff --quiet -- master/sync/lucifer_sync_pb2.py master/sync/lucifer_sync_pb2_grpc.py; then \
		echo "ERROR: Generated proto stubs are out of date. Run 'make proto' and commit." >&2; \
		git --no-pager diff -- master/sync/lucifer_sync_pb2.py master/sync/lucifer_sync_pb2_grpc.py >&2; \
		exit 1; \
	fi
	@echo "$(CYAN)Proto stubs current.$(RESET)"

dev-certs: ## Mint local CA + server + client certs for gRPC mTLS dev (infra/certs/)
	@echo "$(CYAN)Generating dev mTLS certificates in infra/certs/...$(RESET)"
	@mkdir -p infra/certs
	@cd infra/certs && \
		openssl genrsa -out ca.key 4096 >/dev/null 2>&1 && \
		openssl req -new -x509 -days 365 -key ca.key -out ca.crt -subj "/CN=lucifer-dev-ca" >/dev/null 2>&1 && \
		openssl genrsa -out server.key 4096 >/dev/null 2>&1 && \
		openssl req -new -key server.key -out server.csr -subj "/CN=localhost" >/dev/null 2>&1 && \
		openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
			-out server.crt -days 365 -extfile <(printf "subjectAltName=DNS:localhost,IP:127.0.0.1") >/dev/null 2>&1 && \
		openssl genrsa -out client.key 4096 >/dev/null 2>&1 && \
		openssl req -new -key client.key -out client.csr -subj "/CN=lucifer-dev-client" >/dev/null 2>&1 && \
		openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
			-out client.crt -days 365 >/dev/null 2>&1 && \
		rm -f *.csr *.srl
	@echo "$(CYAN)Dev certs ready: infra/certs/{ca,server,client}.{crt,key}$(RESET)"

# ─── Code Quality ───────────────────────────────────────────────────────────
lint: ## Run Ruff linter + formatter check
	ruff check master/
	ruff format --check master/

lint-fix: ## Auto-fix linting issues
	ruff check --fix master/
	ruff format master/

typecheck: ## Run mypy strict type checking
	mypy master/ --ignore-missing-imports

# ─── Testing ────────────────────────────────────────────────────────────────
test: ## Run full test suite (requires Docker services to be up)
	pytest master/tests/ -v

test-unit: ## Run only unit tests (no external services needed)
	pytest master/tests/unit/ -v -m "not integration"

test-integration: ## Run integration tests (requires up)
	pytest master/tests/integration/ -v -m "integration"

test-cov: ## Run tests with coverage HTML report
	pytest master/tests/ --cov=master --cov-report=html
	open htmlcov/index.html

# ─── Edge Client (Rust) ─────────────────────────────────────────────────────
edge-build: ## Build the Rust edge sync client (Phase 4b)
	cd edge && cargo build --workspace --release

edge-test: ## Test the Rust edge sync client
	cd edge && cargo test --workspace

edge-lint: ## Format + clippy the Rust edge workspace
	cd edge && cargo fmt --all -- --check
	cd edge && cargo clippy --workspace --all-targets -- -D warnings

edge-cli: ## Build the lucifer-edge CLI binary
	cd edge && cargo build --release -p lucifer-edge-cli
	@echo "Built: edge/target/release/lucifer-edge"

# ─── Install ─────────────────────────────────────────────────────────────────
install: ## Install Python deps in editable mode with dev extras
	pip install -e ".[dev]"

install-pre-commit: ## Install and activate pre-commit hooks
	pre-commit install

# ─── Utilities ───────────────────────────────────────────────────────────────
clean: ## Remove __pycache__, .mypy_cache, .ruff_cache, .pytest_cache
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	rm -rf htmlcov/ .coverage

dev: ## Start the master FastAPI server in hot-reload mode
	cd master && uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

shell-postgres: ## Open psql shell into the dev Postgres instance
	docker compose -f infra/docker-compose.yml exec postgres \
		psql -U lucifer -d lucifer

shell-neo4j: ## Open cypher-shell into the dev Neo4j instance
	docker compose -f infra/docker-compose.yml exec neo4j \
		cypher-shell -u neo4j -p $$(grep NEO4J_PASSWORD .env | cut -d= -f2)
