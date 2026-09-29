"""托育建设投运证据门领域服务。

口径要点：
- 工程竣工（MILESTONE_REPORTED）只是工程事实，班型开放（SCOPE_OPENED）必须
  满足 src.policy 的班型证据矩阵；不存在"竣工即投运"。
- 证据按"提交 → 职责人签署结论"生效；提交者不得签署自己提交的版本。
- 已签署结论不原地改写，纠正必须提交新版本；新版本失败会把旧版本上已发生
  的放款列入待追回。
- 证照过期、证据撤销、重大整改只暂停依赖该证据的班型。
- 同一放款回执重送幂等，不产生第二笔。
- 全部状态由只追加事件重放得到，进程重启后待复核、待追回不丢失。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from .envelope import validate_event
from . import policy
from .store import EventStore

EVENT_TYPES = (
    "PROJECT_REGISTERED",
    "MILESTONE_REPORTED",
    "EVIDENCE_SUBMITTED",
    "EVIDENCE_SIGNED",
    "EVIDENCE_REVOKED",
    "ISSUE_REPORTED",
    "ISSUE_RESOLVED",
    "SCOPE_OPENED",
    "SCOPE_SUSPENDED",
    "SCOPE_RESUMED",
    "FUNDS_DISBURSED",
    "FUNDS_RECALLED",
    "FUNDS_RECONCILED",
)


class DomainError(Exception):
    """业务规则拒绝。"""


def parse_at(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise DomainError(f"时间格式无效: {value}") from exc
    if parsed.tzinfo is None:
        raise DomainError("时间必须包含时区")
    return parsed


def _money(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise DomainError("金额必须是数字")
    return Decimal(str(value))


# --- 归约状态 ---------------------------------------------------------------

@dataclass
class _Version:
    number: int
    submitted_by: str
    valid_from: datetime
    valid_until: datetime | None
    submitted_at: datetime
    sign: dict[str, Any] | None = None


@dataclass
class _Chain:
    evidence_id: str
    category: str
    versions: dict[int, _Version] = field(default_factory=dict)
    current_version: int = 0
    revoked: dict[str, Any] | None = None


@dataclass
class _ScopeState:
    opened: bool = False
    opened_at: datetime | None = None
    active_reasons: set[str] = field(default_factory=set)


@dataclass
class ProjectState:
    project_id: str | None = None
    name: str | None = None
    investment_batch: str | None = None
    deadlines: dict[str, datetime] = field(default_factory=dict)
    milestones: dict[str, dict[str, Any]] = field(default_factory=dict)
    chains: dict[str, _Chain] = field(default_factory=dict)        # evidence_id
    category_index: dict[str, str] = field(default_factory=dict)  # category -> evidence_id
    issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    scopes: dict[str, _ScopeState] = field(default_factory=dict)
    disbursements: dict[str, dict[str, Any]] = field(default_factory=dict)  # receipt_id
    disbursement_order: list[str] = field(default_factory=list)
    recalled: dict[str, Decimal] = field(default_factory=dict)     # receipt_id -> 已追回
    events: list[dict[str, Any]] = field(default_factory=list)

    def require_registered(self) -> None:
        if self.project_id is None:
            raise DomainError("项目尚未登记")

    def chain_for_category(self, category: str) -> _Chain | None:
        evidence_id = self.category_index.get(category)
        return self.chains.get(evidence_id) if evidence_id else None


def _apply(state: ProjectState, event: dict[str, Any]) -> None:
    p = event["payload"]
    at = parse_at(event["occurred_at"])
    kind = event["event_type"]

    if kind == "PROJECT_REGISTERED":
        if state.project_id is not None:
            raise DomainError("项目已登记")
        state.project_id = p["project_id"]
        state.name = p["name"]
        state.investment_batch = p["investment_batch"]
        state.deadlines = {
            key: parse_at(value) for key, value in p["deadlines"].items()
        }
        state.scopes = {s: _ScopeState() for s in policy.SERVICE_SCOPES}

    elif kind == "MILESTONE_REPORTED":
        state.milestones[p["milestone"]] = {"at": parse_at(p["occurred_at"]), "by": p["reported_by"]}

    elif kind == "EVIDENCE_SUBMITTED":
        chain = state.chains.get(p["evidence_id"])
        if chain is None:
            chain = _Chain(evidence_id=p["evidence_id"], category=p["category"])
            state.chains[p["evidence_id"]] = chain
            if p["category"] in state.category_index:
                raise DomainError(f"证据类别已存在证据链: {p['category']}")
            state.category_index[p["category"]] = p["evidence_id"]
        elif chain.category != p["category"]:
            raise DomainError("证据链类别不可变更")
        number = p["version"]
        if number in chain.versions:
            raise DomainError("证据版本重复")
        if number != chain.current_version + 1:
            raise DomainError("证据版本必须连续递增")
        chain.current_version = number
        chain.versions[number] = _Version(
            number=number,
            submitted_by=p["submitted_by"],
            valid_from=parse_at(p["valid_from"]),
            valid_until=parse_at(p["valid_until"]) if p.get("valid_until") else None,
            submitted_at=at,
        )

    elif kind == "EVIDENCE_SIGNED":
        chain = state.chains[p["evidence_id"]]
        version = chain.versions[p["version"]]
        if version.sign is not None:
            raise DomainError("该版本已签署，纠正结论须提交新版本")
        version.sign = {
            "conclusion": p["conclusion"],
            "signer_id": p["signer_id"],
            "signer_role": p["signer_role"],
            "signed_at": at,
            "note": p.get("note"),
        }

    elif kind == "EVIDENCE_REVOKED":
        state.chains[p["evidence_id"]].revoked = {
            "reason": p["reason"],
            "revoked_by": p["revoked_by"],
            "revoked_at": at,
        }

    elif kind == "ISSUE_REPORTED":
        if p["issue_id"] in state.issues:
            raise DomainError("问题编号重复")
        state.issues[p["issue_id"]] = {
            "severity": p["severity"],
            "description": p["description"],
            "reported_by": p["reported_by"],
            "reported_at": at,
            "open": True,
        }

    elif kind == "ISSUE_RESOLVED":
        issue = state.issues[p["issue_id"]]
        issue["open"] = False
        issue["resolution"] = p["resolution"]
        issue["resolved_at"] = at

    elif kind == "SCOPE_OPENED":
        state.scopes[p["scope"]].opened = True
        state.scopes[p["scope"]].opened_at = at

    elif kind == "SCOPE_SUSPENDED":
        state.scopes[p["scope"]].active_reasons.add(p["reason_key"])

    elif kind == "SCOPE_RESUMED":
        for key in p["reason_keys"]:
            state.scopes[p["scope"]].active_reasons.discard(key)

    elif kind == "FUNDS_DISBURSED":
        record = dict(p)
        record["at"] = at
        state.disbursements[p["receipt_id"]] = record
        state.disbursement_order.append(p["receipt_id"])
        state.recalled.setdefault(p["receipt_id"], Decimal("0"))

    elif kind == "FUNDS_RECALLED":
        state.recalled[p["receipt_id"]] = state.recalled.get(p["receipt_id"], Decimal("0")) + _money(p["amount"])

    elif kind == "FUNDS_RECONCILED":
        # 结论文书留痕，不改变门状态。
        pass

    else:
        raise DomainError(f"未知事件: {kind}")


def replay(events: Iterable[dict[str, Any]]) -> ProjectState:
    """从事件流重放项目状态（故障恢复入口）。"""
    state = ProjectState()
    for event in events:
        _apply(state, event)
        state.events.append(event)
    return state


# --- 领域服务 ---------------------------------------------------------------

class ReadinessService:
    def __init__(self, store: EventStore | None = None) -> None:
        self.store = store or EventStore()

    # -- 基础 --

    def _load(self, project_id: str) -> ProjectState:
        return replay(self.store.load(project_id))

    def _emit(
        self, state: ProjectState, event_type: str, payload: dict[str, Any], at: datetime,
        aggregate_id: str | None = None,
    ) -> dict[str, Any]:
        event = {
            "event_id": f"{aggregate_id or state.project_id}-{len(state.events) + 1:04d}",
            "event_type": event_type,
            "occurred_at": at.isoformat(),
            "aggregate_id": aggregate_id or state.project_id,
            "version": len(state.events) + 1,
            "payload": payload,
        }
        errors = validate_event(event, set(EVENT_TYPES))
        if errors:
            raise DomainError("；".join(errors))
        _apply(state, event)
        state.events.append(event)
        self.store.append(event)
        return event

    @staticmethod
    def _evidence_effective(chain: _Chain, at: datetime) -> tuple[bool, str]:
        """当前生效口径：最新已签署版本为通过、在有效期内、未撤销。"""
        if chain.revoked is not None:
            return False, f"证据已撤销（{chain.revoked['reason']}）"
        signed_numbers = sorted(n for n, v in chain.versions.items() if v.sign is not None)
        if not signed_numbers:
            return False, "最新版本待签署"
        latest_signed = chain.versions[signed_numbers[-1]]
        sign = latest_signed.sign
        assert sign is not None
        if sign["conclusion"] != policy.CONCLUSION_PASS:
            return False, "最新签署结论为不通过"
        if at < latest_signed.valid_from:
            return False, "证据尚未生效"
        if latest_signed.valid_until and at > latest_signed.valid_until:
            return False, "证据已过有效期"
        return True, ""

    def _category_status(self, state: ProjectState, category: str, at: datetime) -> dict[str, Any]:
        chain = state.chain_for_category(category)
        if chain is None:
            return {"category": category, "effective": False, "reason": "证据未提交"}
        latest = chain.versions[chain.current_version]
        effective, reason = self._evidence_effective(chain, at)
        return {
            "category": category,
            "evidence_id": chain.evidence_id,
            "version": latest.number,
            "effective": effective,
            "reason": reason,
            "signed": latest.sign is not None,
            "conclusion": latest.sign["conclusion"] if latest.sign else None,
            "valid_until": latest.valid_until.isoformat() if latest.valid_until else None,
            "revoked": chain.revoked is not None,
        }

    def _blocked_reasons(self, state: ProjectState, scope: str, at: datetime) -> list[dict[str, str]]:
        blocked: list[dict[str, str]] = []
        for category in policy.SCOPE_REQUIREMENTS[scope]:
            status = self._category_status(state, category, at)
            if not status["effective"]:
                blocked.append({"category": category, "reason": status["reason"]})
        for issue in state.issues.values():
            if issue["open"] and issue["severity"] == policy.ISSUE_MAJOR:
                blocked.append({"category": "trial_run", "reason": f"存在未闭环重大问题: {issue['description']}"})
        return blocked

    # -- 命令 --

    def register_project(
        self,
        project_id: str,
        name: str,
        investment_batch: str,
        deadlines: dict[str, str],
        at: str | datetime,
    ) -> dict[str, Any]:
        when = parse_at(at)
        missing = [key for key in policy.DEADLINE_KINDS if key not in deadlines]
        if missing:
            raise DomainError(f"缺少时限: {', '.join(missing)}")
        state = self._load(project_id)
        if state.project_id is not None:
            raise DomainError("项目已登记")
        return self._emit(state, "PROJECT_REGISTERED", {
            "project_id": project_id,
            "name": name,
            "investment_batch": investment_batch,
            "deadlines": {key: deadlines[key] for key in policy.DEADLINE_KINDS},
        }, when, aggregate_id=project_id)

    def report_milestone(
        self, project_id: str, milestone: str, occurred_at: str, reported_by: str, at: str | datetime,
    ) -> dict[str, Any]:
        if milestone not in policy.MILESTONE_KINDS:
            raise DomainError(f"未知里程碑: {milestone}")
        state = self._load(project_id)
        state.require_registered()
        return self._emit(state, "MILESTONE_REPORTED", {
            "milestone": milestone,
            "occurred_at": parse_at(occurred_at).isoformat(),
            "reported_by": reported_by,
        }, parse_at(at))

    def submit_evidence(
        self,
        project_id: str,
        evidence_id: str,
        category: str,
        submitted_by: str,
        valid_from: str | datetime,
        at: str | datetime,
        valid_until: str | datetime | None = None,
    ) -> dict[str, Any]:
        if category not in policy.EVIDENCE_CATEGORIES:
            raise DomainError(f"未知证据类别: {category}")
        when = parse_at(at)
        state = self._load(project_id)
        state.require_registered()
        chain = state.chains.get(evidence_id)
        if chain is not None and chain.category != category:
            raise DomainError("证据链类别不可变更")
        existing = state.chain_for_category(category)
        if existing is not None and existing.evidence_id != evidence_id:
            raise DomainError(f"该类别已登记证据链 {existing.evidence_id}，纠正须沿用同一证据链提交新版本")
        version = (chain.current_version + 1) if chain is not None else 1
        if chain is not None and chain.revoked is not None:
            raise DomainError("证据链已撤销，不得再提新版本")
        return self._emit(state, "EVIDENCE_SUBMITTED", {
            "evidence_id": evidence_id,
            "category": category,
            "version": version,
            "submitted_by": submitted_by,
            "valid_from": parse_at(valid_from).isoformat(),
            "valid_until": parse_at(valid_until).isoformat() if valid_until else None,
        }, when)

    def sign_evidence(
        self,
        project_id: str,
        evidence_id: str,
        signer_id: str,
        signer_role: str,
        conclusion: str,
        at: str | datetime,
        note: str | None = None,
    ) -> dict[str, Any]:
        if conclusion not in policy.CONCLUSIONS:
            raise DomainError("结论只能是 pass/fail")
        when = parse_at(at)
        state = self._load(project_id)
        state.require_registered()
        chain = state.chains.get(evidence_id)
        if chain is None:
            raise DomainError("证据不存在")
        version = chain.versions.get(chain.current_version)
        assert version is not None
        if version.sign is not None:
            raise DomainError("最新版本已签署，纠正结论须提交新版本")
        if signer_role not in policy.SIGNING_ROLES[chain.category]:
            raise DomainError(f"职责 {signer_role} 无权签署 {chain.category}")
        if signer_id == version.submitted_by:
            raise DomainError("提交者不得签署自己提交的材料")
        if conclusion == policy.CONCLUSION_PASS and chain.category == policy.EV_TRIAL_CLEARANCE:
            open_major = [i for i in state.issues.values() if i["open"] and i["severity"] == policy.ISSUE_MAJOR]
            if open_major:
                raise DomainError("试运行仍有未闭环重大问题，不能签署清零结论")
        return self._emit(state, "EVIDENCE_SIGNED", {
            "evidence_id": evidence_id,
            "version": version.number,
            "conclusion": conclusion,
            "signer_id": signer_id,
            "signer_role": signer_role,
            "note": note,
        }, when)

    def revoke_evidence(
        self, project_id: str, evidence_id: str, reason: str, revoked_by: str, at: str | datetime,
    ) -> list[dict[str, Any]]:
        """撤销证据（如合作协议终止），并暂停依赖它的在开班型。"""
        if reason not in policy.REVOCATION_REASONS:
            raise DomainError(f"未知撤销原因: {reason}")
        when = parse_at(at)
        state = self._load(project_id)
        state.require_registered()
        chain = state.chains.get(evidence_id)
        if chain is None:
            raise DomainError("证据不存在")
        events: list[dict[str, Any]] = []
        if chain.revoked is None:
            events.append(self._emit(state, "EVIDENCE_REVOKED", {
                "evidence_id": evidence_id,
                "reason": reason,
                "revoked_by": revoked_by,
            }, when))
        events.extend(self._sync_scope_states(state, revoked_by, when))
        return events

    def report_issue(
        self,
        project_id: str,
        issue_id: str,
        severity: str,
        description: str,
        reported_by: str,
        at: str | datetime,
    ) -> list[dict[str, Any]]:
        if severity not in policy.ISSUE_SEVERITIES:
            raise DomainError("问题严重度只能是 minor/major")
        when = parse_at(at)
        state = self._load(project_id)
        state.require_registered()
        events = [self._emit(state, "ISSUE_REPORTED", {
            "issue_id": issue_id,
            "severity": severity,
            "description": description,
            "reported_by": reported_by,
        }, when)]
        if severity == policy.ISSUE_MAJOR:
            events.extend(self._sync_scope_states(state, reported_by, when))
        return events

    def resolve_issue(
        self, project_id: str, issue_id: str, resolution: str, at: str | datetime, actor_id: str,
    ) -> list[dict[str, Any]]:
        when = parse_at(at)
        state = self._load(project_id)
        state.require_registered()
        if issue_id not in state.issues:
            raise DomainError("问题不存在")
        events = [self._emit(state, "ISSUE_RESOLVED", {
            "issue_id": issue_id,
            "resolution": resolution,
        }, when)]
        events.extend(self._sync_scope_states(state, actor_id, when))
        return events

    def open_scope(self, project_id: str, scope: str, actor_id: str, at: str | datetime) -> dict[str, Any]:
        if scope not in policy.SERVICE_SCOPES:
            raise DomainError(f"未知班型: {scope}")
        when = parse_at(at)
        state = self._load(project_id)
        state.require_registered()
        scope_state = state.scopes[scope]
        if scope_state.opened:
            raise DomainError("班型已开放；暂停后恢复请走恢复口径")
        blocked = self._blocked_reasons(state, scope, when)
        if blocked:
            raise DomainError("证据未全部生效，拒绝开放: " + "；".join(
                f"{b['category']}（{b['reason']}）" for b in blocked
            ))
        return self._emit(state, "SCOPE_OPENED", {"scope": scope, "opened_by": actor_id}, when)

    def refresh_scope_states(self, project_id: str, actor_id: str, at: str | datetime) -> list[dict[str, Any]]:
        """按当前证据生效情况对齐班型暂停/恢复（定时批处理可重复调用，幂等）。"""
        when = parse_at(at)
        state = self._load(project_id)
        state.require_registered()
        return self._sync_scope_states(state, actor_id, when)

    def _sync_scope_states(self, state: ProjectState, actor_id: str, at: datetime) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for scope in policy.SERVICE_SCOPES:
            scope_state = state.scopes[scope]
            if not scope_state.opened:
                continue
            blocked = self._blocked_reasons(state, scope, at)
            wanted: dict[str, dict[str, str]] = {}
            for item in blocked:
                category = item["category"]
                if category == "trial_run":
                    key = "issue:major"
                    reason = policy.SUSPEND_RECTIFICATION
                else:
                    key = f"evidence:{category}"
                    reason = policy.CATEGORY_SUSPEND_REASON[category]
                wanted[key] = {"reason": reason, "detail": item["reason"]}
            for key, info in wanted.items():
                if key not in scope_state.active_reasons:
                    events.append(self._emit(state, "SCOPE_SUSPENDED", {
                        "scope": scope,
                        "reason": info["reason"],
                        "reason_key": key,
                        "detail": info["detail"],
                        "by": actor_id,
                    }, at))
            cleared = [key for key in list(scope_state.active_reasons) if key not in wanted]
            if cleared:
                events.append(self._emit(state, "SCOPE_RESUMED", {
                    "scope": scope,
                    "reason_keys": sorted(cleared),
                    "by": actor_id,
                }, at))
        return events

    def disburse_funds(
        self, project_id: str, receipt_id: str, amount: Any, at: str | datetime, note: str | None = None,
    ) -> dict[str, Any]:
        """放款：同一回执重送直接返回原事件，不重复放款；依据快照为当前生效证据版本。"""
        when = parse_at(at)
        value = _money(amount)
        if value <= 0:
            raise DomainError("放款金额必须为正")
        state = self._load(project_id)
        state.require_registered()
        if receipt_id in state.disbursements:
            return next(
                event for event in state.events
                if event["event_type"] == "FUNDS_DISBURSED"
                and event["payload"]["receipt_id"] == receipt_id
            )
        basis: list[dict[str, Any]] = []
        for category in policy.EVIDENCE_CATEGORIES:
            chain = state.chain_for_category(category)
            if chain is None:
                continue
            effective, _ = self._evidence_effective(chain, when)
            if effective:
                signed_numbers = sorted(n for n, v in chain.versions.items() if v.sign is not None)
                number = signed_numbers[-1]
                sign = chain.versions[number].sign
                assert sign is not None
                basis.append({
                    "category": category,
                    "evidence_id": chain.evidence_id,
                    "version": number,
                    "conclusion": sign["conclusion"],
                    "signed_at": sign["signed_at"].isoformat(),
                })
        return self._emit(state, "FUNDS_DISBURSED", {
            "receipt_id": receipt_id,
            "amount": float(value),
            "basis": basis,
            "note": note,
        }, when)

    def recall_funds(
        self,
        project_id: str,
        receipt_id: str,
        recall_receipt_id: str,
        amount: Any,
        reason: str,
        at: str | datetime,
    ) -> dict[str, Any]:
        """登记追回（如旧签署结论被新版本纠正）。追回金额不得超过未追回余额。"""
        when = parse_at(at)
        value = _money(amount)
        if value <= 0:
            raise DomainError("追回金额必须为正")
        state = self._load(project_id)
        state.require_registered()
        if receipt_id not in state.disbursements:
            raise DomainError("原放款回执不存在")
        for event in state.events:
            if event["event_type"] == "FUNDS_RECALLED" and event["payload"].get("recall_receipt_id") == recall_receipt_id:
                raise DomainError("追回回执重复")
        outstanding = _money(state.disbursements[receipt_id]["amount"]) - state.recalled.get(receipt_id, Decimal("0"))
        if value > outstanding:
            raise DomainError(f"追回金额超过未追回余额 {outstanding}")
        return self._emit(state, "FUNDS_RECALLED", {
            "receipt_id": receipt_id,
            "recall_receipt_id": recall_receipt_id,
            "amount": float(value),
            "reason": reason,
        }, when)

    # -- 查询 --

    def project_view(self, project_id: str, at: str | datetime) -> dict[str, Any]:
        when = parse_at(at)
        state = self._load(project_id)
        state.require_registered()

        deadline_view = {}
        for key, deadline in state.deadlines.items():
            deadline_view[key] = {
                "deadline": deadline.isoformat(),
                "remaining_days": (deadline - when).total_seconds() / 86400,
                "breached": when > deadline,
            }

        scopes_view = {}
        for scope in policy.SERVICE_SCOPES:
            scope_state = state.scopes[scope]
            blocked = self._blocked_reasons(state, scope, when) if scope_state.opened else []
            scopes_view[scope] = {
                "opened": scope_state.opened,
                "opened_at": scope_state.opened_at.isoformat() if scope_state.opened_at else None,
                "operational": scope_state.opened and not scope_state.active_reasons,
                "active_suspensions": sorted(scope_state.active_reasons),
                "blocking": blocked,
                "requirements": [
                    self._category_status(state, category, when)
                    for category in policy.SCOPE_REQUIREMENTS[scope]
                ],
            }

        return {
            "project_id": state.project_id,
            "name": state.name,
            "investment_batch": state.investment_batch,
            "viewed_at": when.isoformat(),
            "milestones": {
                kind: {"occurred_at": item["at"].isoformat(), "reported_by": item["by"]}
                for kind, item in state.milestones.items()
            },
            "deadlines": deadline_view,
            "scopes": scopes_view,
            "open_major_issues": [
                {"issue_id": key, "description": item["description"], "reported_at": item["reported_at"].isoformat()}
                for key, item in state.issues.items()
                if item["open"] and item["severity"] == policy.ISSUE_MAJOR
            ],
            "review_queue": self._review_queue(state, when),
            "pending_recovery": self._pending_recovery(state),
            "funds": self._funds_ledger(state),
        }

    def _review_queue(self, state: ProjectState, at: datetime) -> list[dict[str, Any]]:
        queue: list[dict[str, Any]] = []
        for chain in state.chains.values():
            latest = chain.versions[chain.current_version]
            if chain.revoked is not None:
                queue.append({
                    "type": "evidence_revoked",
                    "evidence_id": chain.evidence_id,
                    "category": chain.category,
                    "detail": f"证据已撤销（{chain.revoked['reason']}）",
                })
                continue
            if latest.sign is None:
                queue.append({
                    "type": "awaiting_signature",
                    "evidence_id": chain.evidence_id,
                    "category": chain.category,
                    "version": latest.number,
                    "detail": "新版本待职责人签署",
                })
            elif latest.sign["conclusion"] != policy.CONCLUSION_PASS:
                queue.append({
                    "type": "failed_correction",
                    "evidence_id": chain.evidence_id,
                    "category": chain.category,
                    "version": latest.number,
                    "detail": "签署不通过，需整改后提交新版本",
                })
            elif latest.valid_until and at > latest.valid_until:
                queue.append({
                    "type": "expired",
                    "evidence_id": chain.evidence_id,
                    "category": chain.category,
                    "version": latest.number,
                    "detail": f"已于 {latest.valid_until.isoformat()} 到期",
                })
        for issue_id, issue in state.issues.items():
            if issue["open"]:
                queue.append({
                    "type": "open_issue",
                    "issue_id": issue_id,
                    "severity": issue["severity"],
                    "detail": issue["description"],
                })
        return queue

    def _pending_recovery(self, state: ProjectState) -> list[dict[str, Any]]:
        """已放款但依据事后被新版本不通过结论或撤销影响的资金。"""
        items: list[dict[str, Any]] = []
        for receipt_id in state.disbursement_order:
            record = state.disbursements[receipt_id]
            causes: list[str] = []
            for base in record["basis"]:
                chain = state.chains.get(base["evidence_id"])
                if chain is None:
                    continue
                if chain.revoked is not None and chain.revoked["revoked_at"] > record["at"]:
                    causes.append(f"{base['category']} 证据已撤销")
                    continue
                for number, version in chain.versions.items():
                    if number <= base["version"] or version.sign is None:
                        continue
                    if version.sign["conclusion"] != policy.CONCLUSION_PASS:
                        causes.append(
                            f"{base['category']} 第{base['version']}版结论被第{number}版不通过结论纠正"
                        )
            if not causes:
                continue
            total = _money(record["amount"])
            recalled = state.recalled.get(receipt_id, Decimal("0"))
            outstanding = total - recalled
            if outstanding > 0:
                items.append({
                    "receipt_id": receipt_id,
                    "amount": float(total),
                    "outstanding": float(outstanding),
                    "causes": causes,
                    "disbursed_at": record["at"].isoformat(),
                })
        return items

    def _funds_ledger(self, state: ProjectState) -> list[dict[str, Any]]:
        ledger = []
        for receipt_id in state.disbursement_order:
            record = state.disbursements[receipt_id]
            total = _money(record["amount"])
            recalled = state.recalled.get(receipt_id, Decimal("0"))
            ledger.append({
                "receipt_id": receipt_id,
                "amount": float(total),
                "recalled": float(recalled),
                "outstanding": float(total - recalled),
                "disbursed_at": record["at"].isoformat(),
                "basis": record["basis"],
            })
        return ledger

    def list_projects(self) -> list[str]:
        if not self.store._dir:  # noqa: SLF001
            return []
        return sorted(path.stem for path in self.store._dir.glob("*.jsonl"))
