-- Source-level scope defaults, so every document version inherits an explicit
-- plan/version window from the moment it is uploaded.
alter table sources add column if not exists plan_scope text[] not null default '{}';
alter table sources add column if not exists product_version_scope text[] not null default '{}';
