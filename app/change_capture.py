"""读数变更捕获：变更日志表 + 行级触发器。

readings 的所有 INSERT / UPDATE / DELETE（无论来自本服务还是任何独立 SQL
会话）都会在**同一个事务**内写入一条 reading_changes 记录。记录携带：

- change_xid：提交该变更的事务 ID（xid8）。同一事务的多行变更共享同一 xid；
- change_id：按语句写入顺序递增的标识，用于同一 xid 内的稳定排序；
- 变更前后的 meter_id / read_time / wh（INSERT 无 before，DELETE 无 after）。

分页读取端只返回 ``change_xid < pg_snapshot_xmin(pg_current_snapshot())``
的行：这些事务在读取快照下已经结束（提交或回滚），因此不会出现“先返回
较大的 xid，之后较小的 xid 才提交”的永久漏读。

触发器在服务启动时以幂等方式安装（CREATE OR REPLACE），因此已有的库也能
在升级后开始捕获；安装前的历史变更不回填。
"""
from __future__ import annotations

import logging
import threading
import time

import psycopg

from . import db as dbmod
from .config import settings

logger = logging.getLogger(__name__)

CAPTURE_DDL = r"""
CREATE TABLE IF NOT EXISTS public.reading_changes (
    change_xid    xid8        NOT NULL,
    change_id     bigint      GENERATED ALWAYS AS IDENTITY,
    changed_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    operation     text        NOT NULL,
    old_meter_id  bigint,
    old_read_time timestamptz,
    old_wh        bigint,
    new_meter_id  bigint,
    new_read_time timestamptz,
    new_wh        bigint,
    CONSTRAINT reading_changes_pkey PRIMARY KEY (change_xid, change_id),
    CONSTRAINT reading_changes_operation_check
        CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE')),
    CONSTRAINT reading_changes_shape_check CHECK (
        (operation = 'INSERT'
            AND old_meter_id IS NULL AND old_read_time IS NULL AND old_wh IS NULL
            AND new_meter_id IS NOT NULL AND new_read_time IS NOT NULL
            AND new_wh IS NOT NULL)
        OR
        (operation = 'UPDATE'
            AND old_meter_id IS NOT NULL AND old_read_time IS NOT NULL
            AND old_wh IS NOT NULL
            AND new_meter_id IS NOT NULL AND new_read_time IS NOT NULL
            AND new_wh IS NOT NULL)
        OR
        (operation = 'DELETE'
            AND old_meter_id IS NOT NULL AND old_read_time IS NOT NULL
            AND old_wh IS NOT NULL
            AND new_meter_id IS NULL AND new_read_time IS NULL AND new_wh IS NULL)
    )
);

CREATE OR REPLACE FUNCTION public.reading_changes_capture()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO public.reading_changes (
            change_xid, operation, new_meter_id, new_read_time, new_wh
        ) VALUES (
            pg_catalog.pg_current_xact_id(), 'INSERT',
            NEW.meter_id, NEW.read_time, NEW.wh
        );
    ELSIF TG_OP = 'UPDATE' THEN
        INSERT INTO public.reading_changes (
            change_xid, operation,
            old_meter_id, old_read_time, old_wh,
            new_meter_id, new_read_time, new_wh
        ) VALUES (
            pg_catalog.pg_current_xact_id(), 'UPDATE',
            OLD.meter_id, OLD.read_time, OLD.wh,
            NEW.meter_id, NEW.read_time, NEW.wh
        );
    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO public.reading_changes (
            change_xid, operation, old_meter_id, old_read_time, old_wh
        ) VALUES (
            pg_catalog.pg_current_xact_id(), 'DELETE',
            OLD.meter_id, OLD.read_time, OLD.wh
        );
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE TRIGGER readings_change_capture
AFTER INSERT OR UPDATE OR DELETE ON public.readings
FOR EACH ROW
EXECUTE FUNCTION public.reading_changes_capture();
"""


class ChangeCaptureError(RuntimeError):
    """无法安装变更捕获结构。"""


_ensured_dsns: set[str] = set()
_ensure_lock = threading.Lock()


def _seed_demo_changes_if_empty(conn: psycopg.Connection) -> None:
    """演示环境：变更日志为空时，生成 INSERT / UPDATE / DELETE 示例。

    在**同一事务**内插入、更正、删除一条演示读数后提交：变更日志会留下
    三条可读记录，而 readings 的最终状态与演示前完全一致，不污染示例表。
    若演示行已存在（用户数据撞车），直接跳过，不报错。
    """
    if conn.execute(
        "SELECT EXISTS (SELECT 1 FROM public.reading_changes)"
    ).fetchone()[0]:
        return
    demo_time = "TIMESTAMPTZ '2026-11-15 10:00:00+00'"
    exists = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM readings WHERE meter_id = 3001 "
        f"AND read_time = {demo_time})"
    ).fetchone()[0]
    if exists:
        return
    conn.execute(
        "INSERT INTO readings (meter_id, read_time, wh) VALUES "
        f"(3001, {demo_time}, 1000)"
    )
    conn.execute(
        "UPDATE readings SET wh = 1100, read_time = TIMESTAMPTZ '2026-11-16 00:00:00+00' "
        f"WHERE meter_id = 3001 AND read_time = {demo_time}"
    )
    conn.execute(
        "DELETE FROM readings WHERE meter_id = 3001 "
        "AND read_time = TIMESTAMPTZ '2026-11-16 00:00:00+00'"
    )


def ensure_change_capture(
    dsn: str | None = None,
    *,
    attempts: int = 20,
    retry_delay: float = 0.5,
    seed_demo: bool | None = None,
) -> None:
    """幂等安装变更日志与触发器；数据库暂未就绪时短暂重试。"""
    target = dsn or settings.pg_dsn
    with _ensure_lock:
        if target in _ensured_dsns:
            return
        last_error: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            conn = None
            try:
                conn = dbmod.open_connection(target)
                conn.execute(CAPTURE_DDL)
                if seed_demo is None:
                    seed_demo = settings.seed_demo_changes
                if seed_demo:
                    conn.execute("BEGIN")
                    try:
                        _seed_demo_changes_if_empty(conn)
                        conn.execute("COMMIT")
                    except psycopg.Error as exc:
                        # 演示数据失败不应影响服务可用性
                        conn.execute("ROLLBACK")
                        logger.warning("生成演示变更失败，已跳过：%s", exc)
                _ensured_dsns.add(target)
                logger.info("readings 变更捕获已就绪")
                return
            except psycopg.OperationalError as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(retry_delay)
            except Exception as exc:  # DDL 权限/语法等问题重试无意义
                raise ChangeCaptureError(
                    f"安装 readings 变更捕获失败：{exc.__class__.__name__}: {exc}"
                ) from exc
            finally:
                if conn is not None:
                    dbmod.quiet_close(conn)
        raise ChangeCaptureError(
            f"数据库未就绪，无法安装 readings 变更捕获：{last_error}"
        )
