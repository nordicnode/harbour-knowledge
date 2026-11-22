-- Tenant bootstrap helper + document topic link.
-- The helper is the only privileged read in the app: it maps a workspace slug to
-- its id before app.tenant_id can be set. RLS still governs every tenant table.

alter table documents add column if not exists topic_id uuid references topics(id);
create index if not exists documents_topic_idx on documents (topic_id);

create or replace function tenant_id_for_slug(p_slug text)
returns uuid
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select id from tenants where slug = p_slug;
$$;

revoke all on function tenant_id_for_slug(text) from public;
grant execute on function tenant_id_for_slug(text) to kb_app;
