"""API 请求 / 响应模型。"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

JobStatus = Literal[
    "pending",      # 已受理，等待运行名额
    "running",      # 运行中
    "cancelling",   # 已收到取消请求，正在中止本轮 SQL
    "succeeded",    # 终态：核对完成（可能一致，也可能有差异）
    "failed",       # 终态：任一侧失败
    "cancelled",    # 终态：用户取消
    "timeout",      # 终态：超过单轮时限
]

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timeout"}
ACTIVE_STATUSES = {"pending", "running", "cancelling"}


class CreateJobRequest(BaseModel):
    left_sql: str = Field(..., min_length=1, description="左侧只读 SELECT（如改写后的日结 SQL）")
    right_sql: str = Field(..., min_length=1, description="右侧只读 SELECT（基准 SQL）")
    name: str | None = Field(None, max_length=128, description="可选备注名")


class DiffRow(BaseModel):
    # 与结果集列顺序对齐；NULL 用 null 表示，空串为 ""，二者不同
    values: list[Any]
    left_count: int
    right_count: int


class ReconcileResult(BaseModel):
    identical: bool
    columns: list[str]
    column_types: list[str]          # bigint | text
    left_row_total: int
    right_row_total: int
    matched_group_count: int         # 行内容与次数都相同的去重行数
    diff_group_total: int            # 存在差异的去重行种类总数（不受 20 条展示上限影响）
    diffs_shown: int
    diffs: list[DiffRow]             # 最多 20 种


class JobOut(BaseModel):
    job_id: str
    name: str | None
    status: JobStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    deadline: datetime | None
    cancel_requested: bool
    error: str | None
    result: ReconcileResult | None

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(timezone.utc)


class ReadingSnapshot(BaseModel):
    """一条读数的前后镜像（快照）；变更前后不存在的一侧为 null。"""

    meter_id: int = Field(..., description="表号")
    read_time: datetime = Field(..., description="读数时刻（UTC）")
    wh: int = Field(..., description="累计电量 Wh")


class ReadingChange(BaseModel):
    """readings 的一次行级变更。"""

    cursor: str = Field(..., description="本条变更的接续游标")
    operation: Literal["INSERT", "UPDATE", "DELETE"]
    changed_at: datetime = Field(..., description="变更写入时间（UTC）")
    before: ReadingSnapshot | None = Field(
        None, description="变更前；INSERT 为 null"
    )
    after: ReadingSnapshot | None = Field(
        None, description="变更后；DELETE 为 null"
    )
    affected_meter_ids: list[int] = Field(
        ...,
        description="需要重算的表号（变更前后表号的并集，升序）",
    )
    affected_utc_dates: list[date] = Field(
        ...,
        description="需要重算的 UTC 日期（变更前后时刻所在日期，去重升序）",
    )


class ReadingChangesPage(BaseModel):
    items: list[ReadingChange]
    next_cursor: str | None = Field(
        None,
        description="接续游标；空页时与请求游标相同（不推进），从头读时为 null",
    )
    has_more: bool = Field(..., description="是否还有更多已可读的变更")
