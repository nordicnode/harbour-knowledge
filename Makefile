SHELL := /bin/bash
.PHONY: db migrate api web seed reindex local-embeddings local-chat local-chat-install local-chat-pull local-chat-warm test check clean-db

# Read a dotenv value without leaking its value into make output. Strip an
# optional surrounding quote pair and an unquoted inline comment.
define env_value
$(shell sed -n 's/^[[:space:]]*$(1)[[:space:]]*=[[:space:]]*//p' .env 2>/dev/null | tail -n 1 | sed -e 's/[[:space:]]#.*$$//' -e 's/[[:space:]]*$$//' -e 's/^"\(.*\)"$$/\1/' -e "s/^'\(.*\)'$$/\1/" | tr -d '\r')
endef

# The local embedding stack is optional. Only ask uv for it when this workspace
# is configured for local embeddings; `UV_EXTRA=... make api` overrides this.
EMBEDDING_PROVIDER := $(strip $(call env_value,EMBEDDING_PROVIDER))
UV_EXTRA ?= $(if $(filter local,$(EMBEDDING_PROVIDER)),local-embeddings,)
UV_RUN := uv run $(if $(UV_EXTRA),--extra $(UV_EXTRA),)

db:
	@if docker inspect kb-postgres >/dev/null 2>&1; then \
		docker start kb-postgres >/dev/null 2>&1 || true; \
		echo "using existing kb-postgres container"; \
	else \
		docker compose up -d postgres; \
	fi
	@for i in $$(seq 1 60); do \
		if docker exec kb-postgres sh -c 'pg_isready -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"' >/dev/null 2>&1; then \
			echo "postgres ready"; \
			exit 0; \
		fi; \
		sleep 1; \
	done; \
	echo "postgres did not become ready within 60 seconds" >&2; \
	exit 1

# Track applied files so a re-run is a no-op. Older databases that predate the
# tracking table simply apply the idempotent migrations once more.
migrate:
	@set -euo pipefail; \
	read_env() { \
		local key="$$1" value; \
		value="$$(sed -n "s/^[[:space:]]*$${key}[[:space:]]*=[[:space:]]*//p" .env 2>/dev/null | tail -n 1 | tr -d '\r')"; \
		value="$${value%%[[:space:]]#*}"; \
		value="$${value#"$${value%%[![:space:]]*}"}"; \
		value="$${value%"$${value##*[![:space:]]}"}"; \
		if [ "$${#value}" -ge 2 ] && [[ "$$value" == \"* && "$$value" == *\" ]]; then \
			value="$${value:1:$${#value}-2}"; \
		fi; \
		if [ "$${#value}" -ge 2 ] && [[ "$$value" == \'* && "$$value" == *\' ]]; then \
			value="$${value:1:$${#value}-2}"; \
		fi; \
		printf '%s' "$$value"; \
	}; \
	url="$${MIGRATION_DATABASE_URL:-$$(read_env MIGRATION_DATABASE_URL)}"; \
	if [ -z "$$url" ]; then echo "MIGRATION_DATABASE_URL is missing from .env" >&2; exit 1; fi; \
	psql "$$url" -v ON_ERROR_STOP=1 -c "\
		create table if not exists schema_migrations (\
			version text primary key,\
			applied_at timestamptz not null default now()\
		);" >/dev/null; \
	for file in db/migrations/*.sql; do \
		version=$$(basename "$$file"); \
		applied=$$(psql "$$url" -At -v ON_ERROR_STOP=1 -c "select 1 from schema_migrations where version = '$$version'"); \
		if [ "$$applied" = "1" ]; then \
			echo "already applied: $$file"; \
			continue; \
		fi; \
		echo "applying $$file"; \
		psql "$$url" -v ON_ERROR_STOP=1 --single-transaction -f "$$file" \
			-c "insert into schema_migrations (version) values ('$$version')" >/dev/null; \
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
	cd services/api && $(UV_RUN) uvicorn app.main:app --reload --port 8000

web:
	cd apps/web && bun run dev

seed:
	cd services/api && $(UV_RUN) python -m app.seed

reindex:
	cd services/api && $(UV_RUN) python -m app.reindex

test:
	cd services/api && $(UV_RUN) pytest -q
	cd apps/web && bun run typecheck

check: test
	cd apps/web && bun run build

clean-db:
	docker rm -f kb-postgres >/dev/null 2>&1 || true
	docker volume rm kb-pgdata >/dev/null 2>&1 || true
