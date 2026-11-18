# 电表日结 SQL 核对 API

数仓工程师改写电表日结的关联 SQL 后，用本服务确认读数**没有漏计或重复计入**。
提交两条只读 `SELECT`，服务在同一已提交快照上并行读取两侧结果，按
**行内容 + 出现次数**做多重集核对（忽略行序，`NULL` 与空串视为不同），
返回一致结论或最多 20 种差异行及两侧次数。

同时提供 `/readings/changes` 分页接口：readings 的任何 INSERT / UPDATE /
DELETE 都会被事务内捕获，按提交顺序安全下发；每条变更给出变更前后的表号、
时刻、累计 Wh，以及需要重算的表号与 UTC 日期（见第 3 节）。

## 1. 快速启动

```bash
docker compose up --build
# API:   http://localhost:8000
# 文档:  http://localhost:8000/docs   （Swagger UI，可直接发请求演示）
```

启动后 PostgreSQL 自动执行 `sql/init.sql`，建好 `meters` / `readings`
源表与变更捕获结构，并写入示例数据；文件末尾附有可直接粘贴到 `/docs` 的
两条等价示例查询。变更日志为空时应用会生成 3 条演示变更，打开 `/docs`
即可演示 `GET /readings/changes`。

停止并清理：

```bash
docker compose down -v
```

## 2. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET`  | `/health` | 存活探针，不触碰数据库 |
| `GET`  | `/readings/changes` | 分页读取 readings 变更（游标安全，见第 3 节） |
| `POST` | `/jobs` | 提交核对任务（同时只允许一组运行，占用中返回 `409`） |
| `GET`  | `/jobs` | 列出近期任务（含终态），状态查询始终可用 |
| `GET`  | `/jobs/{job_id}` | 查询单个任务状态与结论 |
| `POST` | `/jobs/{job_id}/cancel` | 请求取消，及时受理 |

任务状态：`pending → running →（cancelling）→ succeeded | failed | cancelled | timeout`。
失败、取消、超时一律 **不发布比较结论**（`result` 为 `null`），仅给出原因。

### 请求示例

```bash
curl -X POST http://localhost:8000/jobs -H 'Content-Type: application/json' -d '{
  "left_sql":  "SELECT meter_id::bigint, wh::bigint FROM readings",
  "right_sql": "SELECT meter_id::bigint, wh::bigint FROM readings",
  "name": "daily-settlement-v2"
}'
```

### 结论结构（成功时）

```json
{
  "status": "succeeded",
  "result": {
    "identical": false,
    "columns": ["meter_id", "wh"],
    "column_types": ["bigint", "bigint"],
    "left_row_total": 504013,
    "right_row_total": 504012,
    "matched_group_count": 504011,
    "diff_group_total": 1,
    "diffs_shown": 1,
    "diffs": [
      {"values": [1001, 1150], "left_count": 2, "right_count": 1}
    ]
  }
}
```

## 3. 读数变更流 `/readings/changes`

下游日结库按提交顺序消费 readings 的变更，重算受影响的表号与 UTC 日期。

启用方式：服务启动时幂等安装 `reading_changes` 表与 `readings` 上的
行级触发器（`sql/init.sql` 也预建了同样的结构）。此后**无论变更来自本服务、
psql 还是任何独立 SQL 会话**，同一事务内的 INSERT / UPDATE / DELETE 都会
落一条变更记录；触发器安装前的历史变更不回填。

### 请求与响应

```bash
curl 'http://localhost:8000/readings/changes?limit=2'
curl 'http://localhost:8000/readings/changes?limit=2&cursor=<上一页的 next_cursor>'
```

- `limit`：每页 1～100，默认 50，越界返回 `422`；
- `cursor`：上页返回的 `next_cursor`，省略表示从头读；非法游标返回 `400`。

