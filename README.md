# 运费对账（Freight Reconciliation）

一张运单关联 **过磅单** 与 **价目** 两张来源表后，可能漏计（某侧命中 0 条）
也可能冒出多条计费明细（某侧命中多条，两次内连接产生笛卡尔积）。本服务用
FastAPI + SQLite 对正式入仓前的匹配结果做“预演”，只产出每票命中数、
每月内连接总行数和异常清单，**全程不展开计费行**。

在预演之上另提供 **运费对账**：按发车月提交实收清单，只列有差异的票，
报告应收未收、不应收却实收与重复行数。

## 匹配规则

| 来源 | 记录号 | 匹配键 | 操作 |
| --- | --- | --- | --- |
| 过磅单 `weigh_records` | `record_no` 唯一 | 车次 `train_no` | 补录 / 替换 / 撤回 |
| 价目 `price_records` | `record_no` 唯一 | 始发站 `origin` + 发车日落入 `[valid_from, valid_to]`（**含首尾两日**） | 补录 / 替换 / 撤回 |
| 运单 `waybills` | `waybill_no` 唯一 | 车次、始发站、发车日 | `/docs` 一次批量导入 |

- 每票运单的两次内连接生成行数 = `过磅命中数 × 价目命中数`。
- 任一侧命中数 ≠ 1 即异常（0 = 漏配，>1 = 重复），异常页给出两侧各自命中数。
- 月合计按发车月（`YYYY-MM`）统计全部运单的内连接总行数。

## 快速启动（docker compose）

```bash
docker compose up --build
# 打开交互式文档，可直接导入运单、补录/替换/撤回来源：
#   http://localhost:8000/docs
# 数据库持久化在命名卷 freight-data（/data/freight.db，WAL 模式）
```

本地直跑（无 Docker 时）：

```bash
pip install -r requirements.txt
FREIGHT_DB_PATH=./data/freight.db uvicorn app.main:app --reload
```

## 接口一览（均在 /docs 可见可调）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/waybills/import` | 批量导入运单；批次内/库内撞号整批 409 回滚 |
| PUT | `/weigh/records/{record_no}` | 过磅单补录或替换（upsert） |
| DELETE | `/weigh/records/{record_no}` | 撤回过磅单，不存在返回 404 |
| PUT | `/price/records/{record_no}` | 价目补录或替换（含窗口倒置校验） |
| DELETE | `/price/records/{record_no}` | 撤还价目 |
| GET | `/rehearsal/months/{mon}/total` | 月内连接总行数（读预聚合表） |
| GET | `/rehearsal/anomalies` | 异常运单键集分页（`cursor`/`limit`，可按月过滤） |
| GET | `/rehearsal/months/{mon}/overview` | **同一只读快照**返回月合计 + 异常页 + 版本号 |
| GET | `/rehearsal/waybills/{no}` | 单票预演明细 |
| POST | `/reconciliation/months/{mon}/receipts` | **提交该发车月实收清单**（整月替换） |
| GET | `/reconciliation/months/{mon}/diffs` | 差异票按运单号键集分页（`cursor`/`limit`） |
| GET | `/reconciliation/months/{mon}/summary` | 整月差异汇总（少收 / 不该收 / 重复） |
| POST | `/admin/rebuild` | 全量重建（核对用，正常写入只走增量） |
| GET | `/health` | 数据版本号与库路径 |

写入响应统一返回 `version / affected_waybills / affected_months / detail`。
来源写入的 `detail` 额外返回 `rechecked_combos`（重查的实收组合行数）与
`reconciliation_changed`（实际改写的差异票数），可直接核对增量范围。

## 运费对账

### 提交实收清单

`POST /reconciliation/months/2026-03/receipts`

```json
{"lines": [
  {"waybill_no": "W1", "weigh_record_no": "G1", "price_record_no": "P1"},
  {"waybill_no": "W1", "weigh_record_no": "G1", "price_record_no": "P1"}
]}
```

- 每行填 **运单号、过磅单号、价目号**；按运单号 + 两个来源单号组成组合。
- 运单必须已导入且属于该发车月（否则 422，整批回滚）；来源单号**不校验存在**，
  已撤回的过磅单/价目号照收，只在判定时视为“不应收到”。
