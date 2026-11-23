-- A bulk withdrawal must wait for every source's in-flight ingest without
-- taking locks in an order that can deadlock against another bulk request.
-- Sort the ids inside the function so all callers acquire the same order.
create or replace function lock_ingest_sources(p_source_ids uuid[])
returns void
language plpgsql
set search_path = pg_catalog, pg_temp
as $$
declare
  source_id uuid;
begin
  for source_id in
    select id from unnest(p_source_ids) as id order by id
  loop
    perform pg_advisory_xact_lock(hashtextextended(source_id::text, 0));
  end loop;
end;
$$;

revoke all on function lock_ingest_sources(uuid[]) from public;
grant execute on function lock_ingest_sources(uuid[]) to kb_app;
