"""服务端外观：命令受理、事件信封包装、持久化事务与监管视图。

所有业务判定都在 domain.decide 中；本层只负责：
* 注入时钟与事件 ID（测试可固定）；
* 把领域事件包装成 contracts/domain.json 定义的信封并原子追加；
* 启动时重放事件日志，待复核/待追回事项因此在故障恢复后不丢失；
* 提供按项目的期限余量、阻断项、资金证据依据等只读监管视图。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import domain
from .domain import (
    CONCLUSION_KINDS, MILESTONE_KINDS, SERVICE_TYPES,
    Context, DomainError, ProjectState, chain_status, decide,
    new_state, replay, scope_blockers,
    scope_effective_status, sweep_suspensions,
)
from .store import EventStore

CN_TZ = timezone(timedelta(hours=8))
SERVICE_TYPE_LABELS = {
    "full_day": "全日托", "half_day": "半日托", "hourly": "计时托",
}
MILESTONE_LABELS = {
    "start_construction": "开工", "completion": "竣工", "operation": "投运",
}


def default_clock() -> datetime:
    return datetime.now(tz=CN_TZ)


class ReadinessService:
    def __init__(self, store: str | Path | EventStore,
                 clock: Callable[[], datetime] = default_clock):
        self.store = store if isinstance(store, EventStore) else EventStore(store)
        self._clock = clock
        self._states: dict[str, ProjectState] = {}
        self._project_index: dict[str, str] = {}
        self._loaded: dict[str, list[dict[str, Any]]] = {}
        self._reload()

    # ------------------------------------------------------------------
    # 恢复

    def _reload(self) -> None:
        self._states = {}
        self._project_index = {}
        self._loaded = self.store.load_all()
        for aggregate_id, envelopes in self._loaded.items():
            state = new_state()
            for env in envelopes:
                state.version = env["version"]
                replay(state, self._unwrap(env))
            self._states[aggregate_id] = state
            if state.project_id:
                self._project_index[state.project_id] = aggregate_id

    @staticmethod
    def _unwrap(env: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": env["event_id"],
            "type": env["event_type"],
            "occurred_at": datetime.fromisoformat(env["occurred_at"]),
            "payload": env["payload"],
        }

    # ------------------------------------------------------------------
    # 命令受理

    def handle(self, command: dict[str, Any]) -> dict[str, Any]:
        """受理一条命令并原子落盘其产生的事件。

        返回 {"events": [...信封...], "deduplicated": bool}。
        命令被拒绝时抛 DomainError，不写入任何事件。
        """
        payload = command.get("payload", {})
        project_id = payload.get("project_id")
        if command.get("command") == "register_project":
            aggregate_id = f"investment_project-{project_id}"
            if aggregate_id in self._states:
                raise DomainError("项目已登记，不可重复登记")
        else:
            if not project_id:
                raise DomainError("payload.project_id 不能为空")
            aggregate_id = self._project_index.get(project_id)
            if aggregate_id is None:
                raise DomainError(f"项目未登记: {project_id}")
        state = self._states.get(aggregate_id, new_state())
        ctx = Context(now=self._clock, new_id=self._new_id)
        events = decide(ctx, state, command)
        if not events:
            # 幂等重放（如同一回执重送）：不产生任何事件与放款
            return {"events": [], "deduplicated": True}
        envelopes = self._persist_events(aggregate_id, state, events)
        return {"events": envelopes, "deduplicated": False}

    def sweep(self, project_id: str) -> dict[str, Any]:
        """对单个项目执行证据失效巡检并落盘新增的暂停事件。"""
        aggregate_id = self._require_project(project_id)
        state = self._states[aggregate_id]
        ctx = Context(now=self._clock, new_id=self._new_id)
        events = sweep_suspensions(ctx, state)
        if not events:
            return {"events": [], "deduplicated": True}
        envelopes = self._persist_events(aggregate_id, state, events)
        return {"events": envelopes, "deduplicated": False}

    def _persist_events(self, aggregate_id: str, state: ProjectState,
                        events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        envelopes, next_version = [], state.version
        for event in events:
            next_version += 1
            envelopes.append({
                "event_id": self._new_id("evt"),
                "event_type": event["type"],
                "occurred_at": event["occurred_at"].isoformat(),
                "aggregate_id": aggregate_id,
                "version": next_version,
                "payload": event["payload"],
            })
        self.store.append_batch(envelopes)
        for env in envelopes:
            state.version = env["version"]
            replay(state, self._unwrap(env))
        self._states[aggregate_id] = state
        self._loaded.setdefault(aggregate_id, []).extend(envelopes)
        if state.project_id:
            self._project_index[state.project_id] = aggregate_id
        return envelopes

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    def _require_project(self, project_id: str) -> str:
        aggregate_id = self._project_index.get(project_id)
        if aggregate_id is None:
            raise DomainError(f"项目未登记: {project_id}")
        return aggregate_id

    def _state_of(self, project_id: str) -> ProjectState:
        return self._states[self._require_project(project_id)]

    # ------------------------------------------------------------------
    # 监管视图

    def project_view(self, project_id: str, at: datetime | None = None) -> dict[str, Any]:
        at = at or self._clock()
        state = self._state_of(project_id)
        return {
            "project_id": state.project_id,
            "name": state.name,
            "batch": state.batch,
            "as_of": at.isoformat(),
            "deadlines": self._deadline_view(state, at),
            "scopes": {
                st: self._scope_view(state, st, at) for st in SERVICE_TYPES
            },
            "conclusions": {
                kind: self._conclusion_view(state, kind, at)
                for kind in CONCLUSION_KINDS
            },
            "evidence": {
                eid: {
                    "category": rec["category"],
                    "submitted_by": rec["submitted_by"],
                    "valid_from": rec["valid_from"].isoformat(),
                    "valid_until": rec["valid_until"].isoformat()
                    if rec["valid_until"] else None,
                    "in_valid_period": _in_period(rec, at),
                    "event_id": rec["event_id"],
                }
                for eid, rec in state.evidence.items()
            },
            "protocols": self._protocol_view(state, at),
            "pending_review": self.pending_review(project_id, at),
            "pending_recovery": self.pending_recovery(project_id),
            "funds": self.funds_view(project_id),
        }

    def _deadline_view(self, state: ProjectState, at: datetime) -> dict[str, Any]:
        view = {}
        for kind in MILESTONE_KINDS:
            deadline = state.deadlines.get(kind)
            reported = state.milestones.get(kind)
            entry: dict[str, Any] = {
                "label": MILESTONE_LABELS[kind],
                "deadline": deadline.isoformat() if deadline else None,
                "reported": bool(reported),
                "reported_at": reported["occurred_at"] if reported else None,
            }
            if deadline is not None and not reported:
                margin = deadline - at
                entry["margin_seconds"] = int(margin.total_seconds())
                entry["margin_human"] = _human_delta(margin)
                entry["breached"] = margin < timedelta(0)
            elif deadline is not None and reported:
                overrun = datetime.fromisoformat(reported["occurred_at"]) - deadline
                entry["overrun_seconds"] = int(overrun.total_seconds())
                entry["on_time"] = overrun <= timedelta(0)
            view[kind] = entry
        return view

    def _scope_view(self, state: ProjectState, service_type: str,
                    at: datetime) -> dict[str, Any]:
        blockers = scope_blockers(state, service_type, at)
        return {
            "label": SERVICE_TYPE_LABELS[service_type],
            "status": scope_effective_status(state, service_type, at),
            "blockers": blockers,
            "ready": not blockers,
        }

    def _conclusion_view(self, state: ProjectState, kind: str,
                         at: datetime) -> dict[str, Any]:
        conclusion = state.conclusions.get(kind)
        ok, reason = chain_status(state, kind, at)
        return {
            "signed": conclusion is not None,
            "result": conclusion["result"] if conclusion else None,
            "signer": conclusion["signer"] if conclusion else None,
            "version_in_kind": conclusion["version_in_kind"] if conclusion else 0,
            "effective": ok,
            "ineffective_reason": reason or None,
            "event_id": conclusion["event_id"] if conclusion else None,
        }

    def _protocol_view(self, state: ProjectState, at: datetime) -> list[dict[str, Any]]:
        result = []
        for protocol in state.protocols.values():
            terminated = protocol.get("terminated_at") is not None
            in_period = (not terminated and _in_period(protocol, at))
            result.append({
                "protocol_id": protocol["protocol_id"],
                "counterparty": protocol["counterparty"],
                "covers": list(protocol["covers"]),
                "valid_until": protocol["valid_until"].isoformat()
                if protocol["valid_until"] else None,
                "terminated": terminated,
                "effective": in_period,
            })
        return result

    def pending_review(self, project_id: str,
                       at: datetime | None = None) -> list[dict[str, Any]]:
        """待复核工作清单 —— 全部由事件日志派生，恢复后自动重现。"""
        at = at or self._clock()
        state = self._state_of(project_id)
        items: list[dict[str, Any]] = []
        for item in state.rectifications:
            if item.get("active"):
                items.append({"type": "rectification",
                              "rectification_id": item["rectification_id"],
                              "kind": item["kind"], "reason": item["reason"],
                              "raised_at": item["occurred_at"]})
        for issue in state.issues:
            if issue.get("open"):
                items.append({"type": "trial_issue",
                              "issue_id": issue["issue_id"],
                              "severity": issue["severity"],
                              "service_types": issue["service_types"],
                              "description": issue["description"]})
        for kind in CONCLUSION_KINDS:
            conclusion = state.conclusions.get(kind)
            if conclusion is None:
                items.append({"type": "unsigned_conclusion", "kind": kind})
            elif conclusion["result"] == "fail":
                items.append({"type": "failed_conclusion", "kind": kind,
                              "reason": conclusion.get("reason", ""),
                              "event_id": conclusion["event_id"]})
            elif conclusion["result"] == "pass":
                for ref in conclusion["evidence_refs"]:
                    record = state.evidence.get(ref)
                    if record is not None and not _in_period(record, at):
                        items.append({"type": "evidence_expired", "kind": kind,
                                      "evidence_id": ref,
                                      "valid_until": record["valid_until"].isoformat()
                                      if record["valid_until"] else None})
        for service_type in SERVICE_TYPES:
            lifecycle = state.scopes.get(service_type)
            if lifecycle is not None and lifecycle["last"] == "suspended":
                items.append({"type": "suspended_scope",
                              "service_type": service_type,
                              "blockers": scope_blockers(state, service_type, at)})
        return items

    def pending_recovery(self, project_id: str) -> list[dict[str, Any]]:
        state = self._state_of(project_id)
        return [{
            "receipt_id": item["receipt_id"],
            "amount": item["amount"],
            "kind": item["kind"],
            "reason": item["reason"],
            "reconciliation_event_id": item["event_id"],
            "disbursement_event_id": item["disbursement_event_id"],
        } for item in state.reconciliations if not item.get("recovered")]

    def funds_view(self, project_id: str) -> dict[str, Any]:
        state = self._state_of(project_id)
        disbursed = [
            {"receipt_id": r["receipt_id"], "amount": r["amount"],
             "gate_type": r["gate_type"], "gate_ref": r["gate_ref"],
             "event_id": r["event_id"],
             "basis": r["basis"]}
            for r in state.disbursements.values()
        ]
        reconciliations = [
            {"receipt_id": r["receipt_id"], "amount": r["amount"],
             "reason": r["reason"], "recovered": r.get("recovered", False),
             "event_id": r["event_id"]}
            for r in state.reconciliations
        ]
        recovered_total = sum(
            r["amount"] for r in state.reconciliations if r.get("recovered"))
        outstanding_total = sum(
            r["amount"] for r in state.reconciliations if not r.get("recovered"))
        return {
            "disbursed": disbursed,
            "reconciliations": reconciliations,
            "recovered_total": recovered_total,
            "outstanding_recovery_total": outstanding_total,
        }

    # ------------------------------------------------------------------

    def list_projects(self) -> list[str]:
        return sorted(self._project_index)

    def export_jsonl(self) -> str:
        """便于联调：导出全部事件信封。"""
        lines = []
        for aggregate_id in sorted(self._loaded):
            for env in self._loaded[aggregate_id]:
                lines.append(json.dumps(env, ensure_ascii=False, sort_keys=True))
        return "\n".join(lines)


def _in_period(record: dict[str, Any], at: datetime) -> bool:
    if at < record["valid_from"]:
        return False
    if record.get("valid_until") is not None and at >= record["valid_until"]:
        return False
    return True


def _human_delta(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total == 0:
        return "已到期"
    sign = "-" if total < 0 else ""
    total = abs(total)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if not days and minutes:
        parts.append(f"{minutes}分钟")
    return sign + ("".join(parts) or "不足1分钟")
