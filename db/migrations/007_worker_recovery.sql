-- Recover jobs left running when an API/worker process stops mid-ingest.
-- The worker calls this as the elected leader, so only one process requeues.
create index if not exists ingest_jobs_running_started_idx
  on ingest_jobs (status, started_at)
  where status = 'running';

create or replace function requeue_stale_ingest_jobs(
  p_older_than interval default interval '30 minutes',
  p_limit integer default 10
)
returns table (tenant_id uuid, job_id uuid, source_id uuid)
language sql
security definer
set search_path = public, pg_temp
as $$
  with stale as (
    select j.id
    from ingest_jobs j
    join sources s on s.id = j.source_id
    where j.status = 'running'
      and coalesce(j.started_at, j.created_at) < now() - p_older_than
      and s.deleted_at is null
    order by coalesce(j.started_at, j.created_at)
    limit p_limit
  )
  update ingest_jobs j
  set status = 'queued',
      started_at = null,
      finished_at = null,
      error = 'Re-queued after the worker stopped',
      attempts = j.attempts + 1,
      updated_at = now()
  from stale
  where j.id = stale.id
  returning j.tenant_id, j.id, j.source_id;
$$;

revoke all on function requeue_stale_ingest_jobs(interval, integer) from public;
grant execute on function requeue_stale_ingest_jobs(interval, integer) to kb_app;
