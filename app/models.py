"""Pydantic 请求/响应模型。日期均为 ISO 8601（YYYY-MM-DD）。"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# --------------------------------------------------------------------------- #
# 输入
# --------------------------------------------------------------------------- #


class WaybillIn(BaseModel):
    waybill_no: str = Field(min_length=1, max_length=64, description="唯一运单号")
    train_no: str = Field(min_length=1, max_length=64, description="车次")
    origin: str = Field(min_length=1, max_length=64, description="始发站")
    departure_dt: _dt.date = Field(description="发车日")

    @field_validator("waybill_no", "train_no", "origin")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class WaybillBatch(BaseModel):
    items: list[WaybillIn] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def _unique_within_batch(self) -> "WaybillBatch":
        seen: set[str] = set()
        for it in self.items:
            if it.waybill_no in seen:
                raise ValueError(f"批次内运单号重复: {it.waybill_no}")
            seen.add(it.waybill_no)
        return self


class WeighIn(BaseModel):
    record_no: str = Field(min_length=1, max_length=64, description="过磅单记录号")
    train_no: str = Field(min_length=1, max_length=64)
    weight_kg: float = Field(gt=0, description="过磅重量（公斤，正数）")
    note: str = Field(default="", max_length=255)

    @field_validator("record_no", "train_no")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class PriceIn(BaseModel):
    record_no: str = Field(min_length=1, max_length=64, description="价目记录号")
    origin: str = Field(min_length=1, max_length=64)
    valid_from: _dt.date
    valid_to: _dt.date
    price_per_t: float = Field(ge=0, description="每吨单价")
    note: str = Field(default="", max_length=255)

    @model_validator(mode="after")
    def _window_order(self) -> "PriceIn":
        if self.valid_from > self.valid_to:
            raise ValueError("valid_from 不能晚于 valid_to")
        return self

    @field_validator("record_no", "origin")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


class WriteResult(BaseModel):
    status: Literal["committed"] = "committed"
    version: int
    affected_waybills: int = Field(description="重算涉及的运单数")
    affected_months: list[str] = Field(description="重算涉及的发车月")
    detail: dict[str, Any] = Field(default_factory=dict)


class RehearsalWaybill(BaseModel):
    waybill_no: str
    departure_mon: str
    weigh_hits: int
    price_hits: int
    generated_rows: int = Field(description="两次内连接生成的总行数")
    is_anomaly: bool


class AnomalyPage(BaseModel):
    items: list[RehearsalWaybill]
    next_cursor: str | None
    limit: int
    count: int


class MonthTotal(BaseModel):
    departure_mon: str
    generated_rows: int = Field(
        description="该月全部运单两次内连接会生成的总行数"
    )


class Overview(BaseModel):
    """月合计与异常页取自同一已提交状态（同一只读快照 + 同一版本号）。"""

    version: int
    month_total: MonthTotal
    anomalies: AnomalyPage


# --------------------------------------------------------------------------- #
# 运费对账
# --------------------------------------------------------------------------- #


class ReceiptLine(BaseModel):
    """实收清单一行：运单号 + 过磅单号 + 价目号。"""

    waybill_no: str = Field(min_length=1, max_length=64)
    weigh_record_no: str = Field(min_length=1, max_length=64)
    price_record_no: str = Field(min_length=1, max_length=64)

    @field_validator("waybill_no", "weigh_record_no", "price_record_no")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class ReceiptBatch(BaseModel):
    """整月实收清单；lines 必填，允许空列表（显式表示当月零实收）。"""

    lines: list[ReceiptLine] = Field(max_length=100_000)


class DiffTicket(BaseModel):
    waybill_no: str
    departure_mon: str
    expected_total: int = Field(description="该票应收组合数（预演生成行数）")
    expected_received: int = Field(description="已收到的应收组合数")
    missing_expected: int = Field(description="应收却未收到的组合数")
    unexpected_received: int = Field(description="不应收到却实收的组合数")
    duplicate_rows: int = Field(description="重复行数（第二次起计）")
    received_rows: int = Field(description="清单中该票的原始行数")


class DiffPage(BaseModel):
    items: list[DiffTicket]
    next_cursor: str | None
    limit: int
    count: int


class ReconMonthSummary(BaseModel):
    departure_mon: str
    diff_tickets: int
    missing_expected: int
    unexpected_received: int
    duplicate_rows: int



class Health(BaseModel):
    status: Literal["ok"]
    version: int
    db_path: str
