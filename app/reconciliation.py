"""运费对账：按发车月提交实收清单，维护“实收 vs 应收”差异。

关键约束（对应需求）：
1. 实收清单按发车月整月替换：一个（运单号、过磅单号、价目号）组合只落一行，
   原始行数保存在 line_count；从第二次出现起计为重复行。
2. 清单只校验运单已导入且属于当月；来源单号是否仍在（已撤回）不校验，
   撤回的来源单号照收，只是该组合会被判为“不应收到”。
3. 差异票只保存在 recon_diffs（已核平的票不落表），按运单号分页直接走
   主键 / waybill_no 索引，读取的运单数只随页长增长，不遍历已核平票，
   分页查询也不碰清单与来源表。
4. 应收组合总数取预演结果 match_stats.generated_rows（= 过磅命中数 ×
   价目命中数），全程不展开应收组合。
5. 来源补录 / 更正 / 撤回时只重查引用该记录号的实收组合；整票未收的
   少收数直接由预演结果推导，因此随预演更新；若更正前后都仍少收，
   不另行改写该票的差异记录（只在“是否差异”或聚合计数变化时增删改）。
6. 只有提交过实收清单的月份（recon_months）才产生差异票；未提交的月份
   不会凭空把全部运单当成整票未收。
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator

from . import db
from .errors import RehearsalError
from .models import ReceiptLine

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# 一个实收组合是否属于“当前来源下的应收组合”。
# 只做存在性判断，不展开乘积累积的应收组合。
_IS_EXPECTED = """
CASE WHEN
    EXISTS (
        SELECT 1
          FROM weigh_records g
          JOIN waybills w ON w.waybill_no = recon_receipts.waybill_no
         WHERE g.record_no = recon_receipts.weigh_record_no
           AND g.train_no = w.train_no
    )
    AND EXISTS (
        SELECT 1
          FROM price_records p
          JOIN waybills w ON w.waybill_no = recon_receipts.waybill_no
         WHERE p.record_no = recon_receipts.price_record_no
           AND p.origin = w.origin
           AND w.departure_dt BETWEEN p.valid_from AND p.valid_to
    )
THEN 1 ELSE 0 END
"""


def _chunks(values: Iterable[str], size: int = 800) -> Iterator[list[str]]:
    buf: list[str] = []
    for value in values:
        buf.append(value)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def _require_month(mon: str) -> None:
    if not _MONTH_RE.match(mon):
        raise RehearsalError(f"发车月格式应为 YYYY-MM: {mon}")


# --------------------------------------------------------------------------- #
# 清单提交
# --------------------------------------------------------------------------- #


def _validate_waybills(
    conn: sqlite3.Connection, mon: str, waybill_nos: Iterable[str]
) -> None:
    requested = set(waybill_nos)
    found: dict[str, str] = {}
    for chunk in _chunks(sorted(requested)):
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT waybill_no, substr(departure_dt, 1, 7) AS mon "
            f"FROM waybills WHERE waybill_no IN ({marks})",
            tuple(chunk),
        ).fetchall()
        for row in rows:
            found[row["waybill_no"]] = row["mon"]

    missing = sorted(no for no in requested if no not in found)
    wrong_month = sorted(
        no for no in requested if no in found and found[no] != mon
    )
    if missing:
        raise RehearsalError(
            "实收清单中的运单尚未导入: " + ", ".join(missing[:10])
        )
    if wrong_month:
        raise RehearsalError(
            f"实收清单中的运单不属于发车月 {mon}: "
            + ", ".join(wrong_month[:10])
        )


def _set_expected(
    conn: sqlite3.Connection, where_sql: str = "", params: tuple = ()
) -> None:
    conn.execute(
        f"UPDATE recon_receipts SET is_expected = {_IS_EXPECTED} {where_sql}",
        params,
    )


_TICKET_AGG_SQL = """
SELECT r.departure_mon, r.waybill_no,
       COALESCE(SUM(r.is_expected), 0) AS expected_received,
       COALESCE(SUM(CASE WHEN r.is_expected = 0 THEN 1 ELSE 0 END), 0)
           AS unexpected_received,
       COALESCE(SUM(r.line_count - 1), 0) AS duplicate_rows,
       COALESCE(SUM(r.line_count), 0) AS receipt_rows
  FROM recon_receipts r
  JOIN temp.recon_rechecked c
    ON c.departure_mon = r.departure_mon
   AND c.waybill_no = r.waybill_no
 GROUP BY r.departure_mon, r.waybill_no
