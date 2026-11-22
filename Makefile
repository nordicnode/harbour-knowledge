SHELL := /bin/bash
.PHONY: db migrate api web seed reindex local-embeddings local-chat local-chat-install local-chat-pull local-chat-warm test clean-db

db:
	docker compose up -d postgres
	@until docker exec kb-postgres pg_isready -U kb -d kb >/dev/null 2>&1; do sleep 1; done
	@echo "postgres ready on 127.0.0.1:55432"

migrate:
	set -a; source .env; set +a; \
	for file in db/migrations/*.sql; do \
		echo "applying $$file"; \
		psql "$$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f "$$file"; \
	done

local-embeddings:
	cd services/api && uv sync --extra local-embeddings

local-chat:
	./scripts/local-chat.sh serve

local-chat-install:
	./scripts/local-chat.sh install

local-chat-pull:
	./scripts/local-chat.sh pull

local-chat-warm:
	./scripts/local-chat.sh warm

api:
	cd services/api && uv run --extra local-embeddings uvicorn app.main:app --reload --port 8000

web:
	cd apps/web && bun run dev

seed:
	cd services/api && uv run --extra local-embeddings python -m app.seed

reindex:
	cd services/api && uv run --extra local-embeddings python -m app.reindex

test:
	cd services/api && uv run --extra local-embeddings pytest -q

clean-db:
	docker rm -f kb-postgres >/dev/null 2>&1 || true
	docker volume rm kb-pgdata >/dev/null 2>&1 || true
