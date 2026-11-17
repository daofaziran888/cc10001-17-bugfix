"""采集仓储接口：维护结果批次，覆盖触发校验、标记通过、标记失败等动作。

状态流转走 `services/result_store` 的状态机；每次变更都会写状态变更记录，
本模块额外暴露变更日志查询与历史纠偏（状态重算）入口。
"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.result_store import ResultStoreService

router = APIRouter(prefix="/api/result_store", tags=["采集仓储"])

service = ResultStoreService()

LIST_FIELDS = ["批次编号", "关联任务", "原始行数", "有效行数", "存储路径", "压缩格式", "校验摘要", "落地时间"]
STATUSES = ["接收中", "已落盘", "校验通过", "校验失败"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按批次编号检索"),
    status: str | None = Query(default=None, description="接收中、已落盘、校验通过、校验失败"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、批次编号关键字与状态过滤采集仓储列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="result_store.csv"'})


@router.get("/status_changes", response_model=PageResult[dict])
def list_status_changes(
    batch_id: int | None = Query(default=None, description="按批次过滤变更记录"),
    status: str | None = Query(default=None, description="按原状态或目标状态过滤"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=500),
) -> PageResult[dict]:
    """状态变更日志：每条记录保存原状态、目标状态、动作与变更原因。"""
    items, total = service.list_status_changes(batch_id=batch_id, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/reconcile", response_model=ActionResult)
def reconcile_entries(
    batch_id: int | None = Query(default=None, description="只重算指定批次；缺省为全部批次"),
) -> ActionResult:
    """历史纠偏：按动作日志用修正后的映射重算当前状态，旧状态保留为历史结论。"""
    report, message = service.reconcile(batch_id)
    if report is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=report)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条结果批次明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"结果批次 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/status_changes", response_model=PageResult[dict])
def get_entry_status_changes(
    entry_id: int,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=500),
) -> PageResult[dict]:
    """单条批次的状态变更日志，用于查看每次变更的依据。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"结果批次 {entry_id} 不存在或已归档")
    items, total = service.list_status_changes(batch_id=entry_id, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条结果批次，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="结果批次已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条结果批次执行触发校验、标记通过、标记失败；非法跳转会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip(), payload.remark)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
