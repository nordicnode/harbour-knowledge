-- Recent review decisions can be undone once, briefly, from the review toast.
-- The snapshot is written by the decision endpoint and cleared by undo.
alter table review_items
  add column if not exists undo_payload jsonb,
  add column if not exists undo_expires_at timestamptz;

create index if not exists review_items_undo_idx
  on review_items (tenant_id, undo_expires_at)
  where undo_payload is not null;
