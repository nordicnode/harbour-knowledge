-- Embedding identity and staged reindex.
--
-- Vectors are only comparable when they were produced by the same provider,
-- model, dimensions, query instruction, embedding version and ingestion
-- pipeline revision. `embedding_identity` records that fingerprint on each
-- passage, and dense retrieval only considers rows whose identity matches the
-- active configuration. Existing rows deliberately start with a null identity:
-- their vectors may predate the exact token-limit check or have been produced
-- under a different setting, so they stay out of dense search until `make
-- reindex` has rebuilt them. Lexical retrieval remains available meanwhile.
--
-- The staging table lets reindex commit embeddings in small batches and switch
-- them into `passages` atomically only after every current passage is present.
-- An interrupted run leaves the previous embeddings untouched and resumes from
-- the staged rows.

alter table passages add column if not exists embedding_identity text;

create index if not exists passages_embedding_identity_idx
  on passages (embedding_identity)
  where status = 'current' and level = 'chunk';

create table if not exists embedding_reindex_staging (
  passage_id  uuid primary key references passages(id) on delete cascade,
  tenant_id   uuid not null references tenants(id) on delete cascade,
  identity    text not null,
  embedding   vector(1024) not null,
  created_at  timestamptz not null default now()
);

create index if not exists embedding_reindex_staging_tenant_idx
  on embedding_reindex_staging (tenant_id, identity);

alter table embedding_reindex_staging enable row level security;
alter table embedding_reindex_staging force row level security;
drop policy if exists tenant_isolation on embedding_reindex_staging;
create policy tenant_isolation on embedding_reindex_staging
  for all
  using (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
  with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);

grant select, insert, update, delete on embedding_reindex_staging to kb_app;
