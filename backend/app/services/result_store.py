"""采集仓储业务规则：状态流转、字段校验与筛选口径都收在这里。

状态机：接收中 --触发校验--> 已落盘 --标记通过/标记失败--> 校验通过/校验失败。
每次状态变更都会写入一条状态变更记录（原状态、目标状态、动作、原因），
结论状态（校验通过/校验失败）会回写到质量审计清单，同一个原因贯穿两处。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.store import store

MODULE = "result_store"
AUDIT_MODULE = "quality_check"
LIST_FIELDS = ["批次编号", "关联任务", "原始行数", "有效行数", "存储路径", "压缩格式", "校验摘要", "落地时间"]
REQUIRED_FIELDS = ["批次编号", "关联任务", "原始行数"]
STATUS_ORDER = ["接收中", "已落盘", "校验通过", "校验失败"]

# 动作 -> 目标状态。旧映射整体错位（触发校验→校验通过、标记通过→校验失败、
# 标记失败→已落盘），导致批次状态互相矛盾；以下为修正后的映射。
ACTION_RULES = {"触发校验": "已落盘", "标记通过": "校验通过", "标记失败": "校验失败"}

# 合法跳转：当前状态 -> 允许执行的动作，除此之外一律按非法跳转拦截。
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "接收中": {"触发校验"},
    "已落盘": {"标记通过", "标记失败"},
    "校验通过": set(),
    "校验失败": set(),
}

TERMINAL_STATUSES = {"校验通过", "校验失败"}
# 状态冲突时的判定优先级：失败结论优先于通过结论，宁严勿宽。
CONCLUSION_PRIORITY = {"校验通过": 1, "校验失败": 2}
# 批次结论 -> 质量审计清单的状态与判定结果。
AUDIT_STATUS_RULES = {"校验通过": "已通过", "校验失败": "需修复"}
AUDIT_VERDICT_RULES = {"校验通过": "通过", "校验失败": "不通过"}

DEFAULT_REASONS = {
    "触发校验": "落盘完成，触发完整性校验",
    "标记通过": "人工抽检通过，批次放行",
    "标记失败": "人工抽检未通过，批次需修复",
    "质量审计": "质量审计引擎回写结论",
    "状态修正": "旧映射纠偏：按动作日志重算当前状态",
}

# 动作执行与状态变更记录写入共用一把锁：同时操作只留一份状态变更。
_change_lock = threading.Lock()


class ResultStoreService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("批次编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    # ---------- 状态变更记录 ----------

    def _next_change_id(self) -> int:
        rows = store.joint_rows("status_changes")
        return max((int(row.get("id", 0)) for row in rows), default=0) + 1

    def _record_change(
        self,
        batch: dict[str, Any],
        action: str,
        from_status: str,
        to_status: str,
        reason: str,
        audit_entry_id: int | None = None,
    ) -> dict[str, Any]:
        """写入一条状态变更记录：原状态、目标状态、动作与原因都可追溯。"""
        record = {
            "id": self._next_change_id(),
            "batch_id": int(batch.get("id", 0)),
            "批次编号": str(batch.get("批次编号", "")),
            "action": action,
            "from_status": from_status,
            "to_status": to_status,
            "reason": reason,
            "audit_entry_id": audit_entry_id,
            "changed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.joint_append("status_changes", record)
        return record

    def list_status_changes(
        self,
        *,
        batch_id: int | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """读取状态变更日志，支持按批次与状态筛选，最新的排在前面。"""
        rows = list(store.joint_rows("status_changes"))
        if batch_id is not None:
            rows = [row for row in rows if int(row.get("batch_id", 0)) == batch_id]
        if status:
            rows = [row for row in rows if status in (row.get("from_status"), row.get("to_status"))]
        rows.sort(key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    # ---------- 质量审计清单回写 ----------

    def _effective_conclusion(self, batch_id: int, fallback: str | None = None) -> str | None:
        """状态冲突按优先级形成判定依据：失败优先于通过。

        「状态修正」记录之前的结论属于旧映射产物，已被纠偏作废，不再参与判定。
        """
        conclusions: list[str] = []
        if fallback in CONCLUSION_PRIORITY:
            conclusions.append(str(fallback))
        records = [
            row for row in store.joint_rows("status_changes")
            if int(row.get("batch_id", 0)) == batch_id
        ]
        records.sort(key=lambda row: int(row.get("id", 0)))
        last_fix = max(
            (idx for idx, row in enumerate(records) if row.get("action") == "状态修正"),
            default=-1,
        )
        for row in records[last_fix + 1:]:
            target = row.get("to_status")
            if target in CONCLUSION_PRIORITY:
                conclusions.append(str(target))
        if not conclusions:
            return None
        return max(conclusions, key=lambda item: CONCLUSION_PRIORITY[item])

    def _write_back_audit(self, batch: dict[str, Any], target: str, reason: str) -> int | None:
        """把状态结论回写到质量审计清单。

        每个批次只维护一条「QC-批次编号」结论记录：已存在就更新，不存在就补建；
        审计引擎自己产生的 AUDIT-* 明细记录不被覆盖。返回结论记录的 id。
        """
        batch_code = str(batch.get("批次编号", ""))
        conclusion = self._effective_conclusion(int(batch.get("id", 0)), target) or target
        verdict = AUDIT_VERDICT_RULES[conclusion]
        audit_status = AUDIT_STATUS_RULES[conclusion]
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        code = f"QC-{batch_code}"
        rows = [
            row for row in store.rows(AUDIT_MODULE)
            if str(row.get("记录编号", "")) == code
        ]
        if rows:
            entry = rows[0]
        else:
            from app.services.quality_check import QualityCheckService

            entry, missing = QualityCheckService().create_entry({
                "记录编号": code,
                "关联批次": batch_code,
                "检查项": "采集仓储状态回写",
            })
            if entry is None:
                return None
        entry.update({
            "关联批次": batch_code,
            "检查项": "采集仓储状态回写",
            "阈值": "状态结论一致性",
            "实际值": conclusion,
            "判定结果": verdict,
            "审计时间": now,
            "处理建议": reason,
            "status": audit_status,
            "pending": False,
            "abnormal": conclusion == "校验失败",
        })
        return int(entry.get("id", 0))

    # ---------- 动作执行 ----------

    def run_action(
        self,
        entry_id: int,
        action: str,
        remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """读取批次当前状态与动作请求，按动作更新状态并写入状态变更记录。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"结果批次 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集仓储可执行范围"
        target = ACTION_RULES[action]
        reason = (remark or "").strip() or DEFAULT_REASONS[action]
        with _change_lock:
            current = str(entry.get("status", ""))
            if current == target:
                # 同时操作或重复提交：批次已在目标状态，只保留原有那一份状态变更。
                return entry, f"结果批次已处于「{target}」，本次「{action}」未重复写入状态变更"
            if action not in ALLOWED_TRANSITIONS.get(current, set()):
                return None, f"结果批次当前状态「{current}」不允许执行「{action}」，已拦截非法跳转"
            audit_entry_id = None
            if target in TERMINAL_STATUSES:
                audit_entry_id = self._write_back_audit(entry, target, reason)
            entry["status"] = target
            entry["pending"] = target not in TERMINAL_STATUSES
            entry["abnormal"] = target == "校验失败"
            self._record_change(entry, action, current, target, reason, audit_entry_id)
        return entry, f"结果批次已{action}：{current} → {target}"

    def apply_audit_result(
        self,
        batch_id: int,
        verdict: str,
        reason: str,
        audit_entry_id: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """质量审计引擎回写：审计结论落到批次状态，同样只留一份状态变更。"""
        entry = store.find(MODULE, batch_id)
        if entry is None:
            return None, f"结果批次 {batch_id} 不存在或已归档"
        target = "校验通过" if verdict == "通过" else "校验失败"
        with _change_lock:
            current = str(entry.get("status", ""))
            if current == target:
                return entry, f"结果批次已处于「{target}」，审计结论未重复写入"
            conclusion_entry_id = self._write_back_audit(entry, target, reason)
            entry["status"] = target
            entry["pending"] = False
            entry["abnormal"] = target == "校验失败"
            self._record_change(
                entry, "质量审计", current, target, reason,
                conclusion_entry_id if conclusion_entry_id is not None else audit_entry_id,
            )
        return entry, f"质量审计结论已回写：{current} → {target}"

    # ---------- 历史纠偏 ----------

    def reconcile(self, batch_id: int | None = None) -> tuple[dict[str, Any] | None, str]:
        """按动作日志用修正后的映射重算当前状态，纠正旧映射写错的历史批次。

        取舍：采用「重算 + 双层记录」——批次本身只保留重算后的一个当前状态，
        旧错误状态作为历史结论留在变更日志里，并追加一条「状态修正」记录；
        没有动作日志的批次无法重算，保留旧状态为历史结论；
        已分发等下游状态不在校验状态机内，保持原样。
        """
        batches = store.rows(MODULE)
        if batch_id is not None:
            entry = store.find(MODULE, batch_id)
            if entry is None:
                return None, f"结果批次 {batch_id} 不存在或已归档"
            batches = [entry]
        corrected: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        with _change_lock:
            for batch in batches:
                current = str(batch.get("status", ""))
                batch_key = int(batch.get("id", 0))
                if current not in STATUS_ORDER:
                    skipped.append({"batch_id": batch_key, "status": current, "why": "下游状态，不在校验状态机内"})
                    continue
                records = [
                    row for row in store.joint_rows("status_changes")
                    if int(row.get("batch_id", 0)) == batch_key
                ]
                records.sort(key=lambda row: int(row.get("id", 0)))
                if not records:
                    skipped.append({"batch_id": batch_key, "status": current, "why": "无动作日志，保留历史结论"})
                    continue
                # 用修正后的映射重放动作日志，重算当前状态。
                expected = str(records[0].get("from_status", current))
                for row in records:
                    action = str(row.get("action", ""))
                    expected = ACTION_RULES.get(action, str(row.get("to_status", expected)))
                if expected == current:
                    skipped.append({"batch_id": batch_key, "status": current, "why": "状态与动作日志一致"})
                    continue
                reason = DEFAULT_REASONS["状态修正"]
                batch["status"] = expected
                batch["pending"] = expected not in TERMINAL_STATUSES
                batch["abnormal"] = expected == "校验失败"
                record = self._record_change(batch, "状态修正", current, expected, reason)
                if expected in TERMINAL_STATUSES:
                    audit_entry_id = self._write_back_audit(batch, expected, reason)
                    if audit_entry_id is not None:
                        record["audit_entry_id"] = audit_entry_id
                        store.joint_save("status_changes")
                corrected.append({
                    "batch_id": batch_key,
                    "批次编号": str(batch.get("批次编号", "")),
                    "from_status": current,
                    "to_status": expected,
                    "change_id": record["id"],
                })
        message = f"状态重算完成：纠正 {len(corrected)} 条，跳过 {len(skipped)} 条"
        return {"corrected": corrected, "skipped": skipped}, message
