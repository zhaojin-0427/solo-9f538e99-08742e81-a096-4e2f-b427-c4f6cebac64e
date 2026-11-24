"""运费对账端到端用例（真实 FastAPI + 真实 SQLite）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import db


# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #


def waybill(no: str, train: str, origin: str, dt: str) -> dict:
    return {"waybill_no": no, "train_no": train, "origin": origin, "departure_dt": dt}


def weigh(no: str, train: str, weight: float = 1000.0) -> dict:
    return {"record_no": no, "train_no": train, "weight_kg": weight}


def price(no: str, origin: str, vfrom: str, vto: str, rate: float = 50.0) -> dict:
    return {
        "record_no": no,
        "origin": origin,
        "valid_from": vfrom,
        "valid_to": vto,
        "price_per_t": rate,
    }


def receipt(no: str, weigh_no: str, price_no: str) -> dict:
    return {
        "waybill_no": no,
        "weigh_record_no": weigh_no,
        "price_record_no": price_no,
    }


def import_waybills(client: TestClient, items: list[dict]) -> None:
    r = client.post("/waybills/import", json={"items": items})
    assert r.status_code == 200, r.text


def put(client: TestClient, kind: str, rec_no: str, body: dict) -> dict:
    r = client.put(f"/{kind}/records/{rec_no}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def delete(client: TestClient, kind: str, rec_no: str) -> dict:
    r = client.delete(f"/{kind}/records/{rec_no}")
    assert r.status_code == 200, r.text
    return r.json()


def submit(client: TestClient, mon: str, lines: list[dict]) -> dict:
    r = client.post(
        f"/reconciliation/months/{mon}/receipts", json={"lines": lines}
    )
    assert r.status_code == 200, r.text
    return r.json()


def diffs(client: TestClient, mon: str, limit: int = 50, cursor=None) -> dict:
    params = {"limit": limit}
    if cursor is not None:
        params["cursor"] = cursor
    r = client.get(f"/reconciliation/months/{mon}/diffs", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def summary(client: TestClient, mon: str) -> dict:
    r = client.get(f"/reconciliation/months/{mon}/summary")
    assert r.status_code == 200, r.text
    return r.json()


def by_no(page: dict) -> dict:
    return {it["waybill_no"]: it for it in page["items"]}


# --------------------------------------------------------------------------- #
# 1. 三列组合 + 重复计数：同一组合从第二次出现起只计重复
# --------------------------------------------------------------------------- #


def test_combination_counts_and_duplicates(client: TestClient) -> None:
    import_waybills(
        client,
        [
            waybill("W1", "T1", "BJ", "2026-03-05"),  # 应收 1 组合
            waybill("W2", "T2", "BJ", "2026-03-06"),  # 应收 1 组合
        ],
    )
    put(client, "weigh", "G1", weigh("G1", "T1"))
    put(client, "weigh", "G2", weigh("G2", "T2"))
    put(client, "price", "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))

    res = submit(
        client,
        "2026-03",
        [
            # W1 正常 1 行；再出现两次 → 2 行重复
            receipt("W1", "G1", "P1"),
            receipt("W1", "G1", "P1"),
            receipt("W1", "G1", "P1"),
            # W2 收到两个不同组合：一个应收、一个价目号不存在（不应收到）
            receipt("W2", "G2", "P1"),
            receipt("W2", "GX", "PX"),
        ],
    )
    assert res["detail"]["submitted_rows"] == 5
    assert res["detail"]["distinct_combos"] == 3
    assert res["detail"]["duplicate_rows"] == 2

    page = by_no(diffs(client, "2026-03"))
    # W1：应收 1；实收 2 个不同行但去重后只有 1 个组合收到；重复 2
    assert page["W1"]["missing_expected"] == 0
    assert page["W1"]["unexpected_received"] == 0
    assert page["W1"]["duplicate_rows"] == 2
    assert page["W1"]["received_rows"] == 3

    # W2：应收 1，收到 1 个应收组合；GX/PX 组合不应收 → unexpected 1
    assert page["W2"]["missing_expected"] == 0
    assert page["W2"]["unexpected_received"] == 1
    assert page["W2"]["duplicate_rows"] == 0

    s = summary(client, "2026-03")
    assert s["diff_tickets"] == 2
    assert s["missing_expected"] == 0
    assert s["unexpected_received"] == 1
    assert s["duplicate_rows"] == 2


# --------------------------------------------------------------------------- #
# 2. 少收 / 多收 / 重复按票与按月分别报告；已核平票不进差异页
# --------------------------------------------------------------------------- #


def test_missing_unexpected_and_duplicate_reported(client: TestClient) -> None:
    import_waybills(
        client,
        [
            waybill("W1", "T1", "BJ", "2026-03-01"),  # 全收 → 不进差异
            waybill("W2", "T2", "BJ", "2026-03-02"),  # 少收
            waybill("W3", "T3", "BJ", "2026-03-03"),  # 多收
            waybill("W4", "T4", "BJ", "2026-03-04"),  # 重复
        ],
    )
    for i in range(1, 5):
        put(client, "weigh", f"G{i}", weigh(f"G{i}", f"T{i}"))
    put(client, "price", "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))

    submit(
        client,
        "2026-03",
        [
            receipt("W1", "G1", "P1"),
            # W2 没有任何行 → 整票未收，少 1
            receipt("W3", "G3", "P1"),
            receipt("W3", "G3", "P2"),  # P2 不存在 → 多收 1
            receipt("W4", "G4", "P1"),
            receipt("W4", "G4", "P1"),  # 重复 1
        ],
    )

    page = by_no(diffs(client, "2026-03"))
    assert set(page) == {"W2", "W3", "W4"}
    assert page["W2"]["missing_expected"] == 1
    assert page["W2"]["received_rows"] == 0
    assert page["W3"]["unexpected_received"] == 1
    assert page["W3"]["missing_expected"] == 0
    assert page["W4"]["duplicate_rows"] == 1

    s = summary(client, "2026-03")
    assert s["missing_expected"] == 1
    assert s["unexpected_received"] == 1
    assert s["duplicate_rows"] == 1


# --------------------------------------------------------------------------- #
# 3. 来源单号已撤回也照收：组合被判为“不应收到”
# --------------------------------------------------------------------------- #


def test_withdrawn_source_reference_still_accepted(client: TestClient) -> None:
    import_waybills(client, [waybill("W1", "T1", "BJ", "2026-03-05")])
    put(client, "weigh", "G1", weigh("G1", "T1"))
    put(client, "price", "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))
    submit(client, "2026-03", [receipt("W1", "G1", "P1")])
    assert diffs(client, "2026-03")["count"] == 0

    # 撤回来源，清单保留；该组合现在不应收
    delete(client, "weigh", "G1")
    page = by_no(diffs(client, "2026-03"))
    assert page["W1"]["unexpected_received"] == 1
    assert page["W1"]["missing_expected"] == 0

    # 用已撤回的过磅单号再次提交清单：照收（不校验来源存在）
    res = submit(client, "2026-03", [receipt("W1", "G1", "P1")])
    assert res["detail"]["distinct_combos"] == 1
    page = by_no(diffs(client, "2026-03"))
    assert page["W1"]["unexpected_received"] == 1


# --------------------------------------------------------------------------- #
# 4. 清单元数据校验：运单须已导入且属于当月；失败不改旧结果
# --------------------------------------------------------------------------- #


def test_receipt_validation_keeps_old_result(client: TestClient) -> None:
    import_waybills(
        client,
        [
            waybill("W1", "T1", "BJ", "2026-03-05"),
            waybill("W2", "T2", "BJ", "2026-04-05"),
        ],
    )
    put(client, "weigh", "G1", weigh("G1", "T1"))
    put(client, "price", "P1", price("P1", "BJ", "2026-03-01", "2026-04-30"))
    put(client, "price", "P2", price("P2", "BJ", "2026-03-01", "2026-04-30"))
    # W1 应收 2 组合，只收 1 → 旧结果有 1 条差异
    submit(client, "2026-03", [receipt("W1", "G1", "P1")])
    assert by_no(diffs(client, "2026-03"))["W1"]["missing_expected"] == 1
    version = client.get("/health").json()["version"]

    # 未导入运单
    r = client.post(
        "/reconciliation/months/2026-03/receipts",
        json={"lines": [receipt("NOPE", "G1", "P1")]},
    )
    assert r.status_code == 422
    # 运单不属于当月（W2 是 4 月）
    r = client.post(
        "/reconciliation/months/2026-03/receipts",
        json={"lines": [receipt("W2", "G1", "P1")]},
    )
    assert r.status_code == 422
    # 发车月格式非法（写接口与两个读接口都应是 422）
    r = client.post(
        "/reconciliation/months/2026-13/receipts",
        json={"lines": [receipt("W1", "G1", "P1")]},
    )
    assert r.status_code == 422
    assert client.get("/reconciliation/months/2026-13/diffs").status_code == 422
    assert client.get("/reconciliation/months/bad/summary").status_code == 422
    # 缺少 lines 字段
    r = client.post("/reconciliation/months/2026-03/receipts", json={})
    assert r.status_code == 422

    assert client.get("/health").json()["version"] == version
    # 旧清单与旧差异完整保留
    page = by_no(diffs(client, "2026-03"))
    assert page["W1"]["missing_expected"] == 1
    assert page["W1"]["received_rows"] == 1


# --------------------------------------------------------------------------- #
# 5. 新清单整月替换旧清单与旧差异
# --------------------------------------------------------------------------- #


def test_empty_receipt_lists_all_as_missing(client: TestClient) -> None:
    import_waybills(
        client,
        [
            waybill("W1", "T1", "BJ", "2026-03-05"),
            waybill("W2", "T2", "BJ", "2026-03-06"),
        ],
    )
    put(client, "weigh", "G1", weigh("G1", "T1"))
    put(client, "weigh", "G2", weigh("G2", "T2"))
    put(client, "price", "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))

    res = submit(client, "2026-03", [])
    assert res["detail"]["distinct_combos"] == 0
    assert res["detail"]["diff_tickets"] == 2
    page = by_no(diffs(client, "2026-03"))
    assert page["W1"]["missing_expected"] == 1
    assert page["W2"]["missing_expected"] == 1
    s = summary(client, "2026-03")
    assert s["missing_expected"] == 2
    assert s["unexpected_received"] == 0
    assert s["duplicate_rows"] == 0


def test_new_waybill_after_submission_joins_reconciliation(
    client: TestClient,
) -> None:
    import_waybills(client, [waybill("W1", "T1", "BJ", "2026-03-05")])
    put(client, "weigh", "G1", weigh("G1", "T1"))
    put(client, "price", "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))
    submit(client, "2026-03", [receipt("W1", "G1", "P1")])
    assert diffs(client, "2026-03")["count"] == 0

    # 当月新增运单（有应收组合但无任何实收）→ 自动成为整票未收差异票
    put(client, "weigh", "G2", weigh("G2", "T2"))
    import_waybills(client, [waybill("W2", "T2", "BJ", "2026-03-06")])
    page = by_no(diffs(client, "2026-03"))
    assert set(page) == {"W2"}
    assert page["W2"]["missing_expected"] == 1

    # 未提交过清单的月份不产生差异票
    import_waybills(client, [waybill("W9", "T9", "BJ", "2026-05-01")])
    assert diffs(client, "2026-05")["count"] == 0


def test_new_receipt_replaces_old_result(client: TestClient) -> None:
    import_waybills(client, [waybill("W1", "T1", "BJ", "2026-03-05")])
    put(client, "weigh", "G1", weigh("G1", "T1"))
    put(client, "price", "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))

    submit(client, "2026-03", [receipt("W1", "G1", "P1")])
    assert diffs(client, "2026-03")["count"] == 0

    # 新清单只有一行不应收组合：旧差异被整体替换
    submit(client, "2026-03", [receipt("W1", "GX", "PX")])
    page = by_no(diffs(client, "2026-03"))
    assert page["W1"]["unexpected_received"] == 1
    assert page["W1"]["expected_received"] == 0
    assert summary(client, "2026-03")["duplicate_rows"] == 0


# --------------------------------------------------------------------------- #
# 6. 来源更正只重查引用该记录号的实收组合
# --------------------------------------------------------------------------- #


def test_source_correction_rechecks_only_referenced_combos(
    client: TestClient,
) -> None:
    import_waybills(
        client,
        [
            waybill("W1", "T1", "BJ", "2026-03-05"),
            waybill("W2", "T2", "BJ", "2026-03-06"),
        ],
    )
    put(client, "weigh", "G1", weigh("G1", "T1"))
    put(client, "weigh", "G2", weigh("G2", "T2"))
    put(client, "price", "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))
    submit(
        client,
        "2026-03",
        [receipt("W1", "G1", "P1"), receipt("W2", "G2", "P1")],
    )
    assert diffs(client, "2026-03")["count"] == 0

    # 更正 G1 的车次：只有引用 G1 的 W1 组合被重查
    res = put(client, "weigh", "G1", weigh("G1", "T9"))
    assert res["detail"]["rechecked_combos"] == 1
    page = by_no(diffs(client, "2026-03"))
    assert set(page) == {"W1"}
    assert page["W1"]["unexpected_received"] == 1
    assert page["W1"]["missing_expected"] == 0
    # W2 未受影响
    assert "W2" not in page

    # 改回来只重查 1 个组合
    res = put(client, "weigh", "G1", weigh("G1", "T1"))
    assert res["detail"]["rechecked_combos"] == 1
    assert diffs(client, "2026-03")["count"] == 0


def test_price_correction_only_touches_referenced_combos(
    client: TestClient,
) -> None:
    import_waybills(
        client,
        [
            waybill("W1", "T1", "BJ", "2026-03-05"),
            waybill("W2", "T2", "SH", "2026-03-06"),
        ],
    )
    put(client, "weigh", "G1", weigh("G1", "T1"))
    put(client, "weigh", "G2", weigh("G2", "T2"))
    put(client, "price", "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))
    put(client, "price", "P2", price("P2", "SH", "2026-03-01", "2026-03-31"))
    submit(
        client,
        "2026-03",
        [receipt("W1", "G1", "P1"), receipt("W2", "G2", "P2")],
    )
    assert diffs(client, "2026-03")["count"] == 0

    # 把 P1 的始发站改掉：只影响引用 P1 的 W1 组合
    res = put(client, "price", "P1", price("P1", "GZ", "2026-03-01", "2026-03-31"))
    assert res["detail"]["rechecked_combos"] == 1
    page = by_no(diffs(client, "2026-03"))
    assert set(page) == {"W1"}
    assert page["W1"]["unexpected_received"] == 1


# --------------------------------------------------------------------------- #
# 7. 整票未收少收数随预演更新；更正前后都少收则不改写差异记录
# --------------------------------------------------------------------------- #


def test_whole_ticket_missing_follows_rehearsal(client: TestClient) -> None:
    import_waybills(
        client,
        [
            waybill("W0", "T0", "BJ", "2026-03-01"),
            waybill("W1", "T1", "BJ", "2026-03-05"),
        ],
    )
    put(client, "weigh", "G1", weigh("G1", "T1"))
    put(client, "price", "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))
    # 清单只提了 W0 的一行不应收组合；W1 一行也没收 → 整票未收
    submit(client, "2026-03", [receipt("W0", "G0", "P0")])
    page = by_no(diffs(client, "2026-03"))
    assert page["W1"]["missing_expected"] == 1
    assert page["W1"]["expected_received"] == 0

    # 再补一条价目，让 W1 应收变成 2（1 过磅 × 2 价目）；少收数随预演更新
    res = put(client, "price", "P2", price("P2", "BJ", "2026-03-01", "2026-03-31"))
    page = by_no(diffs(client, "2026-03"))
    assert page["W1"]["expected_total"] == 2
    assert page["W1"]["missing_expected"] == 2

    # 该价目未被任何实收组合引用：不重查任何组合；
    # 更正前后 W1 都仍少收 → 不改写该票的对账记录。
    assert res["detail"]["rechecked_combos"] == 0
    assert res["detail"]["reconciliation_changed"] == 0

    # 反向：撤掉这条价目后仍少收，同样不改写
    res = delete(client, "price", "P2")
    assert res["detail"]["rechecked_combos"] == 0
    assert res["detail"]["reconciliation_changed"] == 0
    page = by_no(diffs(client, "2026-03"))
    assert page["W1"]["missing_expected"] == 1


# --------------------------------------------------------------------------- #
# 8. 分页只定位差异票：页长控制读取行数，不遍历已核平票
# --------------------------------------------------------------------------- #


def test_diff_pagination_skips_reconciled_tickets(client: TestClient) -> None:
    # 20 票，其中偶数票核平，奇数票少收 → 10 张差异票
    items = [
        waybill(f"W{i:02d}", f"T{i}", "BJ", "2026-03-05") for i in range(20)
    ]
    import_waybills(client, items)
    for i in range(20):
        put(client, "weigh", f"G{i}", weigh(f"G{i}", f"T{i}"))
    put(client, "price", "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))
    lines = [
        receipt(f"W{i:02d}", f"G{i}", "P1") for i in range(0, 20, 2)
    ]
    submit(client, "2026-03", lines)

    page1 = diffs(client, "2026-03", limit=3)
    assert [x["waybill_no"] for x in page1["items"]] == ["W01", "W03", "W05"]
    assert page1["next_cursor"] == "W05"

    page2 = diffs(client, "2026-03", limit=3, cursor=page1["next_cursor"])
    assert [x["waybill_no"] for x in page2["items"]] == ["W07", "W09", "W11"]

    # 全量翻完只有差异票，已核平票不出现
    seen = [x["waybill_no"] for x in page1["items"] + page2["items"]]
    cursor = page2["next_cursor"]
    while cursor is not None:
        p = diffs(client, "2026-03", limit=3, cursor=cursor)
        seen += [x["waybill_no"] for x in p["items"]]
        cursor = p["next_cursor"]
    assert seen == [f"W{i:02d}" for i in range(1, 20, 2)]


def test_diff_page_reads_only_a_page_not_reconciled_tickets(
    client: TestClient, tmp_path
) -> None:
    """大库实测：200000 张已核平票 + 3 张差异票，翻一页的 VM 指令数不随
    已核平票数增长（不扫描、不遍历已核平票，也不碰清单/来源）。"""
    from app import reconciliation

    conn = db.connect(tmp_path / "test_freight.db")
    try:
        with db.immediate(conn):
            conn.executemany(
                "INSERT INTO match_stats(waybill_no, departure_mon, "
                "weigh_hits, price_hits, generated_rows, is_anomaly) "
                "VALUES (?, '2026-03', 1, 1, 1, 0)",
                [(f"C{i:06d}",) for i in range(200_000)]
                + [(f"Z{i:03d}",) for i in range(3)],
            )
            conn.executemany(
                "INSERT INTO recon_diffs(departure_mon, waybill_no, "
                "expected_received, unexpected_received, duplicate_rows, "
                "receipt_rows) VALUES ('2026-03', ?, 0, 1, 0, 1)",
                [(f"Z{i:03d}",) for i in range(3)],
            )

        def steps(limit: int) -> int:
            calls = {"n": 0}

            def handler() -> int:
                calls["n"] += 1
                return 0

            conn.set_progress_handler(handler, 1)
            try:
                page = reconciliation.diff_page(conn, "2026-03", limit, None)
            finally:
                conn.set_progress_handler(None, 0)
            assert page["count"] == min(limit, 3)
            return calls["n"]

        # 页长 1 / 3 的 VM 指令数应在同一量级，与 20 万行库规模无关。
        one, three = steps(1), steps(3)
        assert three < 5_000
        assert one < 5_000
        assert three < one * 4 + 100
    finally:
        conn.close()


def test_diff_query_does_not_touch_receipts_or_sources(
    client: TestClient, tmp_path
) -> None:
    import_waybills(client, [waybill("W1", "T1", "BJ", "2026-03-05")])
    put(client, "weigh", "G1", weigh("G1", "T1"))
    put(client, "price", "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))
    submit(client, "2026-03", [receipt("W1", "G1", "P1")])

    conn = db.connect(tmp_path / "test_freight.db")
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT d.departure_mon, d.waybill_no, ms.generated_rows "
            "FROM recon_diffs d JOIN match_stats ms "
            "ON ms.waybill_no = d.waybill_no "
            "WHERE d.departure_mon = ? AND d.waybill_no > ? "
            "ORDER BY d.waybill_no LIMIT ?",
            ("2026-03", "", 50),
        ).fetchall()
        text = " ".join(row[3] for row in plan)
        assert "recon_receipts" not in text
        assert "waybills" not in text
        assert "weigh_records" not in text
        assert "price_records" not in text
        # 差异页走主键范围扫描，不扫整表
        assert "PRIMARY KEY" in text or "USING INDEX" in text
        assert "SCAN recon_diffs" not in text
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 9. 自测场景：来源更正后，差异票夹在已核平运单中间
# --------------------------------------------------------------------------- #


def test_corrected_diff_ticket_between_reconciled_ones(client: TestClient) -> None:
    # 运单号连续：W1..W5，W2/W4 核平，W3 会被更正后变成差异
    import_waybills(
        client,
        [waybill(f"W{i}", f"T{i}", "BJ", "2026-03-05") for i in range(1, 6)],
    )
    for i in range(1, 6):
        put(client, "weigh", f"G{i}", weigh(f"G{i}", f"T{i}"))
    put(client, "price", "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))

    # 所有票都整票核平（含 W3）
    submit(
        client,
        "2026-03",
        [receipt(f"W{i}", f"G{i}", "P1") for i in range(1, 6)],
    )
    assert diffs(client, "2026-03", limit=10)["count"] == 0

    # 更正 G3 的车次 → 仅 W3 变成“已撤回/不应收”，夹在核平票中间
    res = put(client, "weigh", "G3", weigh("G3", "T9"))
    assert res["detail"]["rechecked_combos"] == 1

    page = diffs(client, "2026-03", limit=10)
    assert [x["waybill_no"] for x in page["items"]] == ["W3"]
    item = page["items"][0]
    assert item["unexpected_received"] == 1
    assert item["missing_expected"] == 0
    assert item["expected_total"] == 0

    # 小页长也直接命中该票（不遍历 W1/W2/W4/W5）
    p1 = diffs(client, "2026-03", limit=1)
    assert [x["waybill_no"] for x in p1["items"]] == ["W3"]

    # 更正回来 → 差异消失
    put(client, "weigh", "G3", weigh("G3", "T3"))
    assert diffs(client, "2026-03", limit=10)["count"] == 0


# --------------------------------------------------------------------------- #
# 10. 全量重建与增量结果一致
# --------------------------------------------------------------------------- #


def test_rebuild_matches_incremental_for_reconciliation(
    client: TestClient,
) -> None:
    import_waybills(
        client,
        [
            waybill("W1", "T1", "BJ", "2026-03-05"),
            waybill("W2", "T2", "BJ", "2026-03-06"),
        ],
    )
    put(client, "weigh", "G1", weigh("G1", "T1"))
    put(client, "price", "P1", price("P1", "BJ", "2026-03-01", "2026-03-31"))
    submit(
        client,
        "2026-03",
        [receipt("W1", "G1", "P1"), receipt("W2", "GX", "PX")],
    )
    put(client, "weigh", "G1", weigh("G1", "T9"))

    before = diffs(client, "2026-03", limit=10)
    before_summary = summary(client, "2026-03")
    assert client.post("/admin/rebuild").status_code == 200
    assert diffs(client, "2026-03", limit=10) == before
    assert summary(client, "2026-03") == before_summary
