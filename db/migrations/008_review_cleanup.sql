-- Sources withdrawn before deletion learned to clean up after itself left
-- review decisions pointing at tombstoned passages. Close those decisions and
-- dismiss the affected conflicts; the underlying audit rows are retained.
with deleted_passages as (
  select p.id
  from passages p
  join documents d on d.id = p.document_id
  join sources s on s.id = d.source_id
  where s.deleted_at is not null or s.status = 'withdrawn'
)
update conflicts c
set status = 'dismissed',
    resolution = 'Source removed before this conflict was resolved.',
    resolved_at = now(),
    updated_at = now()
where c.status = 'pending'
  and (c.passage_id_a in (select id from deleted_passages)
       or c.passage_id_b in (select id from deleted_passages));

with deleted_sources as (
  select id from sources where deleted_at is not null or status = 'withdrawn'
),
deleted_passages as (
  select p.id
  from passages p
  join documents d on d.id = p.document_id
  join sources s on s.id = d.source_id
  where s.deleted_at is not null or s.status = 'withdrawn'
),
affected_conflicts as (
  select c.id
  from conflicts c
  where c.passage_id_a in (select id from deleted_passages)
     or c.passage_id_b in (select id from deleted_passages)
)
update review_items ri
set status = 'rejected',
    decision_note = 'Closed automatically: its source was removed.',
    decided_at = now(),
    undo_payload = null,
    undo_expires_at = null
where ri.status = 'pending'
  and (
    ri.payload->>'source_id' in (select id::text from deleted_sources)
    or ri.payload->>'new_passage_id' in (select id::text from deleted_passages)
    or ri.payload->>'existing_passage_id' in (select id::text from deleted_passages)
    or exists (
      select 1 from deleted_passages dp
      where coalesce(ri.payload->'evidence_passage_ids', '[]'::jsonb) ? dp.id::text
    )
    or ri.payload->>'conflict_id' in (select id::text from affected_conflicts)
    or (
      ri.kind in ('new_answer', 'edit_proposal')
      and exists (
        select 1
        from answer_evidence ae
        join deleted_passages dp on dp.id = ae.passage_id
        join canonical_answers ca on ca.id = ae.answer_id
        where ca.tenant_id = ri.tenant_id
          and ca.id::text = ri.payload->>'answer_id'
          and ae.answer_version = ca.current_version
      )
    )
  );
