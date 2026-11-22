-- Approved answers must not stay in the ready set after the evidence behind
-- them is gone. Backfill answers whose current evidence comes from a withdrawn
-- source or a passage that is no longer current.
update canonical_answers ca
set status = 'needs_recheck', updated_at = now()
where ca.status = 'approved'
  and exists (
    select 1
    from answer_evidence ae
    join passages p on p.id = ae.passage_id
    join documents d on d.id = p.document_id
    join sources s on s.id = d.source_id
    where ae.answer_id = ca.id
      and ae.answer_version = ca.current_version
      and (
        s.deleted_at is not null
        or s.status = 'withdrawn'
        or p.status in ('superseded', 'tombstoned')
        or d.status in ('superseded', 'withdrawn')
      )
  );
