"""服务层共享异常，避免 service / reconciliation 之间循环导入。"""

from __future__ import annotations


class RehearsalError(Exception):
    """服务层基类（统一映射为 422）。"""


class NotFoundError(RehearsalError):
    """记录不存在（例如撤回不存在的记录号）。"""


class ConflictError(RehearsalError):
    """唯一键冲突（例如运单号已存在）。"""