- 同一组合重复出现只落一行，`line_count` 记原始行数；**从第二次出现起**
  计入重复行数。
- 同一发车月再次提交 = **整月替换**：新清单与新差异一起替换旧清单与旧差异；
  校验失败时整批回滚、旧结果仍可查。`lines: []` 表示当月显式零实收。

### 差异口径（每票）

| 字段 | 含义 |
| --- | --- |
| `expected_total` | 该票应收组合数 = 预演生成行数（过磅命中 × 价目命中） |
| `expected_received` | 实收清单中命中的**应收**组合数（按组合去重） |
| `missing_expected` | 应收却未收到的组合数 = `expected_total - expected_received` |
| `unexpected_received` | 不应收到却实收的**不同组合数**（来源已撤、车次/始发站/窗口不匹配、单号不存在等） |
| `duplicate_rows` | 重复行数（同一组合第二次及以后） |
| `received_rows` | 清单中该票的原始行数（含重复） |

整票未收（清单里一行都没有）时，`missing_expected = expected_total`，
该值直接由预演结果推导，因此随预演更新；若来源更正前后该票都仍少收，
**不另行改写该票的差异记录**（UPSERT 的 `WHERE` 聚合比较使其不落写）。

### 增量与查询约束

- **来源改动只重查引用该记录号的实收组合**：改过磅单车次 / 撤回，只重查
  `weigh_record_no` 等于该记录号的行；改价目 / 撤回同理按 `price_record_no`
  定位。其它受影响运单只按预演结果调整“是否差异”，不重算清单。
- **翻页直接定位差异票**：`recon_diffs` 只保存有差异的票，主键为
  `(departure_mon, waybill_no)`；`WHERE departure_mon = ? AND waybill_no > ?`
  + `ORDER BY waybill_no LIMIT ?` 实测为 `SEARCH ... USING PRIMARY KEY`，
  读取行数只随页长增长，不遍历已核平票，也不访问清单/来源表。
- **不展开应收组合**：库中没有应收明细表；应收总数是预演结果的一列乘积，
  判定“某组合是否应收”只用两次 `EXISTS`，不物化笛卡尔积。
- 只有提交过清单的月份才产生差异票（`recon_months`），未提交月份不臆造
  整票未收；提交后当月新增的运单会以整票未收进入差异票。

## 关键设计如何满足需求

1. **增量重算**：写入前记录旧匹配条件（过磅的旧/新车次、价目的旧/新窗口），
   只定位这些条件涉及的运单；月份只取这些运单的发车月（价目改窗口时为新旧
   窗口并集）。响应里回传实际重算范围，可验证没有全量重算。
2. **来源与结果一起生效，失败可查原结果**：来源写入与 `match_stats` /
   `monthly_totals` 刷新、版本号 +1 在同一个 `BEGIN IMMEDIATE` 事务内提交；
   任何错误整体回滚，旧版本结果保持可读（有测试覆盖撞号、撤回不存在记录号）。
3. **同次查询同一已提交状态**：overview 在一个显式只读快照事务里执行两条查询，
   并返回该快照的 `version`；WAL 模式下读不阻塞写、写不阻塞读（有并发线程实测）。
4. **合计不逐单扫描**：`monthly_totals` 是预聚合表，月合计是主键索引点查；
   刷新月份时用一条 `GROUP BY` 集合语句，不按单循环。
5. **异常页只读异常运单**：`idx_match_anomaly(is_anomaly, departure_mon,
   waybill_no)` 覆盖索引，`EXPLAIN QUERY PLAN` 实测为 `SEARCH ... COVERING
   INDEX`，不碰三张来源表。
6. **查询不读来源明细、不展开计费行**：库中没有计费明细表，`match_stats`
   每票仅一行（命中数 + 乘积行数）；所有读接口只访问结果表。

## 测试

```bash
python3 -m pytest -q          # 29 个端到端用例，真实 SQLite + TestClient
```

