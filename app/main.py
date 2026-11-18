"""FastAPI 应用入口与 HTTP 路由。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import changes as changesmod
from . import db as dbmod
from .change_capture import ChangeCaptureError, ensure_change_capture
from .manager import SlotBusyError, manager
from .models import CreateJobRequest, JobOut, ReadingChangesPage
from .sqlvalidation import SqlValidationError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 幂等安装变更捕获（表 + 触发器）；数据库暂不可用时降级为告警，
    # 首次访问 /readings/changes 时还会再次尝试。
    try:
        ensure_change_capture(attempts=10, retry_delay=0.5)
    except ChangeCaptureError as exc:
        logger.warning("启动时安装变更捕获失败：%s", exc)
    yield


app = FastAPI(
    title="电表日结 SQL 核对 API",
    description=(
        "提交两条只读 SELECT，基于同一已提交快照并行读取，"
        "按行内容与出现次数做有序多重集核对（忽略行序，NULL≠空串），"
        "差异最多展示 20 种。单轮 30 秒，全局同时只允许一个任务。"
        "\n\n"
        "另外提供 /readings/changes 分页接口，按提交顺序安全地下发"
        " readings 的 INSERT / UPDATE / DELETE，供下游日结库重算受影响的表号与 UTC 日期。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["运维"])
def health() -> dict:
    """存活探针；不触碰数据库。"""
    return {"status": "ok"}


@app.get("/readings/changes", response_model=ReadingChangesPage, tags=["读数变更"])
def list_reading_changes(
    cursor: str | None = Query(
        None, description="上一页返回的 next_cursor；省略则从头开始"
    ),
    limit: int = Query(
        50, ge=1, le=100, description="每页条数，1～100"
    ),
) -> ReadingChangesPage:
    """按提交顺序分页读取读数变更。

    只返回事务已结束（且按 MVCC 可见）的变更；尚未提交的较早事务会
    阻止更大 xid 的变更被越过，回滚变更不可见，空页不推进游标。
    """
    try:
        items, next_cursor, has_more = changesmod.fetch_page(cursor, limit)
    except changesmod.InvalidCursor as exc:
        raise HTTPException(status_code=400, detail=f"游标非法：{exc}") from exc
    except (changesmod.ChangesQueryError, ChangeCaptureError) as exc:
        raise HTTPException(status_code=503, detail={"message": str(exc)}) from exc
    return ReadingChangesPage(
        items=items, next_cursor=next_cursor, has_more=has_more
    )


@app.get("/jobs", response_model=list[JobOut], tags=["核对任务"])
def list_jobs() -> list[JobOut]:
    """列出近期任务（含终态），状态查询始终可用。"""
    return manager.list_jobs()


@app.post("/jobs", response_model=JobOut, status_code=201, tags=["核对任务"])
def create_job(payload: CreateJobRequest) -> JobOut:
    try:
        job = manager.create_job(payload.left_sql, payload.right_sql, payload.name)
    except SqlValidationError as exc:
        raise HTTPException(status_code=400, detail=f"SQL 校验失败：{exc}") from exc
    except SlotBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "当前已有核对任务在运行，一次只允许一组",
                "active_job_id": exc.active_job_id,
            },
        ) from exc
    return job.snapshot()


@app.get("/jobs/{job_id}", response_model=JobOut, tags=["核对任务"])
def get_job(job_id: str) -> JobOut:
    job = manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.snapshot()


@app.post("/jobs/{job_id}/cancel", response_model=JobOut, tags=["核对任务"])
def cancel_job(job_id: str) -> JobOut:
    try:
        job = manager.request_cancel(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return job.snapshot()


@app.exception_handler(dbmod.DbError)
def _db_error_handler(_request, exc: dbmod.DbError) -> JSONResponse:
    return JSONResponse(status_code=422, detail={"message": str(exc)})