"""


def _rebuild_tickets(conn: sqlite3.Connection, mon: str) -> None:
    conn.execute("DELETE FROM recon_tickets WHERE departure_mon = ?", (mon,))
    conn.execute(
        """
        INSERT INTO recon_tickets(
            departure_mon, waybill_no, expected_received,
            unexpected_received, duplicate_rows, receipt_rows)
        SELECT departure_mon, waybill_no,
               SUM(is_expected),
               SUM(CASE WHEN is_expected = 0 THEN 1 ELSE 0 END),
               SUM(line_count - 1),
               SUM(line_count)
          FROM recon_receipts
         WHERE departure_mon = ?
         GROUP BY departure_mon, waybill_no
        """,
        (mon,),
    )


def _is_diff_expr(ms: str, t: str) -> str:
    """某票“是否差异”的 SQL 表达式（只读预演结果与实收集合计数）。"""
    return (
        f"({ms}.generated_rows - COALESCE({t}.expected_received, 0) > 0"
        f" OR COALESCE({t}.unexpected_received, 0) > 0"
        f" OR COALESCE({t}.duplicate_rows, 0) > 0)"
    )


_DIFF_SELECT = (
    "SELECT ms.departure_mon, ms.waybill_no,"
    "       COALESCE(t.expected_received, 0),"
    "       COALESCE(t.unexpected_received, 0),"
    "       COALESCE(t.duplicate_rows, 0),"
    "       COALESCE(t.receipt_rows, 0)"
    "  FROM match_stats ms"
    "  JOIN recon_months rm ON rm.departure_mon = ms.departure_mon"
    "  LEFT JOIN recon_tickets t ON t.departure_mon = ms.departure_mon"
    "                           AND t.waybill_no = ms.waybill_no"
)


def _rebuild_diffs(conn: sqlite3.Connection, mon: str) -> None:
    conn.execute("DELETE FROM recon_diffs WHERE departure_mon = ?", (mon,))
    conn.execute(
        "INSERT INTO recon_diffs("
        "departure_mon, waybill_no, expected_received, unexpected_received, "
        "duplicate_rows, receipt_rows) "
        + _DIFF_SELECT
        + " WHERE ms.departure_mon = ? AND rm.departure_mon = ?"
        + f" AND {_is_diff_expr('ms', 't')}",
        (mon, mon),
    )


_DIFF_TARGETS = (
    "SELECT ms.departure_mon, ms.waybill_no,"
    "       COALESCE(t.expected_received, 0),"
    "       COALESCE(t.unexpected_received, 0),"
    "       COALESCE(t.duplicate_rows, 0),"
    "       COALESCE(t.receipt_rows, 0)"
    "  FROM match_stats ms"
    "  JOIN recon_targets tg ON tg.departure_mon = ms.departure_mon"
    "                       AND tg.waybill_no = ms.waybill_no"
    "  LEFT JOIN recon_tickets t ON t.departure_mon = ms.departure_mon"
    "                           AND t.waybill_no = ms.waybill_no"
)


def submit_receipts(
    conn: sqlite3.Connection, mon: str, items: list[ReceiptLine]
) -> dict:
    """整月替换实收清单及差异；校验失败时整批回滚，旧结果保持可读。"""
    _require_month(mon)
    _validate_waybills(conn, mon, (it.waybill_no for it in items))

    # 同一组合重复出现：只落一行，line_count 记录原始行数。
    counts = Counter(
        (it.waybill_no, it.weigh_record_no, it.price_record_no)
        for it in items
    )

    conn.execute("DELETE FROM recon_receipts WHERE departure_mon = ?", (mon,))
    conn.execute("DELETE FROM recon_tickets WHERE departure_mon = ?", (mon,))
    conn.execute("DELETE FROM recon_diffs WHERE departure_mon = ?", (mon,))
    conn.execute(
        "INSERT OR IGNORE INTO recon_months(departure_mon) VALUES (?)", (mon,)
    )
    conn.executemany(
        "INSERT INTO recon_receipts("
        "departure_mon, waybill_no, weigh_record_no, price_record_no, "
        "line_count, is_expected) VALUES (?, ?, ?, ?, ?, 0)",
        [
            (mon, waybill_no, weigh_no, price_no, line_count)
            for (
                waybill_no,
                weigh_no,
                price_no,
            ), line_count in counts.items()
        ],
    )
    _set_expected(conn, "WHERE departure_mon = ?", (mon,))
    _rebuild_tickets(conn, mon)
    _rebuild_diffs(conn, mon)

    version = db.bump_version(conn)
    diff_tickets = conn.execute(
        "SELECT COUNT(*) AS n FROM recon_diffs WHERE departure_mon = ?", (mon,)
    ).fetchone()["n"]
    duplicate_rows = sum(count - 1 for count in counts.values())
    return {
        "version": version,
        "affected_waybills": len({it.waybill_no for it in items}),
        "affected_months": [mon],
        "detail": {
            "departure_mon": mon,
            "submitted_rows": len(items),
            "distinct_combos": len(counts),
            "duplicate_rows": duplicate_rows,
            "diff_tickets": diff_tickets,
        },
    }


# --------------------------------------------------------------------------- #
# 来源更正后的增量维护
# --------------------------------------------------------------------------- #


def _sync_diffs(
    conn: sqlite3.Connection, targets: set[tuple[str, str]]
) -> int:
    """集合化维护目标票的差异记录，返回实际改动的差异行数。

    - 票已不再是差异：删除差异记录；
    - 仍差异但聚合未变（典型：整票未收的少收数随预演变化）：
      UPSERT 的 WHERE 条件使其不落写，原记录保持不变；
    - 仍差异且聚合变化：更新。
    """
    if not targets:
        return 0
    conn.execute("DROP TABLE IF EXISTS temp.recon_targets")
    conn.execute(
        "CREATE TEMP TABLE recon_targets("
        "departure_mon TEXT NOT NULL, waybill_no TEXT NOT NULL, "
        "PRIMARY KEY(departure_mon, waybill_no)) WITHOUT ROWID"
    )
    conn.executemany(
        "INSERT INTO recon_targets(departure_mon, waybill_no) VALUES (?, ?)",
        sorted(targets),
    )
    before = conn.total_changes

    # 已核平的票：从差异表移除（目标票是差异票或曾提交过清单的受影响票）。
    conn.execute(
        "DELETE FROM recon_diffs "
        " WHERE (departure_mon, waybill_no) IN "
        "       (SELECT departure_mon, waybill_no FROM recon_targets) "
        "   AND NOT EXISTS ("
        "       SELECT 1 FROM match_stats ms "
        "        LEFT JOIN recon_tickets t "
        "               ON t.departure_mon = ms.departure_mon "
        "              AND t.waybill_no = ms.waybill_no "
        "        WHERE ms.departure_mon = recon_diffs.departure_mon "
        "          AND ms.waybill_no = recon_diffs.waybill_no "
        "          AND (ms.generated_rows > COALESCE(t.expected_received, 0)"
        "               OR COALESCE(t.unexpected_received, 0) > 0"
        "               OR COALESCE(t.duplicate_rows, 0) > 0)"
        "   )"
    )

    # 仍差异的票：聚合变了才写（写法见 _sync_diffs 文档）。
    conn.execute(
        "INSERT INTO recon_diffs("
        "departure_mon, waybill_no, expected_received, unexpected_received, "
        "duplicate_rows, receipt_rows) "
        + _DIFF_TARGETS
        + f" WHERE {_is_diff_expr('ms', 't')}"
        " ON CONFLICT(departure_mon, waybill_no) DO UPDATE SET"
        "   expected_received = excluded.expected_received,"
        "   unexpected_received = excluded.unexpected_received,"
        "   duplicate_rows = excluded.duplicate_rows,"
        "   receipt_rows = excluded.receipt_rows"
        " WHERE recon_diffs.expected_received <> excluded.expected_received"
        "    OR recon_diffs.unexpected_received"
        "       <> excluded.unexpected_received"
        "    OR recon_diffs.duplicate_rows <> excluded.duplicate_rows"
        "    OR recon_diffs.receipt_rows <> excluded.receipt_rows"
    )
    conn.execute("DROP TABLE IF EXISTS temp.recon_targets")
    return conn.total_changes - before


def _recheck(
    conn: sqlite3.Connection, column: str, record_nos: Iterable[str]
) -> tuple[set[tuple[str, str]], int]:
    """只重查引用给定来源记录号的不同实收组合。"""
    pairs: set[tuple[str, str]] = set()
    rechecked = 0
    for record_no in set(record_nos):
        rows = conn.execute(
            f"SELECT DISTINCT departure_mon, waybill_no "
            f"FROM recon_receipts WHERE {column} = ?",
            (record_no,),
        ).fetchall()
        if rows:
            pairs.update((r["departure_mon"], r["waybill_no"]) for r in rows)
            rechecked += conn.execute(
                f"SELECT COUNT(*) AS n FROM recon_receipts WHERE {column} = ?",
                (record_no,),
            ).fetchone()["n"]
            _set_expected(conn, f"WHERE {column} = ?", (record_no,))
    return pairs, rechecked


def apply_source_change(
    conn: sqlite3.Connection,
    waybill_nos: Iterable[str],
    recheck_weighs: Iterable[str] = (),
    recheck_prices: Iterable[str] = (),
) -> dict:
    """来源写入后在同一事务内更新对账差异。

    - 只重查引用被改来源记录号的实收组合；
    - 其余受影响运单只按预演结果调整“是否差异”与少收数（不重算清单）；
    - 整票未收且更正前后都少收时保持差异记录原样。
    """
    pairs, rechecked = _recheck(conn, "weigh_record_no", recheck_weighs)
    more, count = _recheck(conn, "price_record_no", recheck_prices)
    pairs |= more
    rechecked += count

    if pairs:
        # 只对重查过的组合刷新聚合；其余票的清单聚合不变。
        conn.execute("DROP TABLE IF EXISTS temp.recon_rechecked")
        conn.execute(
            "CREATE TEMP TABLE recon_rechecked("
            "departure_mon TEXT NOT NULL, waybill_no TEXT NOT NULL, "
            "PRIMARY KEY(departure_mon, waybill_no)) WITHOUT ROWID"
        )
        conn.executemany(
            "INSERT INTO recon_rechecked(departure_mon, waybill_no) "
            "VALUES (?, ?)",
            sorted(pairs),
        )
        conn.execute(
            "INSERT OR REPLACE INTO recon_tickets("
            "departure_mon, waybill_no, expected_received, "
            "unexpected_received, duplicate_rows, receipt_rows) "
            + _TICKET_AGG_SQL
        )
        conn.execute("DROP TABLE IF EXISTS temp.recon_rechecked")

    targets = set(pairs)
    waybill_set = set(waybill_nos)
    for chunk in _chunks(sorted(waybill_set)):
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT ms.departure_mon, ms.waybill_no "
            "  FROM match_stats ms "
            "  JOIN recon_months rm ON rm.departure_mon = ms.departure_mon "
            f" WHERE ms.waybill_no IN ({marks})",
            tuple(chunk),
        ).fetchall()
        targets.update((r["departure_mon"], r["waybill_no"]) for r in rows)

    return {
        "rechecked_combos": rechecked,
        "reconciliation_changed": _sync_diffs(conn, targets),
    }


def rebuild_all(conn: sqlite3.Connection) -> None:
    """全量重建（管理核对用）：重新判定所有实收组合的应收属性。"""
    _set_expected(conn)
    conn.execute("DELETE FROM recon_tickets")
    conn.execute(
        """
        INSERT INTO recon_tickets(
            departure_mon, waybill_no, expected_received,
            unexpected_received, duplicate_rows, receipt_rows)
        SELECT departure_mon, waybill_no,
               SUM(is_expected),
               SUM(CASE WHEN is_expected = 0 THEN 1 ELSE 0 END),
               SUM(line_count - 1),
               SUM(line_count)
          FROM recon_receipts
         GROUP BY departure_mon, waybill_no
        """
    )
    conn.execute("DELETE FROM recon_diffs")
    conn.execute(
        "INSERT INTO recon_diffs("
        "departure_mon, waybill_no, expected_received, unexpected_received, "
        "duplicate_rows, receipt_rows) "
        + _DIFF_SELECT
        + " WHERE rm.departure_mon IS NOT NULL"
        + f" AND {_is_diff_expr('ms', 't')}"
    )


# --------------------------------------------------------------------------- #
# 差异分页（只读结果表）
# --------------------------------------------------------------------------- #


def diff_page(
    conn: sqlite3.Connection,
    departure_mon: str,
    limit: int,
    cursor: str | None,
) -> dict:
    """按运单号键集分页；只走 recon_diffs + match_stats，不碰清单/来源。

    recon_diffs 的主键是 (departure_mon, waybill_no)：等值月 + 运单号范围，
    ORDER BY waybill_no 直接沿用主键顺序，读取行数只随 limit 增长；
    已核平的票不在 recon_diffs 中，因此不会被遍历。
    """
    _require_month(departure_mon)

    sql = (
        "SELECT d.departure_mon, d.waybill_no, ms.generated_rows, "
        "       d.expected_received, d.unexpected_received, "
        "       d.duplicate_rows, d.receipt_rows "
        "  FROM recon_diffs d "
        "  JOIN match_stats ms ON ms.waybill_no = d.waybill_no "
        " WHERE d.departure_mon = ?"
    )
    params: list = [departure_mon]
    if cursor is not None:
        sql += " AND d.waybill_no > ?"
        params.append(cursor)
    sql += " ORDER BY d.waybill_no ASC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    items = []
    for r in rows:
        missing = max(0, r["generated_rows"] - r["expected_received"])
        items.append(
            {
                "waybill_no": r["waybill_no"],
                "departure_mon": r["departure_mon"],
                "expected_total": r["generated_rows"],
                "expected_received": r["expected_received"],
                "missing_expected": missing,
                "unexpected_received": r["unexpected_received"],
                "duplicate_rows": r["duplicate_rows"],
                "received_rows": r["receipt_rows"],
            }
        )
    return {
        "items": items,
        "next_cursor": rows[-1]["waybill_no"] if len(rows) == limit else None,
        "limit": limit,
        "count": len(items),
    }


def month_summary(conn: sqlite3.Connection, mon: str) -> dict:
    """整月差异汇总（仍只读差异票，不扫描已核平票 / 清单 / 来源）。"""
    _require_month(mon)
    row = conn.execute(
        "SELECT COUNT(*) AS diff_tickets, "
        "       COALESCE(SUM(MAX(0, ms.generated_rows - d.expected_received)),"
        "                 0) AS missing_expected, "
        "       COALESCE(SUM(d.unexpected_received), 0) AS unexpected_received, "
        "       COALESCE(SUM(d.duplicate_rows), 0) AS duplicate_rows "
        "  FROM recon_diffs d "
        "  JOIN match_stats ms ON ms.waybill_no = d.waybill_no "
        " WHERE d.departure_mon = ?",
        (mon,),
    ).fetchone()
    return {
        "departure_mon": mon,
        "diff_tickets": row["diff_tickets"],
        "missing_expected": row["missing_expected"],
        "unexpected_received": row["unexpected_received"],
        "duplicate_rows": row["duplicate_rows"],
    }