覆盖：1:1 正常匹配、漏配与重复并存、有效期首尾边界、过磅改车次/价目改窗口的
增量范围、原子回滚、撤回失败保旧结果、键集分页、跨月合计、全量重建一致性、
版本号单调、查询计划只走结果表索引、输入校验；对账部分覆盖组合去重与重复计数、
少收/不该收/重复三类差异、整月替换、空清单、撤回来源照收、运单已导入且属当月的
校验、只重查引用组合、整票未收随预演更新且不重复改写、大库翻页 VM 指令数不随
已核平票增长、差异查询不碰清单/来源、更正后夹在核平票中的差异票、重建一致性。

## 目录

```
app/            FastAPI 应用
  db.py           连接/模式（来源表 + 预演/对账结果表）
  models.py       请求/响应模型
  service.py      来源写入 + 预演增量重算
  reconciliation.py 实收清单、差异维护与分页
  errors.py       服务层异常
  main.py         路由
tests/          pytest 端到端用例
Dockerfile / docker-compose.yml / requirements.txt
```

## 实现状态

### 已实现

- 批量运单导入（唯一单号、车次、始发站、发车日，撞号整批拒绝）。
- 过磅单/价目按记录号补录、替换、撤回；价目有效期含首尾两日的日期窗口匹配。
- 每票两侧命中数、内连接行数、异常标记；按月合计；异常运单键集分页与按月过滤。
- 仅重算新旧匹配条件涉及运单与月份的增量机制；`/admin/rebuild` 全量核对入口。
- 来源与结果同事务原子提交、版本号、只读快照一致性、WAL 并发。
- 结果表 + 覆盖索引支撑的只读查询路径，无计费明细表。
- 运费对账：按发车月提交实收清单（运单号 + 过磅单号 + 价目号），组合去重、
  第二次起计重复、撤回来源照收、整月替换、失败保旧结果。
- 差异票按运单号键集分页，分别报告应收未收组合数、不应收却实收组合数、
  重复行数；整月汇总接口。
- 来源补录 / 更正 / 撤回只重查引用该记录号的实收组合；整票未收少收数随预演
  更新，更正前后都少收时不另行改写差异记录。
- docker compose 一键启动、`/docs` 全接口可调、健康检查与持久化卷。

### 已实测

- 29 个 pytest 用例全部通过；真实 uvicorn 进程手工走通提交清单 → 翻页 →
  撤回来源 → 差异变化（W1 重复 1 行；撤回 G1 后仅 1 个组合重查、W1 转为不应收）。
- `EXPLAIN QUERY PLAN` 确认月合计点查、异常页覆盖索引、单票主键索引；
  差异页为 `SEARCH recon_diffs USING PRIMARY KEY (departure_mon=? AND
  waybill_no>?)`，未触及 `recon_receipts`/来源表。
- 大库实测：20 万张已核平票 + 3 张差异票，翻页 1 行与 3 行的 SQLite VM
  指令数都在 5k 以内（不随已核平票数增长；用例内以进度回调计数断言）。
- 自测“来源更正后夹在已核平运单中的差异票”：W1..W5 全部核平 → 更正 G3 →
  差异页仅返回 W3（页长 1 也直接命中）→ 改回后差异消失。
- 失败路径实测：撞号导入 409 全回滚、撤回不存在记录 404、清单含未导入 /
  错月运单 422，均保持版本号与旧结果不变。

### 未完成 / 边界说明

- 本环境未安装 Docker 守护进程，compose 文件仅做了结构静态校验，未实际
  `docker compose up` 构建运行（Dockerfile 基于 python:3.11-slim，步骤与本地
  验证过的安装方式一致）。
- 未做认证/鉴权与多租户隔离，定位为内网预演工具。
- 未实现运单本身的修改/删除接口（需求只要求一次导入）；需要时可复用同一套
  增量重算路径。
- 分页为运单号键集分页（稳定、可扩展），未提供 offset 跳页。
- 金额未入模：价目表带 `price_per_t`，但对账只统计组合数与行数，不生成金额
  明细，符合“不展开计费行”的约束。
- 对账差异页按月查询（发车月为必填路径参数），未提供跨月合并分页。
