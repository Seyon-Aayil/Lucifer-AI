# Lucifer AI — Dev Stack Makefile
# Usage: make <target>

.PHONY: up down reset test lint typecheck migrate proto clean help

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
	python -m grpc_tools.protoc \
		-I infra/proto \
		--python_out=master/sync \
		--grpc_python_out=master/sync \
		infra/proto/lucifer_sync.proto
	@echo "$(CYAN)Stubs generated in master/sync/$(RESET)"

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
