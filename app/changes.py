"""读数变更分页读取。

游标推进的安全前提
------------------
事务 ID（xid）在**第一次写入**时分配，早于提交；因此仅按 xid 排序读取会
出现“较大的 xid 已提交并返回，较小的 xid 稍后才提交”的永久漏读。

本模块只返回 ``change_xid < pg_snapshot_xmin(pg_current_snapshot())`` 的
变更。``xmin`` 是读取快照开始时仍活跃的最小 xid，所有小于它的 xid 都已
结束（提交或回滚）。于是：

- 尚未提交的较早变更（xid 较小）会把 xmin 压在它的 xid 上，任何更大的
  xid 都不会先被返回，游标不会越过它；
- 回滚事务的行在 MVCC 下不可见，不会产生可读变更；
- 一页没有可读变更时，``next_cursor`` 原样返回请求游标，不推进。

游标为 ``(change_xid, change_id)`` 的不透明编码，``change_id`` 是事务内
按写入顺序递增的标识，用于同一事务内多行变更的稳定排序。
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

import psycopg

from . import db as dbmod
from .change_capture import ensure_change_capture
from .models import ReadingChange, ReadingSnapshot

_XID8_MAX = 2**64 - 1
_BIGINT_MAX = 2**63 - 1
_CURSOR_RE = re.compile(r"([0-9]{1,20}):([0-9]{1,19})")

_PAGE_SQL = """
WITH horizon AS (
    SELECT pg_snapshot_xmin(pg_current_snapshot()) AS xmin
)
SELECT change_xid,
       change_id,
       changed_at,
       operation,
       old_meter_id, old_read_time, old_wh,
       new_meter_id, new_read_time, new_wh
FROM public.reading_changes, horizon
WHERE change_xid < horizon.xmin
  AND (change_xid, change_id) > (%s::xid8, %s::bigint)
ORDER BY change_xid, change_id
LIMIT %s
"""


class InvalidCursor(ValueError):
    """游标格式非法。"""


class ChangesQueryError(RuntimeError):
    """变更日志读取失败。"""


@dataclass(frozen=True)
class Cursor:
    xid: str
    change_id: int

    def encode(self) -> str:
        raw = f"{self.xid}:{self.change_id}".encode("ascii")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


START_CURSOR = Cursor(xid="0", change_id=0)


def decode_cursor(raw: str | None) -> Cursor:
    if raw is None or raw == "":
        return START_CURSOR
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursor("游标不是合法的 base64url 编码") from exc
    # 仅接受本服务生成的标准形式（无填充），拒绝同一字节的变体编码
    canonical = base64.urlsafe_b64encode(decoded.encode("ascii")).decode("ascii").rstrip("=")
    if canonical != raw:
        raise InvalidCursor("游标不是规范编码")
    match = _CURSOR_RE.fullmatch(decoded)
    if match is None:
        raise InvalidCursor("游标内容非法，应为 xid:change_id")
    xid_text, change_id_text = match.groups()
    if int(xid_text) > _XID8_MAX or int(change_id_text) > _BIGINT_MAX:
        raise InvalidCursor("游标数值超出范围")
    return Cursor(xid=str(int(xid_text)), change_id=int(change_id_text))


def _utc_date(value: datetime | None) -> date | None:
    if value is None:
        return None
    if value.tzinfo is None:  # timestamptz 正常返回带时区；防御性处理
        return value.date()
    return value.astimezone(timezone.utc).date()


def _affected_dates(
    old_read_time: datetime | None, new_read_time: datetime | None
) -> list[date]:
    dates = {_utc_date(old_read_time), _utc_date(new_read_time)}
    dates.discard(None)
    return sorted(dates)  # type: ignore[arg-type]


def _affected_meters(
    old_meter_id: int | None, new_meter_id: int | None
) -> list[int]:
    meters = {old_meter_id, new_meter_id}
    meters.discard(None)
    return sorted(meters)  # type: ignore[arg-type]


def _snapshot(
    meter_id: int | None, read_time: datetime | None, wh: int | None
) -> ReadingSnapshot | None:
    if meter_id is None or read_time is None or wh is None:
        return None
    return ReadingSnapshot(meter_id=meter_id, read_time=read_time, wh=wh)


def fetch_page(raw_cursor: str | None, limit: int) -> tuple[list[ReadingChange], str | None, bool]:
    """返回 (本页变更, 接续游标, 是否还有更多)。

    ``limit`` 已由 API 层限制在 1..100。空页时接续游标与请求游标完全一致。
    """
    cursor = decode_cursor(raw_cursor)
    # 启动时已尝试安装；请求路径只再试一次，避免数据库不可用时长时间持锁
    ensure_change_capture(attempts=1, retry_delay=0)

    conn = None
    try:
        conn = dbmod.open_connection()
        rows = conn.execute(
            _PAGE_SQL, (cursor.xid, cursor.change_id, limit + 1)
        ).fetchall()
    except psycopg.Error as exc:
        raise ChangesQueryError(
            f"读取变更日志失败：{exc.__class__.__name__}: {exc}"
        ) from exc
    finally:
        if conn is not None:
            dbmod.quiet_close(conn)

    has_more = len(rows) > limit
    items: list[ReadingChange] = []
    for row in rows[:limit]:
        (
            change_xid,
            change_id,
            changed_at,
            operation,
            old_meter_id,
            old_read_time,
            old_wh,
            new_meter_id,
            new_read_time,
            new_wh,
        ) = row
        items.append(
            ReadingChange(
                cursor=Cursor(xid=str(change_xid), change_id=change_id).encode(),
                operation=operation,
                changed_at=changed_at,
                before=_snapshot(old_meter_id, old_read_time, old_wh),
                after=_snapshot(new_meter_id, new_read_time, new_wh),
                affected_meter_ids=_affected_meters(old_meter_id, new_meter_id),
                affected_utc_dates=_affected_dates(old_read_time, new_read_time),
            )
        )

    # 空页不推进游标：原样返回请求中的游标（未传或空串则仍为 null）
    request_cursor = raw_cursor or None
    next_cursor = items[-1].cursor if items else request_cursor
    return items, next_cursor, has_more
