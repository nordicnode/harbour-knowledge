-- Knowledge base — Phase 1 schema
-- Postgres 16+, pgvector. Every tenant-owned table carries tenant_id and is
-- protected by row-level security keyed to app.tenant_id (set per transaction).

create extension if not exists vector;
create extension if not exists pgcrypto;

-- ---------------------------------------------------------------------------
-- Tenancy and people
-- ---------------------------------------------------------------------------

create table if not exists tenants (
  id          uuid primary key default gen_random_uuid(),
  slug        text not null unique,
  name        text not null,
  settings    jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create table if not exists users (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  email       text not null,
  name        text not null,
  role        text not null check (role in ('admin','editor','agent')),
  title       text not null default 'Team member',
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  unique (tenant_id, email)
);

-- ---------------------------------------------------------------------------
-- Sources and immutable document versions
-- ---------------------------------------------------------------------------

create table if not exists sources (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  kind          text not null check (kind in
                  ('pdf','docx','pptx','xlsx','html','epub','text','markdown','image','chat_export','ticket_export','url')),
  title         text not null,
  origin_url    text,
  storage_path  text,
  mime_type     text,
  size_bytes    bigint,
  hash_sha256   text not null,
  source_tier   text not null default 'official_doc'
                  check (source_tier in ('system_of_record','official_doc','wiki','chat_export')),
  visibility    text not null default 'internal' check (visibility in ('internal','public')),
  status        text not null default 'processing'
                  check (status in ('processing','active','quarantined','failed','withdrawn')),
  alias_of_id   uuid references sources(id),
  metadata      jsonb not null default '{}'::jsonb,
  ingested_at   timestamptz,
  deleted_at    timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create index if not exists sources_tenant_hash_idx on sources (tenant_id, hash_sha256);
create index if not exists sources_tenant_status_idx on sources (tenant_id, status);

create table if not exists documents (
  id                     uuid primary key default gen_random_uuid(),
  tenant_id              uuid not null references tenants(id) on delete cascade,
  source_id              uuid not null references sources(id) on delete cascade,
  version                integer not null,
  effective_from         date,
  effective_to           date,
  supersedes_id          uuid references documents(id),
  status                 text not null default 'draft'
                           check (status in ('draft','current','superseded','withdrawn')),
  plan_scope             text[] not null default '{}',
  product_version_scope  text[] not null default '{}',
  language               text not null default 'en',
  title                  text not null,
  content_md             text not null,
  content_hash           text not null,
  doc_type               text not null default 'faq'
                           check (doc_type in ('policy','pricing','procedure','faq','known_issue','bug_workaround',
                                               'troubleshooting','product_description','internal_note')),
  parser                 text not null default 'markdown',
  parse_quality          numeric(4,3),
  metadata               jsonb not null default '{}'::jsonb,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now(),
  unique (source_id, version)
);

create index if not exists documents_source_current_idx on documents (source_id, status);
create index if not exists documents_tenant_status_idx on documents (tenant_id, status);

create table if not exists passages (
  id                    uuid primary key default gen_random_uuid(),
  tenant_id             uuid not null references tenants(id) on delete cascade,
  document_id           uuid not null references documents(id) on delete cascade,
  document_version      integer not null,
  parent_id             uuid references passages(id),
  level                 text not null default 'chunk' check (level in ('section','chunk')),
  text                  text not null,
  text_hash             text not null,
  chunk_index           integer not null default 0,
  section_path          text,
  page_ref              text,
  contextual_blurb      text not null default '',
  embedding             vector(1536),
  embedding_model       text,
  embedding_version     text,
  bm25_text             tsvector,
  ocr_confidence        numeric(4,3),
  token_count           integer not null default 0,
  tags                  text[] not null default '{}',
  dup_cluster_id        uuid,
  dup_similarity        numeric(4,3),
  canonical_passage_id  uuid references passages(id),
  status                text not null default 'current' check (status in ('current','superseded','tombstoned')),
  created_at            timestamptz not null default now()
);

create index if not exists passages_document_idx on passages (document_id, level, chunk_index);
create index if not exists passages_parent_idx on passages (parent_id);
create index if not exists passages_tenant_status_idx on passages (tenant_id, status);
create index if not exists passages_embedding_hnsw on passages using hnsw (embedding vector_cosine_ops)
  where status = 'current' and level = 'chunk';
create index if not exists passages_bm25_gin on passages using gin (bm25_text)
  where status = 'current' and level = 'chunk';

-- ---------------------------------------------------------------------------
-- Organisation
-- ---------------------------------------------------------------------------

create table if not exists topics (
  id                    uuid primary key default gen_random_uuid(),
  tenant_id             uuid not null references tenants(id) on delete cascade,
  parent_id             uuid references topics(id),
  slug                  text not null,
  name                  text not null,
  description           text not null default '',
  owner_id              uuid references users(id),
  review_interval_days  integer not null default 180,
  taxonomy_version      integer not null default 1,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  unique (tenant_id, slug)
);

-- ---------------------------------------------------------------------------
-- Canonical answers: the approved source of truth
-- ---------------------------------------------------------------------------

create table if not exists canonical_answers (
  id                    uuid primary key default gen_random_uuid(),
  tenant_id             uuid not null references tenants(id) on delete cascade,
  topic_id              uuid references topics(id),
  question              text not null,
  answer_md             text not null default '',
  status                text not null default 'draft'
                          check (status in ('draft','in_review','approved','needs_recheck','archived')),
  owner_id              uuid references users(id),
  review_interval_days  integer not null default 180,
  next_review_at        timestamptz,
  plan_scope            text[] not null default '{}',
  version_scope         text[] not null default '{}',
  visibility            text not null default 'internal' check (visibility in ('internal','public')),
  human_confidence      numeric(4,3),
  current_version       integer not null default 0,
  created_by            uuid references users(id),
  approved_by           uuid references users(id),
  approved_at           timestamptz,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

create index if not exists canonical_answers_tenant_status_idx on canonical_answers (tenant_id, status);
create index if not exists canonical_answers_topic_idx on canonical_answers (topic_id);
create unique index if not exists canonical_answers_one_approved_idx
  on canonical_answers (tenant_id, coalesce(topic_id, '00000000-0000-0000-0000-000000000000'::uuid),
                        lower(question), plan_scope, version_scope, visibility)
  where status = 'approved';

create table if not exists answer_versions (
  id                 uuid primary key default gen_random_uuid(),
  tenant_id          uuid not null references tenants(id) on delete cascade,
  answer_id          uuid not null references canonical_answers(id) on delete cascade,
  version            integer not null,
  answer_md          text not null,
  changed_by         uuid references users(id),
  change_reason      text,
  supersedes_version integer,
  created_at         timestamptz not null default now(),
  unique (answer_id, version)
);

create table if not exists answer_evidence (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id) on delete cascade,
  answer_id       uuid not null references canonical_answers(id) on delete cascade,
  answer_version  integer not null,
  passage_id      uuid not null references passages(id),
  quote           text not null,
  claim_ref       text,
  created_at      timestamptz not null default now()
);

create index if not exists answer_evidence_answer_idx on answer_evidence (answer_id, answer_version);
create index if not exists answer_evidence_passage_idx on answer_evidence (passage_id);

-- ---------------------------------------------------------------------------
-- Governance and operations
-- ---------------------------------------------------------------------------

create table if not exists conflicts (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id) on delete cascade,
  passage_id_a    uuid references passages(id),
  passage_id_b    uuid references passages(id),
  answer_id_a     uuid references canonical_answers(id),
  answer_id_b     uuid references canonical_answers(id),
  kind            text not null check (kind in ('contradiction','overlap','version_disagreement')),
  detector        text not null check (detector in ('nli','llm','rule')),
  score           numeric(4,3),
  status          text not null default 'pending' check (status in ('pending','resolved','dismissed')),
  resolution      text,
  resolved_by     uuid references users(id),
  resolved_at     timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index if not exists conflicts_tenant_status_idx on conflicts (tenant_id, status);

create table if not exists gaps (
  id                      uuid primary key default gen_random_uuid(),
  tenant_id               uuid not null references tenants(id) on delete cascade,
  normalised_question     text not null,
  representative_question text not null,
  count                   integer not null default 1,
  first_seen_at           timestamptz not null default now(),
  last_seen_at            timestamptz not null default now(),
  channels                text[] not null default '{}',
  status                  text not null default 'open' check (status in ('open','answered','wont_answer')),
  linked_answer_id        uuid references canonical_answers(id),
  created_at              timestamptz not null default now(),
  updated_at              timestamptz not null default now(),
  unique (tenant_id, normalised_question)
);

create index if not exists gaps_tenant_status_idx on gaps (tenant_id, status, count desc);

create table if not exists qa_logs (
  id                    uuid primary key default gen_random_uuid(),
  tenant_id             uuid not null references tenants(id) on delete cascade,
  user_id               uuid references users(id),
  question              text not null,
  standalone_query      text,
  channel               text not null default 'web' check (channel in ('web','public','widget','discord')),
  plan                  text,
  product_version       text,
  as_of                 date,
  mode                  text check (mode in ('canonical','fallback','abstained')),
  answer_id             uuid references canonical_answers(id),
  answer_version        integer,
  answer_md             text,
  citations             jsonb not null default '[]'::jsonb,
  sources_ignored       jsonb not null default '[]'::jsonb,
  retrieved_passage_ids uuid[] not null default '{}',
  rerank_scores         real[] not null default '{}',
  confidence_composite  numeric(4,3),
  confidence_band       text,
  abstained             boolean not null default false,
  gap_id                uuid references gaps(id),
  feedback_thumbs       smallint,
  edited_answer_md      text,
  edit_distance         numeric(5,3),
  latency_ms            integer,
  provider              text,
  model                 text,
  token_cost            numeric(10,6),
  correlation_id        uuid not null default gen_random_uuid(),
  created_at            timestamptz not null default now()
);

create index if not exists qa_logs_tenant_created_idx on qa_logs (tenant_id, created_at desc);
create index if not exists qa_logs_answer_idx on qa_logs (answer_id);

create table if not exists ingest_jobs (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references tenants(id) on delete cascade,
  source_id        uuid not null references sources(id) on delete cascade,
  status           text not null default 'queued'
                     check (status in ('queued','running','succeeded','failed','dead_letter')),
  stage            text,
  error            text,
  attempts         integer not null default 0,
  idempotency_key  text not null,
  stats            jsonb not null default '{}'::jsonb,
  started_at       timestamptz,
  finished_at      timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (tenant_id, idempotency_key)
);

create index if not exists ingest_jobs_source_idx on ingest_jobs (source_id, created_at desc);

create table if not exists review_items (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  kind           text not null check (kind in
                   ('new_answer','edit_proposal','conflict','dedup_merge','topic_change','stale_answer',
                    'low_ocr','change_impact','entity_merge','glossary_promotion')),
  payload        jsonb not null default '{}'::jsonb,
  status         text not null default 'pending' check (status in ('pending','approved','rejected','deferred')),
  assignee_id    uuid references users(id),
  priority       integer not null default 50,
  dedupe_key     text,
  created_at     timestamptz not null default now(),
  decided_by     uuid references users(id),
  decided_at     timestamptz,
  decision_note  text
);

create unique index if not exists review_items_dedupe_idx
  on review_items (tenant_id, dedupe_key) where dedupe_key is not null and status = 'pending';
create index if not exists review_items_queue_idx on review_items (tenant_id, status, priority desc, created_at);

create table if not exists audit_log (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  actor_type   text not null check (actor_type in ('user','system')),
  actor_id     uuid,
  action       text not null,
  object_type  text not null,
  object_id    uuid,
  detail       jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now()
);

create index if not exists audit_log_tenant_created_idx on audit_log (tenant_id, created_at desc);

-- ---------------------------------------------------------------------------
-- Row-level security
-- ---------------------------------------------------------------------------

do $$
declare
  t text;
begin
  foreach t in array array[
    'users','sources','documents','passages','topics','canonical_answers',
    'answer_versions','answer_evidence','conflicts','gaps','qa_logs',
    'ingest_jobs','review_items','audit_log'
  ]
  loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
    execute format('drop policy if exists tenant_isolation on %I', t);
    execute format($f$
      create policy tenant_isolation on %I
        for all
        using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
        with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
    $f$, t);
  end loop;
end $$;

alter table tenants enable row level security;
alter table tenants force row level security;
drop policy if exists tenant_self on tenants;
create policy tenant_self on tenants
  for all
  using (id = nullif(current_setting('app.tenant_id', true), '')::uuid)
  with check (id = nullif(current_setting('app.tenant_id', true), '')::uuid);

-- Application role: not a table owner, no BYPASSRLS.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'kb_app') then
    create role kb_app login password 'kb_app_password' nosuperuser nocreatedb nocreaterole noinherit;
  end if;
end $$;

grant connect on database kb to kb_app;
grant usage on schema public to kb_app;
grant select, insert, update, delete on all tables in schema public to kb_app;
grant usage, select on all sequences in schema public to kb_app;
alter default privileges in schema public grant select, insert, update, delete on tables to kb_app;
alter default privileges in schema public grant usage, select on sequences to kb_app;
