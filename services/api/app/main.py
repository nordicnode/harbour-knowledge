from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .db import close_pool, get_pool, open_pool
from .errors import AppError
from .routers import answers, ask, gaps, meta, overview, review, search, sources, topics
from .worker import worker_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.validate_provider()
    await open_pool()
    async with get_pool().connection() as conn:
        row = await (
            await conn.execute("select extversion from pg_extension where extname = 'vector'")
        ).fetchone()
        if row is None:
            raise RuntimeError("pgvector is not installed in this database. Run make migrate.")
        log.info("pgvector %s ready; AI mode=%s", row["extversion"], settings.ai_mode)
    stop = asyncio.Event()
    worker = asyncio.create_task(worker_loop(stop))
    try:
        yield
    finally:
        stop.set()
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        await close_pool()


app = FastAPI(
    title="Harbour Knowledge API",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[get_settings().web_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "correlation_id": str(exc.correlation_id),
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {"msg": "Invalid request"}
    field = ".".join(str(part) for part in first.get("loc", []) if part != "body")
    message = f"{field}: {first.get('msg')}" if field else str(first.get("msg"))
    correlation_id = uuid4()
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation", "message": message, "correlation_id": str(correlation_id)}},
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    correlation_id = uuid4()
    log.exception("unhandled error %s", correlation_id, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal",
                "message": "Something went wrong. Quote the correlation id when reporting it.",
                "correlation_id": str(correlation_id),
            }
        },
    )


@app.get("/healthz")
async def healthz():
    try:
        async with get_pool().connection() as conn:
            await conn.execute("select 1")
    except Exception as exc:  # noqa: BLE001 - health endpoint must report, not raise
        log.error("health check failed: %s", exc)
        return JSONResponse(status_code=503, content={"status": "database_unavailable"})
    return {"status": "ok", "ai_mode": get_settings().ai_mode}


for router in (
    ask.router,
    sources.router,
    answers.router,
    review.router,
    gaps.router,
    topics.router,
    search.router,
    overview.router,
    meta.router,
):
    app.include_router(router, prefix="/v1")
