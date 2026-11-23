-- Running workers hold the job row lock during their atomic replacement.
-- Skip those rows instead of waiting and accidentally requeueing a successful
-- attempt that happened to exceed the stale-job threshold.
create or replace function requeue_stale_ingest_jobs(
  p_older_than interval default interval '30 minutes',
  p_limit integer default 10,
  p_max_attempts integer default 3
)
returns table (tenant_id uuid, job_id uuid, source_id uuid, outcome text)
language sql
security definer
set search_path = public, pg_temp
as $$
  with stale as (
    select j.id, j.tenant_id, j.source_id, j.attempts
    from ingest_jobs j
    join sources s on s.id = j.source_id
    where j.status = 'running'
      and coalesce(j.started_at, j.created_at) < now() - p_older_than
      and s.deleted_at is null
    order by coalesce(j.started_at, j.created_at)
    limit p_limit
    for update of j skip locked
  ),
  requeued as (
    update ingest_jobs j
    set status = 'queued', started_at = null, finished_at = null,
        error = 'Re-queued after the worker stopped',
        attempts = j.attempts + 1, updated_at = now()
    from stale
    where j.id = stale.id and stale.attempts < p_max_attempts
    returning j.tenant_id as tenant_id, j.id as job_id, j.source_id as source_id
  ),
  exhausted as (
    update ingest_jobs j
    set status = 'failed', stage = 'recovery',
        error = format('Stopped after %s recovery attempts', p_max_attempts),
        finished_at = now(), updated_at = now()
    from stale
    where j.id = stale.id and stale.attempts >= p_max_attempts
    returning j.tenant_id as tenant_id, j.id as job_id, j.source_id as source_id
  )
  select tenant_id, job_id, source_id, 'queued'::text as outcome from requeued
  union all
  select tenant_id, job_id, source_id, 'failed'::text as outcome from exhausted;
$$;
