-- The worker needs to discover queued jobs across tenants without bypassing RLS
-- on tenant data. This security-definer function exposes only job and source ids.
create or replace function queued_ingest_jobs(p_limit integer default 5)
returns table (tenant_id uuid, job_id uuid, source_id uuid)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select j.tenant_id, j.id, j.source_id
  from ingest_jobs j
  join sources s on s.id = j.source_id
  where j.status = 'queued' and s.deleted_at is null
  order by j.created_at
  limit p_limit;
$$;

revoke all on function queued_ingest_jobs(integer) from public;
grant execute on function queued_ingest_jobs(integer) to kb_app;
