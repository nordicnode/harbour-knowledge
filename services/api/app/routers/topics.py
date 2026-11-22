from __future__ import annotations

from fastapi import APIRouter, Depends

from ..db import fetch_all
from ..deps import Context, get_context

router = APIRouter(tags=["organisation"])


@router.get("/topics")
async def list_topics(ctx: Context = Depends(get_context)):
    rows = await fetch_all(
        ctx.conn,
        """select t.*, u.name as owner_name,
                  (select count(*) from canonical_answers ca where ca.topic_id = t.id and ca.status = 'approved') as approved_count,
                  (select count(*) from canonical_answers ca where ca.topic_id = t.id and ca.status <> 'archived') as answer_count,
                  (select count(*) from documents d where d.topic_id = t.id and d.status = 'current') as source_count
           from topics t left join users u on u.id = t.owner_id
           order by t.name""",
    )
    return {"topics": rows}
