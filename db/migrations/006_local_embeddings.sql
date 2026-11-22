-- Local embedding model dimensions.
--
-- Qwen3-Embedding-0.6B returns 1024 dimensions. The original schema used 1536,
-- so this migration resizes the column once and clears stale vectors. The
-- guard keeps it safe to re-run: once the column is 1024, nothing is touched.
--
-- Existing passages must be re-embedded with `make reindex` after this runs,
-- because the old vectors are not comparable with the new model.

do $$
declare
  current_type text;
begin
  select format_type(a.atttypid, a.atttypmod)
    into current_type
    from pg_attribute a
   where a.attrelid = 'passages'::regclass
     and a.attname = 'embedding'
     and not a.attisdropped;

  if current_type is not null and current_type <> 'vector(1024)' then
    drop index if exists passages_embedding_hnsw;

    alter table passages
      alter column embedding type vector(1024) using null;

    update passages
       set embedding = null,
           embedding_model = null,
           embedding_version = null;

    create index if not exists passages_embedding_hnsw
      on passages using hnsw (embedding vector_cosine_ops)
      where status = 'current' and level = 'chunk';
  end if;
end
$$;