```json
{
  "items": [
    {
      "cursor": "OTQ2OjY",
      "operation": "UPDATE",
      "changed_at": "2026-11-18T00:55:12.345678+00:00",
      "before": {"meter_id": 1001, "read_time": "2026-11-15T09:00:00+00:00", "wh": 1150},
      "after":  {"meter_id": 1001, "read_time": "2026-11-15T09:00:00+00:00", "wh": 1180},
      "affected_meter_ids": [1001],
      "affected_utc_dates": ["2026-11-15"]
    }
  ],
  "next_cursor": "OTQ2OjY",
  "has_more": false
}
```

- `before` / `after`：变更前后的表号、时刻（UTC）、累计 Wh；INSERT 的
  `before` 与 DELETE 的 `after` 为 `null`；
- `affected_meter_ids` / `affected_utc_dates`：需要重算的表号与 UTC 日期
  （前后两端的并集，升序；跨 UTC 日期改写时刻会同时给出两个日期）；
- `next_cursor`：拿它续读下一页。`has_more` 为 `false` 只表示“此刻没有更多
  **已可读**的变更”，稍后仍可能有新变更产生。

### 游标为什么不会漏读（关键交错）

事务 ID（xid）在**第一次写入**时分配，早于提交时刻。若只按 xid 排序读取，
会出现：B（较大 xid）先提交并被返回，A（较小 xid）稍后才提交，续读游标已
越过 A，A 便永久漏读。

因此服务只返回 `change_xid < pg_snapshot_xmin(pg_current_snapshot())` 的变更：

- 尚未提交的较早事务会把 `xmin` 压在它的 xid 上，所有更大的 xid 都不会先
  被返回，游标不会越过它；
- 已提交（按 MVCC 可见）的较小 xid 照常返回，不被无谓阻塞；
- 回滚事务的记录在读取快照下不可见，不留可读变更；
- 同一事务的多行变更共享 xid，按写入顺序（`change_id`）返回，整批一起可见；
- 空页 `next_cursor` 原样返回请求游标（从头读且为空则为 `null`），游标不推进。

这是“提交顺序”的安全下界，代价是：若某个长事务一直不提交，比它晚的事务
要等它结束后才会出现在结果里——这正是避免永久漏读必须付出的代价。

### 已知边界

- 行级触发器只覆盖 `INSERT / UPDATE / DELETE`（含 `COPY` 行）；
  `TRUNCATE readings` 不触发行触发器，不会产生逐行变更；
- 启用（触发器安装）之前的历史变更不回填；服务启动时以
  `CREATE OR REPLACE` 幂等安装，已有的库升级后自动开始捕获；
- 读取使用独立连接逐页查询；数据库不可用时返回 `503`（而不是伪装成空页，
  避免下游误以为已经追平）；
- 变更日志目前不自动清理/归档，长期运行需要按 `changed_at` 或 `change_xid`
  制定保留策略。

### 演示数据

`docker-compose.yml` 默认 `SEED_DEMO_CHANGES=true`：变更日志为空时，启动时
在同一事务内插入、更正并删除一条演示读数后提交，因此 `/docs` 打开即可看到
3 条 INSERT / UPDATE / DELETE 示例，而 `readings` 的最终内容不受影响。
生产环境请设为 `false`。

## 4. 对提交 SQL 的约束

- 必须是**单条只读 SELECT**（允许 `WITH` / `UNION` / `INTERSECT` / `EXCEPT`），
  静态解析拒绝多语句与任何写操作 / DDL / `SELECT INTO` / `LOCK` / `CALL` 等；
- 结果列只允许 `bigint` 与 `text`，两侧**列数相同、对应位置类型相同**，
  否则本轮失败且不给出比较结论；
- 数据库连接再叠加一层纵深防御：`READ ONLY` 事务、`statement_timeout`、
  `lock_timeout`，即使静态校验被绕过也无法写入或长时间持锁。

## 5. 关键设计

### 同一已提交快照，源表仍可更正

