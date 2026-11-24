"""SQLite persistence layer.

设计要点：
- WAL 模式，读写可并发；写事务使用 BEGIN IMMEDIATE 串行化。
- 来源表（waybills / weigh_records / price_records）与预演结果表
  （match_stats / monthly_totals / meta）在同一个数据库文件内，
  来源与结果在同一事务里一起提交，一起生效。
- 预演结果是预先物化物化的结果表，查询只走结果表，不读来源明细、
  不展开计费行。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = Path(
    os.environ.get("FREIGHT_DB_PATH", "/data/freight.db")
)

# WAL 下需要开启 foreign_keys（连接级设置）。
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
)


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开一个配置好的连接（调用方负责关闭）。"""
    path = Path(db_path) if db_path is not None else _current_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


def get_connection() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个连接。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式只读快照事务。

    同一次概览查询（月合计 + 异常分页）在同一个已提交状态上读取：
    BEGIN 之后写者无法改变本事务看到的快照，两条语句看到同一版本。
    """
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()  # 只读事务，rollback 即结束并释放快照


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务：BEGIN IMMEDIATE 拿写锁，提交/回滚原子生效。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS waybills (
    waybill_no   TEXT PRIMARY KEY,
    train_no     TEXT NOT NULL,
    origin       TEXT NOT NULL,
    departure_dt TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weigh_records (
    record_no  TEXT PRIMARY KEY,
    train_no   TEXT NOT NULL,
    weight_kg  REAL NOT NULL,
    note       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS price_records (
    record_no   TEXT PRIMARY KEY,
    origin      TEXT NOT NULL,
    valid_from  TEXT NOT NULL,
    valid_to    TEXT NOT NULL,
    price_per_t REAL NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    CHECK (valid_from <= valid_to)
);

-- 预演结果（每运单一行，聚合命中数，不展开计费行）
CREATE TABLE IF NOT EXISTS match_stats (
    waybill_no     TEXT PRIMARY KEY,
    departure_mon  TEXT NOT NULL,
    weigh_hits     INTEGER NOT NULL,
    price_hits     INTEGER NOT NULL,
    generated_rows INTEGER NOT NULL,
    is_anomaly     INTEGER NOT NULL
);

-- 预演结果（每月一行的合计，按发车月）
CREATE TABLE IF NOT EXISTS monthly_totals (
    departure_mon  TEXT PRIMARY KEY,
    generated_rows INTEGER NOT NULL
);

-- 数据版本号，每次成功写入 +1，用于确认查询读取的已提交状态
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ------------------------------------------------------------------ #
-- 运费对账（实收清单）结果表
--
-- recon_receipts：按月提交的实收清单，按（运单、过磅单号、价目号）
--   去重后每组合一行；line_count 记录原始行数（>=2 的部分即重复行）。
--   is_expected 是“该组合在当前来源下是否应收”的物化标记。
-- recon_tickets：有实收清单的运单汇总（用于来源更正时按增量维护）。
-- recon_diffs：只保存“有差异”的运单，供按运单号翻页时直接定位，
--   不包含已核平运单，因此翻页不会遍历已核平的票。
--
-- 注意：这里只存组合计数，不存“应收组合”的笛卡尔展开行。
-- 应收组合总数由预演结果 match_stats 的 weigh_hits * price_hits 得出。
-- ------------------------------------------------------------------ #
CREATE TABLE IF NOT EXISTS recon_receipts (
    departure_mon    TEXT NOT NULL,
    waybill_no       TEXT NOT NULL,
    weigh_record_no  TEXT NOT NULL,
    price_record_no  TEXT NOT NULL,
    line_count       INTEGER NOT NULL,
    is_expected      INTEGER NOT NULL,
    PRIMARY KEY (departure_mon, waybill_no, weigh_record_no, price_record_no)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS recon_tickets (
    departure_mon        TEXT NOT NULL,
    waybill_no           TEXT NOT NULL,
    expected_received    INTEGER NOT NULL,
    unexpected_received  INTEGER NOT NULL,
    duplicate_rows       INTEGER NOT NULL,
    receipt_rows         INTEGER NOT NULL,
    PRIMARY KEY (departure_mon, waybill_no)
) WITHOUT ROWID;

-- 已提交过实收清单的发车月。只有这些月份才产生“整票未收”的差异票；
-- 未提交的月份不臆造 0 实收。
CREATE TABLE IF NOT EXISTS recon_months (
    departure_mon TEXT PRIMARY KEY
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS recon_diffs (
    departure_mon        TEXT NOT NULL,
    waybill_no           TEXT NOT NULL,
    expected_received    INTEGER NOT NULL,
    unexpected_received  INTEGER NOT NULL,
    duplicate_rows       INTEGER NOT NULL,
    receipt_rows         INTEGER NOT NULL,
    PRIMARY KEY (departure_mon, waybill_no)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_waybills_train
    ON waybills(train_no);
CREATE INDEX IF NOT EXISTS idx_waybills_origin_dt
    ON waybills(origin, departure_dt);
CREATE INDEX IF NOT EXISTS idx_weigh_train
    ON weigh_records(train_no);
CREATE INDEX IF NOT EXISTS idx_price_origin_window
    ON price_records(origin, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_match_anomaly
    ON match_stats(is_anomaly, departure_mon, waybill_no);
CREATE INDEX IF NOT EXISTS idx_match_month
    ON match_stats(departure_mon);
CREATE INDEX IF NOT EXISTS idx_recon_receipts_waybill
    ON recon_receipts(waybill_no, departure_mon);
CREATE INDEX IF NOT EXISTS idx_recon_receipts_weigh
    ON recon_receipts(weigh_record_no);
CREATE INDEX IF NOT EXISTS idx_recon_receipts_price
    ON recon_receipts(price_record_no);
"""


def init_db(db_path: str | Path | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('version', '0')"
        )
        conn.commit()
    finally:
        conn.close()


META_VERSION = "version"


def get_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (META_VERSION,)
    ).fetchone()
    return int(row["value"]) if row else 0


def bump_version(conn: sqlite3.Connection) -> int:
    new_version = get_version(conn) + 1
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = ?",
        (str(new_version), META_VERSION),
    )
    return new_version


# 允许测试通过环境变量/依赖覆盖切换数据库路径。
_db_path: Path | None = None


def set_db_path(path: str | Path | None) -> None:
    global _db_path
    _db_path = Path(path) if path is not None else None


def _current_db_path() -> Path:
    return _db_path if _db_path is not None else DEFAULT_DB_PATH
