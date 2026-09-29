"""托育综合服务中心建设投运证据门 —— 核心领域逻辑。

设计要点（与 README 领域边界一致）：

* 业务事实以领域事件表达；事件一经接收不可原地改写，纠正由后续版本事件完成。
* 聚合按项目（investment_project）组织；命令可在同一事务产生多条事件，版本连续。
* 证据（证照/材料）具有生效期 valid_from/valid_until，状态按时间点求值。
* 三条结论链（建设竣工验收、运营方案审查、消防食品等安全备案）必须由不同职责人
  签署，签署人不得是所引用材料的提交者。
* 班型（全日托/半日托/计时托）按依赖矩阵聚合：结论链、人员班型确认、服务包协议、
  试运营阻断问题，任一必要证据缺失或失效即阻断 —— 竣工不代表投运。
* 证照过期、重大整改、协议终止只暂停依赖它的班型，不株连其他服务范围。
* 同一回执（receipt_id 幂等键）重送不重复放款；
  新版本结论推翻旧的通过结论时，自动以 FUNDS_RECONCILED 反映已发生的资金影响，
  形成待追回事项；FUNDS_RECOVERED 登记追回到账。
* decide/replay 均为纯函数；时钟、事件 ID 与持久化由 service/store 层注入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
import copy

# ---------------------------------------------------------------------------
# 常量

FULL_DAY = "full_day"   # 全日托
HALF_DAY = "half_day"   # 半日托
HOURLY = "hourly"       # 计时托
SERVICE_TYPES = (FULL_DAY, HALF_DAY, HOURLY)

CONSTRUCTION_COMPLETION = "construction_completion"  # 建设部门工程竣工验收
OPERATIONS_PLAN = "operations_plan"                  # 运营方案审查（人员与班型）
SAFETY_REGISTRATION = "safety_registration"          # 消防、食品等安全备案
CONCLUSION_KINDS = (CONSTRUCTION_COMPLETION, OPERATIONS_PLAN, SAFETY_REGISTRATION)

# 每条结论链必须引用的证据类别
CONCLUSION_REQUIRED_CATEGORIES: dict[str, tuple[str, ...]] = {
    CONSTRUCTION_COMPLETION: ("site_acceptance",),
    OPERATIONS_PLAN: ("staff_roster",),
    SAFETY_REGISTRATION: ("fire_safety", "food_safety"),
}
EVIDENCE_CATEGORIES = (
    "site_acceptance", "staff_roster", "fire_safety", "food_safety",
)

MILESTONE_START = "start_construction"  # 开工
MILESTONE_COMPLETION = "completion"     # 竣工
MILESTONE_OPERATION = "operation"       # 投运
MILESTONE_KINDS = (MILESTONE_START, MILESTONE_COMPLETION, MILESTONE_OPERATION)

BLOCKING = "blocking"  # 试运营问题严重度：阻断性

# 放款闸门类型
GATE_MILESTONE = "milestone"
GATE_CONCLUSION = "conclusion"
GATE_SCOPE_OPEN = "scope_open"

# ---------------------------------------------------------------------------
# 事件名称

PROJECT_REGISTERED = "PROJECT_REGISTERED"
MILESTONE_REPORTED = "MILESTONE_REPORTED"
EVIDENCE_RECORDED = "EVIDENCE_RECORDED"
EVIDENCE_SIGNED = "EVIDENCE_SIGNED"
RECTIFICATION_REQUIRED = "RECTIFICATION_REQUIRED"
RECTIFICATION_CLEARED = "RECTIFICATION_CLEARED"
PROTOCOL_SIGNED = "PROTOCOL_SIGNED"
PROTOCOL_TERMINATED = "PROTOCOL_TERMINATED"
TRIAL_ISSUE_RAISED = "TRIAL_ISSUE_RAISED"
TRIAL_ISSUE_CLOSED = "TRIAL_ISSUE_CLOSED"
SCOPE_OPENED = "SCOPE_OPENED"
SCOPE_SUSPENDED = "SCOPE_SUSPENDED"
SCOPE_RESUMED = "SCOPE_RESUMED"
FUNDS_DISBURSED = "FUNDS_DISBURSED"
FUNDS_RECONCILED = "FUNDS_RECONCILED"
FUNDS_RECOVERED = "FUNDS_RECOVERED"

ALL_EVENTS = (
    PROJECT_REGISTERED, MILESTONE_REPORTED, EVIDENCE_RECORDED, EVIDENCE_SIGNED,
    RECTIFICATION_REQUIRED, RECTIFICATION_CLEARED,
    PROTOCOL_SIGNED, PROTOCOL_TERMINATED,
    TRIAL_ISSUE_RAISED, TRIAL_ISSUE_CLOSED,
    SCOPE_OPENED, SCOPE_SUSPENDED, SCOPE_RESUMED,
    FUNDS_DISBURSED, FUNDS_RECONCILED, FUNDS_RECOVERED,
)


class DomainError(ValueError):
    """业务规则被违反；消息可直接呈现给提交方。"""


@dataclass
class Context:
    now: Callable[[], datetime]
    new_id: Callable[[str], str]


# ---------------------------------------------------------------------------
# 状态

@dataclass
class ProjectState:
    project_id: str = ""
    name: str = ""
    batch: str = ""                       # 投资批次
    deadlines: dict[str, datetime] = field(default_factory=dict)
    milestones: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)    # evidence_id -> 记录
    conclusions: dict[str, dict[str, Any]] = field(default_factory=dict)  # kind -> 最新版本
    rectifications: list[dict[str, Any]] = field(default_factory=list)
    protocols: dict[str, dict[str, Any]] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    scopes: dict[str, dict[str, Any]] = field(default_factory=dict)  # service_type -> 生命周期
    disbursements: dict[str, dict[str, Any]] = field(default_factory=dict)  # receipt_id
    reconciliations: list[dict[str, Any]] = field(default_factory=list)
    version: int = 0

    @property
    def registered(self) -> bool:
        return bool(self.project_id)


# ---------------------------------------------------------------------------
# 纯函数：有效期与班型就绪判定

def _is_valid_period(valid_from: datetime | None,
                     valid_until: datetime | None, at: datetime) -> bool:
    if valid_from is not None and at < valid_from:
        return False
    if valid_until is not None and at >= valid_until:
        return False
    return True


def active_rectification(state: ProjectState, kind: str) -> dict[str, Any] | None:
    for item in state.rectifications:
        if item["kind"] == kind and item.get("active"):
            return item
    return None


def chain_status(state: ProjectState, kind: str, at: datetime) -> tuple[bool, str]:
    """返回某条结论链在 at 时点是否有效，以及不可用时的原因码。"""
    conclusion = state.conclusions.get(kind)
    if conclusion is None:
        return False, "conclusion_unsigned"
    if conclusion["result"] != "pass":
        return False, "conclusion_failed"
    if active_rectification(state, kind) is not None:
        return False, "rectification_active"
    for evidence_id in conclusion["evidence_refs"]:
        record = state.evidence.get(evidence_id)
        if record is None:
            return False, "evidence_missing"
        if not _is_valid_period(record["valid_from"], record["valid_until"], at):
            return False, "evidence_expired"
    return True, ""


def effective_protocol(state: ProjectState, service_type: str,
                       at: datetime) -> dict[str, Any] | None:
    for protocol in state.protocols.values():
        if service_type not in protocol["covers"]:
            continue
        if protocol.get("terminated_at") is not None:
            continue
        if _is_valid_period(protocol["valid_from"], protocol["valid_until"], at):
            return protocol
    return None


def open_blocking_issues(state: ProjectState, service_type: str) -> list[dict[str, Any]]:
    return [
        issue for issue in state.issues
        if issue.get("open") and issue["severity"] == BLOCKING
        and service_type in issue["service_types"]
    ]


def scope_blockers(state: ProjectState, service_type: str,
                   at: datetime) -> list[dict[str, str]]:
    """计算某班型在 at 时点的全部阻断项；空列表表示证据齐备、可以开放/恢复。"""
    blockers: list[dict[str, str]] = []
    for kind in CONCLUSION_KINDS:
        ok, reason = chain_status(state, kind, at)
        if not ok:
            blockers.append({"code": reason, "kind": kind,
                             "detail": _chain_reason_text(kind, reason)})
    operations = state.conclusions.get(OPERATIONS_PLAN)
    if operations is not None and operations["result"] == "pass" \
            and service_type not in operations.get("staff_confirmed", []):
        blockers.append({"code": "staff_unconfirmed", "kind": OPERATIONS_PLAN,
                         "detail": "运营方案未确认该班型的人员与班型配置"})
    if effective_protocol(state, service_type, at) is None:
        blockers.append({"code": "protocol_inactive", "kind": "service_package",
                         "detail": "该班型没有当前有效的服务包协议"})
    issues = open_blocking_issues(state, service_type)
    if issues:
        blockers.append({"code": "open_trial_issue", "kind": "trial_run",
                         "detail": f"存在 {len(issues)} 个未关闭的阻断性试运行问题"})
    return blockers


def _chain_reason_text(kind: str, reason: str) -> str:
    labels = {
        "construction_completion": "工程竣工验收",
        "operations_plan": "运营方案审查",
        "safety_registration": "消防食品等安全备案",
        "service_package": "服务包协议",
        "trial_run": "试运行",
    }
    reason_text = {
        "conclusion_unsigned": "尚未签署通过结论",
        "conclusion_failed": "结论为不通过",
        "rectification_active": "存在未消除的重大整改",
        "evidence_missing": "结论引用的证据材料缺失",
        "evidence_expired": "证照或材料已超出有效期",
    }
    return f"{labels.get(kind, kind)}：{reason_text.get(reason, reason)}"


def scope_effective_status(state: ProjectState, service_type: str,
                           at: datetime) -> str:
    """派生状态：never_opened / open / suspended。

    暂停既可能已有显式 SCOPE_SUSPENDED 事件，也可能因证照到期等时间因素派生，
    因此是否阻断一律以 scope_blockers 为准，保证崩溃恢复后口径一致。
    """
    lifecycle = state.scopes.get(service_type)
    if lifecycle is None:
        return "never_opened"
    if scope_blockers(state, service_type, at):
        return "suspended"
    return "open" if lifecycle["last"] in ("opened", "resumed") else "suspended"


# ---------------------------------------------------------------------------
# 命令决策：decide 返回 0..n 条事件（不含 envelope 版本信息）

def decide(ctx: Context, state: ProjectState, command: dict[str, Any]) -> list[dict[str, Any]]:
    cmd = command.get("command")
    handler = _HANDLERS.get(cmd)  # type: ignore[arg-type]
    if handler is None:
        raise DomainError(f"未知命令: {cmd}")
    return handler(ctx, state, command)


def _evt(event_type: str, payload: dict[str, Any], at: datetime) -> dict[str, Any]:
    return {"type": event_type, "occurred_at": at, "payload": payload}


# ---- 项目与里程碑 ---------------------------------------------------------

def _register_project(ctx: Context, state: ProjectState, c: dict[str, Any]):
    if state.registered:
        raise DomainError("项目已登记，不可重复登记")
    p = c["payload"]
    deadlines = {k: _parse_dt(v) for k, v in p["deadlines"].items()}
    for kind in MILESTONE_KINDS:
        if kind not in deadlines:
            raise DomainError(f"缺少时限: {kind}")
    if not (deadlines[MILESTONE_START] <= deadlines[MILESTONE_COMPLETION]
            <= deadlines[MILESTONE_OPERATION]):
        raise DomainError("时限顺序必须为 开工 <= 竣工 <= 投运")
    return [_evt(PROJECT_REGISTERED, {
        "project_id": p["project_id"], "name": p["name"], "batch": p["batch"],
        "deadlines": {k: deadlines[k].isoformat() for k in MILESTONE_KINDS},
    }, ctx.now())]


def _report_milestone(ctx: Context, state: ProjectState, c: dict[str, Any]):
    _require_registered(state)
    p = c["payload"]
    kind = p["kind"]
    if kind not in MILESTONE_KINDS:
        raise DomainError(f"未知里程碑类型: {kind}")
    at = _parse_dt(p["occurred_at"]) if p.get("occurred_at") else ctx.now()
    reporter = p["reporter"]
    if not reporter:
        raise DomainError("reporter 不能为空")
    payload = {"kind": kind, "occurred_at": at.isoformat(), "reporter": reporter}
    if kind in state.milestones:
        # 更正旧上报：以新版本事件表达，不原地改写
        payload["corrects"] = state.milestones[kind]["event_id"]
    return [_evt(MILESTONE_REPORTED, payload, ctx.now())]


# ---- 证据与结论 -----------------------------------------------------------

def _record_evidence(ctx: Context, state: ProjectState, c: dict[str, Any]):
    _require_registered(state)
    p = c["payload"]
    evidence_id, category = p["evidence_id"], p["category"]
    if category not in EVIDENCE_CATEGORIES:
        raise DomainError(f"未知证据类别: {category}")
    if not evidence_id or not p.get("submitted_by"):
        raise DomainError("evidence_id 与 submitted_by 不能为空")
    valid_from = _parse_dt(p["valid_from"])
    valid_until = _parse_dt(p["valid_until"]) if p.get("valid_until") else None
    if valid_until is not None and valid_until <= valid_from:
        raise DomainError("valid_until 必须晚于 valid_from")
    return [_evt(EVIDENCE_RECORDED, {
        "evidence_id": evidence_id, "category": category,
        "submitted_by": p["submitted_by"],
        "valid_from": valid_from.isoformat(),
        "valid_until": valid_until.isoformat() if valid_until else None,
        "note": p.get("note", ""),
        "supersedes": state.evidence.get(evidence_id, {}).get("event_id"),
    }, ctx.now())]


def _sign_conclusion(ctx: Context, state: ProjectState, c: dict[str, Any]):
    _require_registered(state)
    p = c["payload"]
    kind, signer, result = p["kind"], p["signer"], p["result"]
    if kind not in CONCLUSION_KINDS:
        raise DomainError(f"未知结论链: {kind}")
    if not signer:
        raise DomainError("signer 不能为空")
    if result not in ("pass", "fail"):
        raise DomainError("result 必须为 pass 或 fail")
    refs = list(p.get("evidence_refs", ()))
    required = CONCLUSION_REQUIRED_CATEGORIES[kind]
    at = ctx.now()
    by_category: dict[str, str] = {}
    for ref in refs:
        record = state.evidence.get(ref)
        if record is None:
            raise DomainError(f"引用的证据不存在: {ref}")
        # 职责分离：提交者不得批准自己的材料
        if record["submitted_by"] == signer:
            raise DomainError(
                f"签署人 {signer} 是证据 {ref} 的提交者，不得批准自己的材料")
        # 通过结论必须基于当前有效的证据；不通过结论用于复查失效/造假，不设此限
        if result == "pass" and not _is_valid_period(
                record["valid_from"], record["valid_until"], at):
            raise DomainError(f"证据 {ref} 当前不在有效期内，不能据此签署通过")
        by_category[record["category"]] = ref
    missing = [cat for cat in required if cat not in by_category]
    if missing:
        raise DomainError(f"结论缺少必要类别的证据: {', '.join(missing)}")
    staff_confirmed = [s for s in p.get("staff_confirmed", ()) if s in SERVICE_TYPES]
    if kind == OPERATIONS_PLAN and result == "pass" and not staff_confirmed:
        raise DomainError("运营方案通过时必须至少确认一个班型的人员配置")
    if result == "fail" and not p.get("reason"):
        raise DomainError("不通过结论必须填写原因")

    previous = state.conclusions.get(kind)
    event = _evt(EVIDENCE_SIGNED, {
        "kind": kind, "signer": signer, "result": result,
        "evidence_refs": refs, "staff_confirmed": staff_confirmed,
        "reason": p.get("reason", ""),
        "supersedes": previous["event_id"] if previous else None,
        "version_in_kind": (previous["version_in_kind"] + 1) if previous else 1,
    }, ctx.now())
    events = [event]

    # 新版本纠正旧的“通过”结论：对已据此放款的回执登记资金影响（待追回）
    if previous is not None and previous["result"] == "pass" and result == "fail":
        events.extend(_reconcile_affected_funds(state, kind, previous,
                                                p.get("reason", "结论被新版本纠正"),
                                                ctx))
    return events


def _require_rectification(ctx: Context, state: ProjectState, c: dict[str, Any]):
    _require_registered(state)
    p = c["payload"]
    if p["kind"] not in CONCLUSION_KINDS:
        raise DomainError(f"未知结论链: {p['kind']}")
    if not p.get("reason"):
        raise DomainError("整改要求必须说明原因")
    events = [_evt(RECTIFICATION_REQUIRED, {
        "rectification_id": ctx.new_id("rect"),
        "kind": p["kind"], "reason": p["reason"],
        "raised_by": p.get("raised_by", ""), "occurred_at": ctx.now().isoformat(),
        "active": True,
    }, ctx.now())]
    # 重大整改只暂停依赖该结论链的已开放班型
    projected = _state_with_events(state, events)
    extra = _suspend_dependent_scopes(ctx, projected, p["kind"], "重大整改")
    events.extend(extra)
    return events


def _clear_rectification(ctx: Context, state: ProjectState, c: dict[str, Any]):
    p = c["payload"]
    item = next((r for r in state.rectifications
                 if r["rectification_id"] == p["rectification_id"]), None)
    if item is None:
        raise DomainError("整改记录不存在")
    if not item.get("active"):
        raise DomainError("该整改已消除")
    return [_evt(RECTIFICATION_CLEARED, {
        "rectification_id": p["rectification_id"],
        "cleared_by": p.get("cleared_by", ""),
    }, ctx.now())]


# ---- 服务包协议 -----------------------------------------------------------

def _sign_protocol(ctx: Context, state: ProjectState, c: dict[str, Any]):
    _require_registered(state)
    p = c["payload"]
    covers = [s for s in p.get("covers", ()) if s in SERVICE_TYPES]
    if not covers:
        raise DomainError("协议至少覆盖一个班型")
    if not p.get("counterparty") or not p.get("signed_by"):
        raise DomainError("counterparty 与 signed_by 不能为空")
    valid_from = _parse_dt(p["valid_from"])
    valid_until = _parse_dt(p["valid_until"]) if p.get("valid_until") else None
    if valid_until is not None and valid_until <= valid_from:
        raise DomainError("valid_until 必须晚于 valid_from")
    return [_evt(PROTOCOL_SIGNED, {
        "protocol_id": p["protocol_id"], "counterparty": p["counterparty"],
        "covers": covers, "signed_by": p["signed_by"],
        "valid_from": valid_from.isoformat(),
        "valid_until": valid_until.isoformat() if valid_until else None,
        "terminated_at": None,
    }, ctx.now())]


def _terminate_protocol(ctx: Context, state: ProjectState, c: dict[str, Any]):
    p = c["payload"]
    protocol = state.protocols.get(p["protocol_id"])
    if protocol is None:
        raise DomainError("协议不存在")
    if protocol.get("terminated_at") is not None:
        raise DomainError("协议已终止")
    at = _parse_dt(p["occurred_at"]) if p.get("occurred_at") else ctx.now()
    events = [_evt(PROTOCOL_TERMINATED, {
        "protocol_id": p["protocol_id"],
        "reason": p.get("reason", ""),
        "terminated_at": at.isoformat(),
    }, at)]
    # 只暂停失去全部有效协议覆盖的班型
    affected = [s for s in SERVICE_TYPES
                if s in protocol["covers"]
                and effective_protocol(_state_with_events(state, events), s, at) is None]
    for service_type in affected:
        events.extend(_suspend_one(ctx, _state_with_events(state, events),
                                   service_type, "服务包协议终止", at))
    return events


# ---- 试运行问题 -----------------------------------------------------------

def _raise_issue(ctx: Context, state: ProjectState, c: dict[str, Any]):
    _require_registered(state)
    p = c["payload"]
    service_types = [s for s in p.get("service_types", ()) if s in SERVICE_TYPES]
    if not service_types:
        raise DomainError("问题至少关联一个班型")
    severity = p.get("severity", BLOCKING)
    if severity not in (BLOCKING, "major", "minor"):
        raise DomainError("未知严重度")
    if not p.get("description"):
        raise DomainError("问题描述不能为空")
    events = [_evt(TRIAL_ISSUE_RAISED, {
        "issue_id": ctx.new_id("issue"), "service_types": service_types,
        "severity": severity, "description": p["description"],
        "raised_by": p.get("raised_by", ""), "open": True,
    }, ctx.now())]
    if severity == BLOCKING:
        projected = _state_with_events(state, events)
        for service_type in service_types:
            events.extend(_suspend_one(ctx, projected, service_type,
                                       "阻断性试运行问题", ctx.now()))
    return events


def _close_issue(ctx: Context, state: ProjectState, c: dict[str, Any]):
    p = c["payload"]
    issue = next((i for i in state.issues if i["issue_id"] == p["issue_id"]), None)
    if issue is None:
        raise DomainError("问题不存在")
    if not issue.get("open"):
        raise DomainError("问题已关闭")
    return [_evt(TRIAL_ISSUE_CLOSED, {
        "issue_id": p["issue_id"], "closed_by": p.get("closed_by", ""),
    }, ctx.now())]


# ---- 班型开放 / 恢复 ------------------------------------------------------

def _open_scope(ctx: Context, state: ProjectState, c: dict[str, Any]):
    _require_registered(state)
    service_type = c["payload"]["service_type"]
    if service_type not in SERVICE_TYPES:
        raise DomainError(f"未知班型: {service_type}")
    blockers = scope_blockers(state, service_type, ctx.now())
    if blockers:
        raise DomainError("证据未全部有效，不能开放班型: "
                          + "；".join(b["detail"] for b in blockers))
    lifecycle = state.scopes.get(service_type)
    if lifecycle is not None and lifecycle["last"] in ("opened", "resumed"):
        raise DomainError("班型已开放")
    return [_evt(SCOPE_OPENED, {"service_type": service_type,
                                "opened_by": c["payload"].get("requested_by", "")},
                 ctx.now())]


def _resume_scope(ctx: Context, state: ProjectState, c: dict[str, Any]):
    service_type = c["payload"]["service_type"]
    lifecycle = state.scopes.get(service_type)
    if lifecycle is None:
        raise DomainError("班型从未开放，应使用开放流程")
    blockers = scope_blockers(state, service_type, ctx.now())
    if blockers:
        raise DomainError("阻断项尚未消除，不能恢复班型: "
                          + "；".join(b["detail"] for b in blockers))
    if lifecycle["last"] in ("opened", "resumed"):
        raise DomainError("班型当前未暂停")
    return [_evt(SCOPE_RESUMED, {"service_type": service_type,
                                 "resumed_by": c["payload"].get("requested_by", "")},
                 ctx.now())]


def sweep_suspensions(ctx: Context, state: ProjectState) -> list[dict[str, Any]]:
    """时间驱动巡检：把证照到期等派生暂停补记为显式审计事件（不改变阻断口径）。

    恢复不自动进行：证据续期或整改消除后，须显式走恢复流程并重新核验全部阻断项。
    """
    events: list[dict[str, Any]] = []
    projected = state
    at = ctx.now()
    for service_type in SERVICE_TYPES:
        lifecycle = projected.scopes.get(service_type)
        if lifecycle is None or lifecycle["last"] not in ("opened", "resumed"):
            continue
        blockers = scope_blockers(projected, service_type, at)
        if blockers:
            events.append(_evt(SCOPE_SUSPENDED, {
                "service_type": service_type, "reason": "证据失效巡检",
                "trigger": "sweep", "blockers": blockers,
            }, at))
            projected = _state_with_events(state, events)
    return events


# ---- 资金：放款 / 纠正影响 / 追回 -----------------------------------------

def _disburse_funds(ctx: Context, state: ProjectState, c: dict[str, Any]):
    _require_registered(state)
    p = c["payload"]
    receipt_id = p["receipt_id"]
    if receipt_id in state.disbursements:
        # 同一回执重送：不重复放款，返回空事件流
        return []
    amount = p["amount"]
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
        raise DomainError("amount 必须为正数")
    at = ctx.now()
    gate_type, gate_ref = p["gate_type"], p["gate_ref"]
    basis = _gate_basis(state, gate_type, gate_ref, at)
    if not basis["satisfied"]:
        raise DomainError(f"放款闸门未满足: {basis['reason']}")
    return [_evt(FUNDS_DISBURSED, {
        "receipt_id": receipt_id, "amount": amount,
        "gate_type": gate_type, "gate_ref": gate_ref,
        "basis": basis,
        "batch": state.batch,
    }, at)]


def _recover_funds(ctx: Context, state: ProjectState, c: dict[str, Any]):
    p = c["payload"]
    pending = next((r for r in state.reconciliations
                    if r["receipt_id"] == p["receipt_id"]
                    and not r.get("recovered")), None)
    if pending is None:
        raise DomainError("该回执没有待追回的资金纠正记录")
    amount = p.get("amount", pending["amount"])
    if amount != pending["amount"]:
        raise DomainError("追回金额须与纠正记录一致（暂不支持部分追回）")
    return [_evt(FUNDS_RECOVERED, {
        "receipt_id": p["receipt_id"],
        "reconciliation_event_id": pending["event_id"],
        "amount": amount,
        "recovered_by": p.get("recovered_by", ""),
    }, ctx.now())]


def _gate_basis(state: ProjectState, gate_type: str, gate_ref: Any,
                at: datetime) -> dict[str, Any]:
    """构造放款时的证据快照，使每笔资金日后都可追溯到当时的证据依据。"""
    basis: dict[str, Any] = {"gate_type": gate_type, "gate_ref": gate_ref,
                             "satisfied": False, "reason": "", "evidence": []}
    if gate_type == GATE_MILESTONE:
        milestone = state.milestones.get(gate_ref)
        if milestone is None:
            basis["reason"] = f"里程碑 {gate_ref} 尚未上报"
            return basis
        basis["satisfied"] = True
        basis["evidence"].append({"type": MILESTONE_REPORTED,
                                  "event_id": milestone["event_id"],
                                  "reported_at": milestone["occurred_at"]})
        return basis
    if gate_type == GATE_CONCLUSION:
        kinds = gate_ref if isinstance(gate_ref, (list, tuple)) else [gate_ref]
        for kind in kinds:
            ok, reason = chain_status(state, kind, at)
            if not ok:
                basis["reason"] = _chain_reason_text(kind, reason)
                return basis
            conclusion = state.conclusions[kind]
            basis["evidence"].append({
                "type": EVIDENCE_SIGNED, "kind": kind,
                "event_id": conclusion["event_id"],
                "signer": conclusion["signer"],
                "version_in_kind": conclusion["version_in_kind"],
                "evidence_refs": list(conclusion["evidence_refs"]),
                "validity": [
                    {"evidence_id": ref, "category": state.evidence[ref]["category"],
                     "valid_from": state.evidence[ref]["valid_from"].isoformat(),
                     "valid_until": (state.evidence[ref]["valid_until"].isoformat()
                                     if state.evidence[ref]["valid_until"] else None)}
                    for ref in conclusion["evidence_refs"]
                ],
            })
        basis["satisfied"] = True
        return basis
    if gate_type == GATE_SCOPE_OPEN:
        if scope_effective_status(state, gate_ref, at) != "open":
            basis["reason"] = f"班型 {gate_ref} 当前未开放"
            basis["blockers"] = scope_blockers(state, gate_ref, at)
            return basis
        basis["satisfied"] = True
        for kind in CONCLUSION_KINDS:
            conclusion = state.conclusions[kind]
            basis["evidence"].append({
                "type": EVIDENCE_SIGNED, "kind": kind,
                "event_id": conclusion["event_id"],
                "signer": conclusion["signer"]})
        return basis
    basis["reason"] = f"未知闸门类型: {gate_type}"
    return basis


def _reconcile_affected_funds(state: ProjectState, kind: str,
                              previous: dict[str, Any], reason: str,
                              ctx: Context) -> list[dict[str, Any]]:
    """找出闸门依赖该结论链、且尚未登记纠正的放款。

    按结论链 kind 而非具体版本匹配：续证重签会产生新版本继续确认同一条准入条件，
    资金依据快照里记录的是放款当时的版本，纠正在于“该链当前被推翻”。
    """
    already = {r["disbursement_event_id"] for r in state.reconciliations}
    events: list[dict[str, Any]] = []
    for receipt in state.disbursements.values():
        if receipt["event_id"] in already:
            continue
        if not _basis_depends_on_kind(receipt["basis"], kind):
            continue
        events.append(_evt(FUNDS_RECONCILED, {
            "receipt_id": receipt["receipt_id"],
            "disbursement_event_id": receipt["event_id"],
            "amount": receipt["amount"],
            "kind": kind,
            "reversed_conclusion_event_id": previous["event_id"],
            "funded_conclusion_event_ids": [
                item["event_id"] for item in receipt["basis"].get("evidence", [])
                if item.get("type") == EVIDENCE_SIGNED and item.get("kind") == kind
            ],
            "reason": reason,
            "recovery_required": True,
            "recovered": False,
        }, ctx.now()))
    return events


def _basis_depends_on_kind(basis: dict[str, Any], kind: str) -> bool:
    return any(item.get("type") == EVIDENCE_SIGNED and item.get("kind") == kind
               for item in basis.get("evidence", []))


# ---- 辅助：派生暂停事件 ---------------------------------------------------

def _suspend_dependent_scopes(ctx: Context, state: ProjectState, kind: str,
                              why: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for service_type in SERVICE_TYPES:
        lifecycle = state.scopes.get(service_type)
        if lifecycle is None or lifecycle["last"] not in ("opened", "resumed"):
            continue
        if not any(b.get("kind") == kind
                   for b in scope_blockers(_state_with_events(state, events),
                                           service_type, ctx.now())):
            continue
        events.extend(_suspend_one(ctx, _state_with_events(state, events),
                                   service_type, why, ctx.now(),
                                   trigger=f"rectification:{kind}"))
    return events


def _suspend_one(ctx: Context, state: ProjectState, service_type: str,
                 reason: str, at: datetime, trigger: str = "") -> list[dict[str, Any]]:
    lifecycle = state.scopes.get(service_type)
    if lifecycle is None or lifecycle["last"] not in ("opened", "resumed"):
        return []
    if not scope_blockers(state, service_type, at):
        return []
    return [_evt(SCOPE_SUSPENDED, {
        "service_type": service_type, "reason": reason, "trigger": trigger,
        "blockers": scope_blockers(state, service_type, at),
    }, at)]


def _state_with_events(state: ProjectState, events: list[dict[str, Any]]) -> ProjectState:
    """在副本上投影事件，decide 阶段绝不污染调用方状态。"""
    projected = copy.deepcopy(state)
    for i, event in enumerate(events):
        # decide 阶段的事件尚未分配持久 event_id，补临时标识供 replay 折叠
        event.setdefault("event_id", f"__provisional_{id(state):x}_{i}")
        projected = replay(projected, event)
    return projected


def _require_registered(state: ProjectState) -> None:
    if not state.registered:
        raise DomainError("项目尚未登记")


_HANDLERS = {
    "register_project": _register_project,
    "report_milestone": _report_milestone,
    "record_evidence": _record_evidence,
    "sign_conclusion": _sign_conclusion,
    "require_rectification": _require_rectification,
    "clear_rectification": _clear_rectification,
    "sign_protocol": _sign_protocol,
    "terminate_protocol": _terminate_protocol,
    "raise_issue": _raise_issue,
    "close_issue": _close_issue,
    "open_scope": _open_scope,
    "resume_scope": _resume_scope,
    "disburse_funds": _disburse_funds,
    "recover_funds": _recover_funds,
}


# ---------------------------------------------------------------------------
# replay：事件折叠（事件溯源 reducer）

def replay(state: ProjectState, event: dict[str, Any]) -> ProjectState:
    event_type = event["type"]
    p = event["payload"]
    at = event["occurred_at"]

    if event_type == PROJECT_REGISTERED:
        state.project_id = p["project_id"]
        state.name = p["name"]
        state.batch = p["batch"]
        state.deadlines = {k: _parse_dt(v) for k, v in p["deadlines"].items()}
    elif event_type == MILESTONE_REPORTED:
        state.milestones[p["kind"]] = {**p, "event_id": event["event_id"]}
    elif event_type == EVIDENCE_RECORDED:
        state.evidence[p["evidence_id"]] = {
            "evidence_id": p["evidence_id"], "category": p["category"],
            "submitted_by": p["submitted_by"],
            "valid_from": _parse_dt(p["valid_from"]),
            "valid_until": _parse_dt(p["valid_until"]) if p["valid_until"] else None,
            "note": p.get("note", ""),
            "event_id": event["event_id"],
        }
    elif event_type == EVIDENCE_SIGNED:
        state.conclusions[p["kind"]] = {
            "kind": p["kind"], "signer": p["signer"], "result": p["result"],
            "evidence_refs": list(p["evidence_refs"]),
            "staff_confirmed": list(p.get("staff_confirmed", ())),
            "reason": p.get("reason", ""),
            "version_in_kind": p["version_in_kind"],
            "event_id": event["event_id"], "signed_at": at,
        }
    elif event_type == RECTIFICATION_REQUIRED:
        state.rectifications.append({**p, "event_id": event["event_id"]})
    elif event_type == RECTIFICATION_CLEARED:
        for item in state.rectifications:
            if item["rectification_id"] == p["rectification_id"]:
                item["active"] = False
                item["cleared_event_id"] = event["event_id"]
    elif event_type == PROTOCOL_SIGNED:
        state.protocols[p["protocol_id"]] = {
            **p, "valid_from": _parse_dt(p["valid_from"]),
            "valid_until": _parse_dt(p["valid_until"]) if p["valid_until"] else None,
            "event_id": event["event_id"],
        }
    elif event_type == PROTOCOL_TERMINATED:
        protocol = state.protocols[p["protocol_id"]]
        protocol["terminated_at"] = _parse_dt(p["terminated_at"])
        protocol["terminate_event_id"] = event["event_id"]
        protocol["terminate_reason"] = p.get("reason", "")
    elif event_type == TRIAL_ISSUE_RAISED:
        state.issues.append({**p, "event_id": event["event_id"]})
    elif event_type == TRIAL_ISSUE_CLOSED:
        for issue in state.issues:
            if issue["issue_id"] == p["issue_id"]:
                issue["open"] = False
                issue["close_event_id"] = event["event_id"]
    elif event_type in (SCOPE_OPENED, SCOPE_SUSPENDED, SCOPE_RESUMED):
        lifecycle = state.scopes.setdefault(
            p["service_type"], {"last": None, "history": []})
        lifecycle["last"] = {SCOPE_OPENED: "opened", SCOPE_SUSPENDED: "suspended",
                             SCOPE_RESUMED: "resumed"}[event_type]
        lifecycle["history"].append({"event_id": event["event_id"],
                                     "type": event_type, "at": at})
    elif event_type == FUNDS_DISBURSED:
        state.disbursements[p["receipt_id"]] = {**p, "event_id": event["event_id"]}
    elif event_type == FUNDS_RECONCILED:
        state.reconciliations.append({**p, "event_id": event["event_id"]})
    elif event_type == FUNDS_RECOVERED:
        for item in state.reconciliations:
            if item["receipt_id"] == p["receipt_id"] and not item.get("recovered"):
                item["recovered"] = True
                item["recovery_event_id"] = event["event_id"]
    else:
        raise DomainError(f"replay 遇到未知事件: {event_type}")
    return state


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise DomainError("时间必须包含时区")
    return parsed


def new_state() -> ProjectState:
    return ProjectState()