每条任务开三条独立连接：快照连接执行
`BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY` + `pg_export_snapshot()`，
两条读取连接分别 `SET TRANSACTION SNAPSHOT` 绑定**同一个快照**。
核对期间其他会话对 `readings` / `meters` 的更正照常提交，但对本轮不可见。

### 内存有界的外部归并（不全量载入、不靠哈希判等）

1. 命名服务端游标按 `itersize` 分批拉取，客户端从不缓存整表；
2. 每 `SORT_CHUNK_ROWS`（默认 2 万）行在进程内排序，逐行流式 pickle 落盘为一个 run；
3. 归并阶段对两侧 run 做单轮 k 路堆归并（堆中每流只驻留一行），
   键随行走，按全序键聚合等值组后比较两侧次数；
4. 差异明细最多保留 20 种，但总行数、差异组总数等计数覆盖全量数据；
5. 全程不使用整表哈希作为判等依据。

### 单名额、30 秒时限与终止语义

- 进程内单名额信号量：全局同时只运行一组，冲突返回 `409`；
- 看门狗按 `JOB_TIMEOUT_SECONDS`（默认 30）判定超时；
- 取消/超时/任一侧失败后：`conn.cancel()` 请求中止本轮全部 SQL，
  必要时 `pg_terminate_backend` 兜底；
- 严格按顺序收尾：**中止 SQL → 关闭连接 → 清空暂存目录 → 释放名额 → 才落终态**，
  因此状态查询一旦看到终态，名额必然已经释放、暂存必然已经清空。

## 6. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PG_DSN` | `postgresql://recon:recon@postgres:5432/recon` | 数据库连接串 |
| `JOB_TIMEOUT_SECONDS` | `30` | 单轮硬时限 |
| `PG_STATEMENT_TIMEOUT_MS` | `28000` | 服务端单语句超时（留 2s 收尾余量） |
| `SORT_CHUNK_ROWS` | `20000` | 单个排序块行数 |
| `MAX_DIFF_TYPES` | `20` | 差异展示上限 |
| `WORK_DIR` | `/tmp/recon-work` | 外部排序暂存目录（独立卷） |
| `SEED_DEMO_CHANGES` | `false`（compose 中为 `true`） | 变更日志为空时生成 3 条演示变更 |

## 7. 本地自测

```bash
# 单元 + 集成测试（集成测试需要一个 PostgreSQL；可用 PG_DSN 指定）
python -m pytest tests/ -q

# 50 万行级端到端压测（内存峰值、暂存占用、结论正确性、清理与名额释放）
python scripts/load_test.py
```

实测（受限沙箱环境，容器内通常更快）：两侧各 504,012 行、
含一次重复计入差异，端到端约 16 秒，进程峰值 RSS 约 68 MiB，
暂存峰值约 44 MiB，结束后暂存目录清空、名额释放。

`tests/test_changes.py` 覆盖变更流：三类变更的载荷、跨 UTC 日期、
非 UTC 时区归日、分页与空页游标、回滚（含 SAVEPOINT 部分回滚）不可见、
多行事务原子且有序，以及**交错提交**（较小 xid 未提交时不得越过、提交后
按 xid 顺序补读；8 个事务并发写入、随机顺序提交时持续分页恰好读到每条一次）。

## 8. 目录结构

```
app/
  config.py          运行期配置
  sqlvalidation.py   只读 SELECT 静态校验
  db.py              快照会话、只读连接、命名游标流式拉取、列类型闸门
  external_merge.py  流式 run、k 路归并、多重集核对
  manager.py         单名额编排、看门狗、取消/超时/失败的统一收尾
  change_capture.py  变更日志表、行级触发器与幂等安装（含演示数据）
  changes.py         变更分页读取、xmin 安全下界、游标编解码
  models.py          API 模型
  main.py            FastAPI 路由
sql/init.sql         源表、变更捕获结构、示例数据、示例查询
scripts/             压测数据生成与负载脚本
tests/               单元与集成测试
docker-compose.yml   应用（128MiB 上限）+ PostgreSQL
Dockerfile
```
