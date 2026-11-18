"""读数变更分页 API 测试（真实 PostgreSQL）。"""
from __future__ import annotations

import base64
import time
from datetime import datetime, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

T0 = "2026-11-20 08:00:00+00"
T1 = "2026-11-20 09:00:00+00"


def _insert(conn: psycopg.Connection, meter: int, ts: str, wh: int) -> None:
    conn.execute(
        "INSERT INTO readings (meter_id, read_time, wh) "
        "VALUES (%s, %s::timestamptz, %s)",
        (meter, ts, wh),
    )


def _get(client: TestClient, cursor: str | None = None, limit: int | None = None) -> dict:
    params: dict[str, object] = {}
    if cursor is not None:
        params["cursor"] = cursor
    if limit is not None:
        params["limit"] = limit
    resp = client.get("/readings/changes", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _cleanup(conn: psycopg.Connection, meter: int, ts: str) -> None:
    conn.execute(
        "DELETE FROM readings WHERE meter_id = %s AND read_time = %s::timestamptz",
        (meter, ts),
    )


def test_insert_update_delete_payload(client: TestClient, pg_dsn: str) -> None:
    writer = psycopg.connect(pg_dsn, autocommit=True)
    try:
        _insert(writer, 1002, T0, 777)
        writer.execute(
            "UPDATE readings SET wh = 888 WHERE meter_id = 1002 "
            "AND read_time = %s::timestamptz",
            (T0,),
        )
        _cleanup(writer, 1002, T0)

        page = _get(client)
        assert page["has_more"] is False
        ops = [item["operation"] for item in page["items"]]
        assert ops == ["INSERT", "UPDATE", "DELETE"]

        ins, upd, dele = page["items"]
        assert ins["before"] is None
        assert ins["after"]["meter_id"] == 1002
        assert ins["after"]["wh"] == 777
        assert datetime.fromisoformat(ins["after"]["read_time"]) == datetime(
            2026, 11, 20, 8, 0, tzinfo=timezone.utc
        )
        assert ins["affected_utc_dates"] == ["2026-11-20"]
        assert ins["affected_meter_ids"] == [1002]

        assert upd["before"]["wh"] == 777
        assert upd["after"]["wh"] == 888
        assert upd["affected_utc_dates"] == ["2026-11-20"]

        assert dele["before"]["wh"] == 888
        assert dele["after"] is None
        assert dele["affected_utc_dates"] == ["2026-11-20"]

        # 每条都带自己的接续游标；最后一页游标与最后一条一致
        assert page["next_cursor"] == dele["cursor"]
        for item in page["items"]:
            assert base64.urlsafe_b64decode(item["cursor"] + "==").decode().count(":") == 1
    finally:
        writer.close()


def test_non_utc_timestamps_are_normalised_to_utc_dates(
    client: TestClient, pg_dsn: str
) -> None:
    """时刻按 UTC 归日：+08 的 2026-11-16 07:00 属于 UTC 2026-11-15。"""
    writer = psycopg.connect(pg_dsn, autocommit=True)
    try:
        _insert(writer, 1002, "2026-11-16 07:00:00+08", 42)
        item = _get(client)["items"][-1]
        assert item["affected_utc_dates"] == ["2026-11-15"]
        assert datetime.fromisoformat(item["after"]["read_time"]) == datetime(
            2026, 11, 15, 23, 0, tzinfo=timezone.utc
        )
    finally:
        _cleanup(writer, 1002, "2026-11-15 23:00:00+00")
        writer.close()


def test_cross_date_update_reports_both_utc_dates(client: TestClient, pg_dsn: str) -> None:
    writer = psycopg.connect(pg_dsn, autocommit=True)
    try:
        _insert(writer, 1002, "2026-11-20 23:30:00+00", 100)
        writer.execute(
            "UPDATE readings SET read_time = %s::timestamptz WHERE meter_id = 1002 "
            "AND read_time = %s::timestamptz",
            ("2026-11-21 00:30:00+00", "2026-11-20 23:30:00+00"),
        )
        update_item = _get(client)["items"][-1]
        assert update_item["operation"] == "UPDATE"
        assert update_item["affected_utc_dates"] == ["2026-11-20", "2026-11-21"]
    finally:
        _cleanup(writer, 1002, "2026-11-20 23:30:00+00")
        _cleanup(writer, 1002, "2026-11-21 00:30:00+00")
        writer.close()


def test_pagination_limit_and_cursor(client: TestClient, pg_dsn: str) -> None:
    writer = psycopg.connect(pg_dsn, autocommit=True)
    try:
        for i in range(3):
            _insert(writer, 1002, T0, 100 + i)
            _cleanup(writer, 1002, T0)

        first = _get(client, limit=2)
        assert len(first["items"]) == 2
        assert first["has_more"] is True

        second = _get(client, first["next_cursor"], limit=2)
        assert len(second["items"]) == 2
        assert second["has_more"] is True

        third = _get(client, second["next_cursor"], limit=2)
        assert len(third["items"]) == 2
        assert third["has_more"] is False

        # 无重无漏，顺序稳定
        cursors = [i["cursor"] for i in first["items"] + second["items"] + third["items"]]
        assert len(set(cursors)) == 6

        # 续读页为空：游标原样返回，不推进
        empty = _get(client, third["next_cursor"], limit=2)
        assert empty["items"] == []
        assert empty["next_cursor"] == third["next_cursor"]
        assert empty["has_more"] is False
    finally:
        writer.close()


def test_from_start_empty_page_keeps_cursor_null(client: TestClient) -> None:
    page = _get(client)
    assert page["items"] == []
    assert page["next_cursor"] is None
    assert page["has_more"] is False


@pytest.mark.parametrize("limit", [0, 101])
def test_limit_out_of_range_rejected(client: TestClient, limit: int) -> None:
    assert client.get("/readings/changes", params={"limit": limit}).status_code == 422


@pytest.mark.parametrize("cursor", ["not-base64!!", "Zm9v", "MTIz"])  # "foo", "123"
def test_invalid_cursor_rejected(client: TestClient, cursor: str) -> None:
    assert client.get("/readings/changes", params={"cursor": cursor}).status_code == 400


def test_rolled_back_change_is_not_readable(client: TestClient, pg_dsn: str) -> None:
    conn = psycopg.connect(pg_dsn, autocommit=False)
    try:
        _insert(conn, 1002, T0, 555)
        conn.rollback()
    finally:
        conn.close()
    assert _get(client)["items"] == []


def test_rollback_then_reinsert_same_row_is_readable(client: TestClient, pg_dsn: str) -> None:
    dead = psycopg.connect(pg_dsn, autocommit=False)
    writer = psycopg.connect(pg_dsn, autocommit=True)
    try:
        _insert(dead, 1002, T0, 555)
        dead.rollback()
        _insert(writer, 1002, T0, 556)
        items = _get(client)["items"]
        assert len(items) == 1
        assert items[0]["operation"] == "INSERT"
        assert items[0]["after"]["wh"] == 556
    finally:
        dead.close()
        _cleanup(writer, 1002, T0)
        writer.close()


def test_cursor_does_not_jump_over_uncommitted_earlier_change(
    client: TestClient, pg_dsn: str
) -> None:
    """交错提交的关键场景：

    事务 A 先拿到较小 xid 但保持未提交；事务 B 后拿到较大 xid 并先提交。
    此时按游标读取不能返回 B，否则 A 提交后会被游标永久越过。
    以 limit=1 分页：A 未提交时只返回空页（游标不推进）；A 提交后
    先返回 A，再返回 B。
    """
    tx_a = psycopg.connect(pg_dsn, autocommit=False)
    tx_b = psycopg.connect(pg_dsn, autocommit=True)
    try:
        # A 先执行写入 -> 分配较小 xid，但不提交
        _insert(tx_a, 1002, T0, 1111)
        # B 后执行并立即提交 -> xid 较大，已提交可见
        _insert(tx_b, 1002, T1, 2222)

        page = _get(client)
        assert page["items"] == [], "不得返回可能被永久越过的 B"
        assert page["next_cursor"] is None

        # 较早的 A 仍未提交，继续读取也不能越过
        page = _get(client, limit=1)
        assert page["items"] == []

        # A 提交后，按 xid 顺序先 A 后 B
        tx_a.commit()
        first = _get(client, limit=1)
        assert [i["after"]["wh"] for i in first["items"]] == [1111]
        second = _get(client, first["next_cursor"], limit=1)
        assert [i["after"]["wh"] for i in second["items"]] == [2222]
        assert second["has_more"] is False
    finally:
        tx_a.close()
        _cleanup(tx_b, 1002, T0)
        _cleanup(tx_b, 1002, T1)
        tx_b.close()


def test_uncommitted_earlier_change_does_not_block_older_committed_ones(
    client: TestClient, pg_dsn: str
) -> None:
    """未提交的 A 只压住比自己大的 xid，不阻塞此前已完成的变更。"""
    writer = psycopg.connect(pg_dsn, autocommit=True)
    tx_a = psycopg.connect(pg_dsn, autocommit=False)
    try:
        _insert(writer, 1002, T0, 1)
        _insert(tx_a, 1002, T1, 2)  # 更大 xid，未提交

        page = _get(client)
        assert [i["after"]["wh"] for i in page["items"]] == [1]
        assert page["has_more"] is False

        tx_a.rollback()  # 回滚后也不留可读变更
        assert len(_get(client)["items"]) == 1
    finally:
        tx_a.close()
        _cleanup(writer, 1002, T0)
        _cleanup(writer, 1002, T1)
        writer.close()


def test_cursor_never_skips_changes_under_random_commit_order(
    client: TestClient, pg_dsn: str
) -> None:
    """并发写入、乱序提交：持续分页读取，最终必须恰好读到每条变更一次。

    每个写入事务先各自分配 xid（插入但不提交），随后按随机延迟提交；
    读取端在提交进行中反复续读。若游标越过了未提交的较早事务，那条
    变更就会永久缺失，本测试会立即发现。
    """
    import random
    import threading

    writers_count = 8
    start = threading.Barrier(writers_count + 1)
    commits = threading.Barrier(writers_count + 1)
    conns = [psycopg.connect(pg_dsn, autocommit=False) for _ in range(writers_count)]
    wh_values = list(range(2001, 2001 + writers_count))
    # 每行主键不同，写入互不阻塞，只有提交顺序是随机的
    stamps = [f"2026-11-20 {8 + i:02d}:00:00+00" for i in range(writers_count)]

    def write_and_commit(index: int) -> None:
        try:
            _insert(conns[index], 1002, stamps[index], wh_values[index])
            start.wait()
            commits.wait()
            # 后拿到 xid 的事务反而可能先提交
            time.sleep(random.uniform(0, 0.08))
            conns[index].commit()
        except Exception:
            conns[index].rollback()
            raise

    threads = [
        threading.Thread(target=write_and_commit, args=(i,), daemon=True)
        for i in range(writers_count)
    ]
    collected: dict[str, dict] = {}
    cursor: str | None = None
    try:
        for t in threads:
            t.start()
        start.wait()      # 所有事务都已写入并持有 xid
        commits.wait()    # 放行提交
        # 边提交边续读，直到所有写入线程结束
        while any(t.is_alive() for t in threads):
            page = _get(client, cursor, limit=1)
            for item in page["items"]:
                assert item["cursor"] not in collected, "变更被重复返回"
                collected[item["cursor"]] = item
            cursor = page["next_cursor"]
        for t in threads:
            t.join(timeout=5)

        # 排空剩余可读变更
        while True:
            page = _get(client, cursor, limit=2)
            for item in page["items"]:
                assert item["cursor"] not in collected
                collected[item["cursor"]] = item
            if not page["items"]:
                break
            cursor = page["next_cursor"]

        got = {
            item["after"]["wh"]
            for item in collected.values()
            if item["operation"] == "INSERT" and item["after"] is not None
        }
        assert got == set(wh_values), "有变更被游标永久跳过"
        assert len(collected) == writers_count
    finally:
        for conn in conns:
            conn.close()
        writer = psycopg.connect(pg_dsn, autocommit=True)
        for stamp in stamps:
            _cleanup(writer, 1002, stamp)
        writer.close()


def test_savepoint_rollback_leaves_only_committed_part(client: TestClient, pg_dsn: str) -> None:
    """子事务（SAVEPOINT）回滚的变更不可见，已提交部分照常可见。"""
    writer = psycopg.connect(pg_dsn, autocommit=True)
    tx = psycopg.connect(pg_dsn, autocommit=False)
    try:
        _insert(tx, 1002, T0, 10)
        tx.execute("SAVEPOINT sp")
        _insert(tx, 1002, T1, 20)
        tx.execute("ROLLBACK TO SAVEPOINT sp")
        tx.commit()

        items = _get(client)["items"]
        assert [i["after"]["wh"] for i in items] == [10]
    finally:
        tx.close()
        _cleanup(writer, 1002, T0)
        _cleanup(writer, 1002, T1)
        writer.close()


def test_update_moving_meter_reports_both_meters(client: TestClient, pg_dsn: str) -> None:
    writer = psycopg.connect(pg_dsn, autocommit=True)
    try:
        _insert(writer, 1002, T0, 30)
        writer.execute(
            "UPDATE readings SET meter_id = 2002 WHERE meter_id = 1002 "
            "AND read_time = %s::timestamptz",
            (T0,),
        )
        item = _get(client)["items"][-1]
        assert item["operation"] == "UPDATE"
        assert item["before"]["meter_id"] == 1002
        assert item["after"]["meter_id"] == 2002
        assert item["affected_meter_ids"] == [1002, 2002]
    finally:
        _cleanup(writer, 2002, T0)
        _cleanup(writer, 1002, T0)
        writer.close()


def test_multi_row_transaction_is_ordered_and_atomic(client: TestClient, pg_dsn: str) -> None:
    """同一事务的多行变更共享 xid，按写入顺序返回；提交前整批不可见。"""
    tx = psycopg.connect(pg_dsn, autocommit=False)
    writer = psycopg.connect(pg_dsn, autocommit=True)
    try:
        _insert(tx, 1002, T0, 10)
        _insert(tx, 1002, T1, 20)
        assert _get(client)["items"] == []

        tx.commit()
        items = _get(client)["items"]
        assert [i["after"]["wh"] for i in items] == [10, 20]
        xids = {
            base64.urlsafe_b64decode(i["cursor"] + "==").decode().split(":")[0]
            for i in items
        }
        assert len(xids) == 1  # 同一事务的同一 xid
    finally:
        tx.close()
        _cleanup(writer, 1002, T0)
        _cleanup(writer, 1002, T1)
        writer.close()
