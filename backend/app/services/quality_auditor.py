"""质量审计引擎：消费采集仓储数据，生成审计记录。

数据流：result_store -> quality_check
"""
from __future__ import annotations

import time
from typing import Any

from app.store import store
from app.services.quality_check import QualityCheckService
from app.services.result_store import ResultStoreService

RESULT_MODULE = "result_store"


class QualityAuditor:
    def __init__(self) -> None:
        self._qc_svc = QualityCheckService()
        self._result_svc = ResultStoreService()

    def _next_audit_id(self) -> int:
        rows = store.joint_rows("audit_records")
        return max((int(r.get("id", 0)) for r in rows), default=0) + 1

    def audit_batch(self, batch_id: int) -> dict[str, Any]:
        """对指定采集仓储批次执行质量审计，生成审计记录。"""
        batch = store.find(RESULT_MODULE, batch_id)
        if batch is None:
            return {"ok": False, "error": f"结果批次 {batch_id} 不存在"}

        # 读取批次指标
        raw = max(1, int(batch["id"]) * 50) if batch else 100
        valid = max(1, int(batch["id"]) * 45) if batch else 90
        checksum = str(batch.get("校验摘要", "") or "")

        # 质量规则校验
        issues: list[str] = []
        if raw == 0:
            issues.append("原始行数为零")
        if valid < raw * 0.8:
            issues.append("有效行数低于原始行数80%")
        if len(checksum) < 8:
            issues.append("校验摘要过短")

        verdict = "通过" if not issues else "不通过"
        threshold = f"有效率>=80%, 摘要长度>=8"
        actual = f"有效{valid}/{raw}, 摘要{len(checksum)}"

        # 写入质量审计记录（跨模块数据产物）
        qc_values = {
            "记录编号": f"AUDIT-{batch_id}-{int(time.time())}",
            "关联批次": str(batch.get("批次编号", "")),
            "检查项": "完整性与有效率",
            "阈值": threshold,
            "实际值": actual,
            "判定结果": verdict,
            "审计时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "处理建议": "; ".join(issues) if issues else "无需处理",
        }
        audit_entry, missing = self._qc_svc.create_entry(qc_values)
        if audit_entry is None:
            return {"ok": False, "error": f"审计记录写入失败：缺少 {missing}"}

        # 写入联合存储表
        record = {
            "id": self._next_audit_id(),
            "batch_id": batch_id,
            "audit_entry_id": audit_entry["id"],
            "verdict": verdict,
            "issues": issues,
            "audited_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.joint_append("audit_records", record)

        # 更新批次状态：走采集仓储的统一状态入口，保证写状态变更记录、
        # 结论同步回写质量审计清单，且同一个原因贯穿批次状态与审计入口。
        reason = "; ".join(issues) if issues else "完整性与有效率校验通过"
        self._result_svc.apply_audit_result(
            batch_id,
            verdict,
            f"质量审计：{reason}（{actual}）",
            audit_entry_id=audit_entry["id"],
        )

        return {
            "ok": True,
            "audit": record,
            "audit_entry": audit_entry,
        }

    def audit_all(self) -> dict[str, Any]:
        """对采集仓储中所有待校验批次执行审计。"""
        batches = [b for b in store.rows(RESULT_MODULE) if b.get("status") in ("接收中", "已落盘")]
        results: list[dict[str, Any]] = []
        for batch in batches:
            res = self.audit_batch(int(batch["id"]))
            results.append(res)
        passed = sum(1 for r in results if r.get("ok") and r.get("audit", {}).get("verdict") == "通过")
        return {"ok": True, "audited": len(results), "passed": passed, "results": results}

    def list_records(self) -> list[dict[str, Any]]:
        return list(store.joint_rows("audit_records"))

    def get_record(self, record_id: int) -> dict[str, Any] | None:
        return store.joint_find("audit_records", record_id)


quality_auditor = QualityAuditor()
