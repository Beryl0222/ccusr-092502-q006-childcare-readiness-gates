"""证据门业务规则测试（事件溯源 + 可恢复存储 + 监管视图）。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import (
    CONSTRUCTION_COMPLETION, EVIDENCE_SIGNED, FUNDS_DISBURSED,
    FUNDS_RECONCILED, FUNDS_RECOVERED, FULL_DAY, HALF_DAY, HOURLY,
    MILESTONE_COMPLETION, MILESTONE_OPERATION, MILESTONE_START,
    OPERATIONS_PLAN, SAFETY_REGISTRATION, SCOPE_OPENED, SCOPE_RESUMED,
    SCOPE_SUSPENDED, DomainError,
)
from src.service import ReadinessService

TZ = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 1, 9, 0, tzinfo=TZ)


def iso(dt: datetime) -> str:
    return dt.isoformat()


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.at = start

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kw) -> None:
        self.at += timedelta(**kw)


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.svc = ReadinessService(Path(self.tmp.name) / "events.jsonl",
                                    clock=self.clock)
        self.pid = "tj-001"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- 夹具 --------------------------------------------------------------

    def register(self, pid: str | None = None) -> None:
        pid = pid or self.pid
        self.svc.handle({
            "command": "register_project",
            "payload": {
                "project_id": pid, "name": "河西区托育综合服务中心",
                "batch": "2026-中央投资-第二批",
                "deadlines": {
                    MILESTONE_START: iso(T0 + timedelta(days=10)),
                    MILESTONE_COMPLETION: iso(T0 + timedelta(days=40)),
                    MILESTONE_OPERATION: iso(T0 + timedelta(days=55)),
                },
            },
        })

    def record_all_evidence(self, *, food_until: datetime | None = None) -> None:
        def rec(evidence_id: str, category: str, submitter: str,
                valid_from=T0 - timedelta(days=1), valid_until=None) -> None:
            self.svc.handle({"command": "record_evidence", "payload": {
                "project_id": self.pid, "evidence_id": evidence_id,
                "category": category, "submitted_by": submitter,
                "valid_from": iso(valid_from),
                "valid_until": iso(valid_until) if valid_until else None,
            }})

        rec("site-1", "site_acceptance", "建设单位-张工")
        rec("roster-1", "staff_roster", "运营方-李主管")
        rec("fire-1", "fire_safety", "消防经办人-王",
            valid_until=T0 + timedelta(days=365))
        rec("food-1", "food_safety", "食堂经办人-赵",
            valid_until=food_until or (T0 + timedelta(days=90)))

    def sign_all_conclusions(self, *, staff=(FULL_DAY, HALF_DAY, HOURLY),
                             safety_result="pass") -> None:
        self.svc.handle({"command": "sign_conclusion", "payload": {
            "project_id": self.pid, "kind": CONSTRUCTION_COMPLETION,
            "signer": "验收官-陈", "result": "pass", "evidence_refs": ["site-1"],
        }})
        self.svc.handle({"command": "sign_conclusion", "payload": {
            "project_id": self.pid, "kind": OPERATIONS_PLAN,
            "signer": "运营审查官-周", "result": "pass",
            "evidence_refs": ["roster-1"], "staff_confirmed": list(staff),
        }})
        payload = {"project_id": self.pid, "kind": SAFETY_REGISTRATION,
                   "signer": "安全审查官-吴", "result": safety_result,
                   "evidence_refs": ["fire-1", "food-1"]}
        if safety_result == "fail":
            payload["reason"] = "消防通道占用"
        self.svc.handle({"command": "sign_conclusion", "payload": payload})

    def sign_protocols(self) -> None:
        for pid, covers in (("proto-a", (FULL_DAY, HALF_DAY)),
                            ("proto-b", (HOURLY,))):
            self.svc.handle({"command": "sign_protocol", "payload": {
                "project_id": self.pid, "protocol_id": pid,
                "counterparty": "河西区妇幼保健院",
                "covers": list(covers), "signed_by": "院长-孙",
                "valid_from": iso(T0 - timedelta(days=1)),
            }})

    def open_everything(self) -> None:
        for st in (FULL_DAY, HALF_DAY, HOURLY):
            self.svc.handle({"command": "open_scope",
                             "payload": {"project_id": self.pid,
                                         "service_type": st}})

    def build_ready_project(self, **kw) -> None:
        self.register()
        self.record_all_evidence(**kw)
        self.sign_all_conclusions()
        self.sign_protocols()

    # -- 1. 竣工 != 投运 ---------------------------------------------------

    def test_completion_does_not_grant_operation(self) -> None:
        self.register()
        self.svc.handle({"command": "report_milestone", "payload": {
            "project_id": self.pid, "kind": MILESTONE_START,
            "occurred_at": iso(T0 + timedelta(days=12)), "reporter": "建设单位-张工"}})
        self.svc.handle({"command": "report_milestone", "payload": {
            "project_id": self.pid, "kind": MILESTONE_COMPLETION,
            "occurred_at": iso(T0 + timedelta(days=38)), "reporter": "建设单位-张工"}})
        self.record_all_evidence()
        # 只有竣工验收链通过
        self.svc.handle({"command": "sign_conclusion", "payload": {
            "project_id": self.pid, "kind": CONSTRUCTION_COMPLETION,
            "signer": "验收官-陈", "result": "pass", "evidence_refs": ["site-1"]}})

        # 工程已竣工，但全日托证据不齐，禁止放行
        with self.assertRaisesRegex(DomainError, "证据未全部有效"):
            self.svc.handle({"command": "open_scope",
                             "payload": {"project_id": self.pid,
                                         "service_type": FULL_DAY}})
        view = self.svc.project_view(self.pid)
        self.assertEqual(view["scopes"][FULL_DAY]["status"], "never_opened")
        codes = {b["code"] for b in view["scopes"][FULL_DAY]["blockers"]}
        self.assertIn("conclusion_unsigned", codes)
        self.assertIn("protocol_inactive", codes)

        # 竣工里程碑可以放款；但以班型开放为闸门的放款必须被拒
        ok = self.svc.handle({"command": "disburse_funds", "payload": {
            "project_id": self.pid, "receipt_id": "rcpt-1", "amount": 500,
            "gate_type": "milestone", "gate_ref": MILESTONE_COMPLETION}})
        self.assertEqual(ok["events"][0]["event_type"], FUNDS_DISBURSED)
        with self.assertRaisesRegex(DomainError, "放款闸门未满足"):
            self.svc.handle({"command": "disburse_funds", "payload": {
                "project_id": self.pid, "receipt_id": "rcpt-x", "amount": 100,
                "gate_type": "scope_open", "gate_ref": FULL_DAY}})

    # -- 2. 班型按各自证据分别开放 -----------------------------------------

    def test_scopes_open_independently_per_evidence(self) -> None:
        self.register()
        self.record_all_evidence()
        # 运营方案只确认全日托、半日托
        self.sign_all_conclusions(staff=(FULL_DAY, HALF_DAY))
        self.sign_protocols()

        self.svc.handle({"command": "open_scope",
                         "payload": {"project_id": self.pid,
                                     "service_type": FULL_DAY}})
        self.svc.handle({"command": "open_scope",
                         "payload": {"project_id": self.pid,
                                     "service_type": HALF_DAY}})
        with self.assertRaisesRegex(DomainError, "人员与班型配置"):
            self.svc.handle({"command": "open_scope",
                             "payload": {"project_id": self.pid,
                                         "service_type": HOURLY}})

        # 新版本运营方案补充确认计时托（旧结论不原地改写）
        self.svc.handle({"command": "sign_conclusion", "payload": {
            "project_id": self.pid, "kind": OPERATIONS_PLAN,
            "signer": "运营审查官-周", "result": "pass",
            "evidence_refs": ["roster-1"],
            "staff_confirmed": [FULL_DAY, HALF_DAY, HOURLY]}})
        result = self.svc.handle({"command": "open_scope",
                                  "payload": {"project_id": self.pid,
                                              "service_type": HOURLY}})
        self.assertEqual(result["events"][0]["event_type"], SCOPE_OPENED)

        view = self.svc.project_view(self.pid)
        self.assertTrue(all(view["scopes"][s]["ready"]
                            for s in (FULL_DAY, HALF_DAY, HOURLY)))
        self.assertEqual(view["conclusions"][OPERATIONS_PLAN]["version_in_kind"], 2)

    # -- 3. 职责分离 --------------------------------------------------------

    def test_submitter_cannot_approve_own_material(self) -> None:
        self.register()
        self.record_all_evidence()
        with self.assertRaisesRegex(DomainError, "不得批准自己的材料"):
            self.svc.handle({"command": "sign_conclusion", "payload": {
                "project_id": self.pid, "kind": CONSTRUCTION_COMPLETION,
                "signer": "建设单位-张工",  # 正是 site-1 的提交者
                "result": "pass", "evidence_refs": ["site-1"]}})

    def test_signing_requires_all_categories(self) -> None:
        self.register()
        self.record_all_evidence()
        with self.assertRaisesRegex(DomainError, "缺少必要类别"):
            self.svc.handle({"command": "sign_conclusion", "payload": {
                "project_id": self.pid, "kind": SAFETY_REGISTRATION,
                "signer": "安全审查官-吴", "result": "pass",
                "evidence_refs": ["fire-1"]}})  # 缺 food_safety

    # -- 4. 证照过期：派生暂停 + 续期后恢复 --------------------------------

    def test_expired_certificate_suspends_scopes_and_renewal_resumes(self) -> None:
        expiry = T0 + timedelta(days=30)
        self.build_ready_project(food_until=expiry)
        self.open_everything()

        # 到期时点：视图直接派生阻断，无需依赖批处理
        self.clock.advance(days=31)
        view = self.svc.project_view(self.pid)
        for st in (FULL_DAY, HALF_DAY, HOURLY):
            self.assertFalse(view["scopes"][st]["ready"])
            self.assertIn("evidence_expired",
                          {b["code"] for b in view["scopes"][st]["blockers"]})

        # 巡检把派生暂停补记为审计事件
        swept = self.svc.sweep(self.pid)
        self.assertEqual({e["event_type"] for e in swept["events"]},
                         {SCOPE_SUSPENDED})
        self.assertEqual(len(swept["events"]), 3)

        # 失效期间不能恢复
        with self.assertRaisesRegex(DomainError, "阻断项尚未消除"):
            self.svc.handle({"command": "resume_scope",
                             "payload": {"project_id": self.pid,
                                         "service_type": FULL_DAY}})

        # 续办食品证（同标识新版本，supersedes 旧版）并重新签署安全备案
        self.svc.handle({"command": "record_evidence", "payload": {
            "project_id": self.pid, "evidence_id": "food-1",
            "category": "food_safety", "submitted_by": "食堂经办人-赵",
            "valid_from": iso(self.clock.at - timedelta(days=1)),
            "valid_until": iso(self.clock.at + timedelta(days=180))}})
        self.svc.handle({"command": "sign_conclusion", "payload": {
            "project_id": self.pid, "kind": SAFETY_REGISTRATION,
            "signer": "安全审查官-吴", "result": "pass",
            "evidence_refs": ["fire-1", "food-1"]}})
        for st in (FULL_DAY, HALF_DAY, HOURLY):
            result = self.svc.handle({"command": "resume_scope",
                                      "payload": {"project_id": self.pid,
                                                  "service_type": st}})
            self.assertEqual(result["events"][0]["event_type"], SCOPE_RESUMED)
        view = self.svc.project_view(self.pid)
        self.assertTrue(all(view["scopes"][s]["status"] == "open"
                            for s in (FULL_DAY, HALF_DAY, HOURLY)))

    # -- 5. 重大整改：只暂停依赖链、消除后不自动恢复 -----------------------

    def test_rectification_scoped_suspension_and_manual_resume(self) -> None:
        self.build_ready_project()
        self.open_everything()
        result = self.svc.handle({"command": "require_rectification", "payload": {
            "project_id": self.pid, "kind": SAFETY_REGISTRATION,
            "reason": "消防演练记录缺失", "raised_by": "监督员-郑"}})
        types = [e["event_type"] for e in result["events"]]
        self.assertEqual(types.count(SCOPE_SUSPENDED), 3)

        view = self.svc.project_view(self.pid)
        self.assertTrue(any(i["type"] == "rectification"
                            for i in view["pending_review"]))

        # 整改消除不自动恢复，须显式恢复并重新核验
        rect_id = result["events"][0]["payload"]["rectification_id"]
        self.svc.handle({"command": "clear_rectification",
                         "payload": {"project_id": self.pid,
                                     "rectification_id": rect_id,
                                     "cleared_by": "监督员-郑"}})
        view = self.svc.project_view(self.pid)
        self.assertTrue(all(view["scopes"][s]["status"] == "suspended"
                            for s in (FULL_DAY, HALF_DAY, HOURLY)))
        result = self.svc.handle({"command": "resume_scope",
                                  "payload": {"project_id": self.pid,
                                              "service_type": FULL_DAY}})
        self.assertEqual(result["events"][0]["event_type"], SCOPE_RESUMED)

    # -- 6. 协议终止：只暂停失去覆盖的班型 ---------------------------------

    def test_protocol_termination_suspends_only_dependent_scope(self) -> None:
        self.build_ready_project()
        self.open_everything()
        result = self.svc.handle({"command": "terminate_protocol", "payload": {
            "project_id": self.pid, "protocol_id": "proto-b",
            "reason": "妇幼机构合作到期不续"}})
        self.assertEqual([e["event_type"] for e in result["events"]],
                         ["PROTOCOL_TERMINATED", SCOPE_SUSPENDED])
        self.assertEqual(result["events"][1]["payload"]["service_type"], HOURLY)
        view = self.svc.project_view(self.pid)
        self.assertEqual(view["scopes"][FULL_DAY]["status"], "open")
        self.assertEqual(view["scopes"][HALF_DAY]["status"], "open")
        self.assertEqual(view["scopes"][HOURLY]["status"], "suspended")

    # -- 7. 试运行问题 ------------------------------------------------------

    def test_blocking_trial_issue_scoped_nonblocking_does_not_suspend(self) -> None:
        self.build_ready_project()
        self.open_everything()
        self.svc.handle({"command": "raise_issue", "payload": {
            "project_id": self.pid, "service_types": [HOURLY],
            "severity": "blocking", "description": "计时托接送登记缺失",
            "raised_by": "督导员-钱"}})
        # 重大但非阻断问题不暂停
        self.svc.handle({"command": "raise_issue", "payload": {
            "project_id": self.pid, "service_types": [FULL_DAY],
            "severity": "major", "description": "晨检表格待优化",
            "raised_by": "督导员-钱"}})
        view = self.svc.project_view(self.pid)
        self.assertEqual(view["scopes"][HOURLY]["status"], "suspended")
        self.assertEqual(view["scopes"][FULL_DAY]["status"], "open")

        issue = next(i for i in view["pending_review"]
                     if i["type"] == "trial_issue"
                     and i["severity"] == "blocking")
        self.svc.handle({"command": "close_issue",
                         "payload": {"project_id": self.pid,
                                     "issue_id": issue["issue_id"]}})
        result = self.svc.handle({"command": "resume_scope",
                                  "payload": {"project_id": self.pid,
                                              "service_type": HOURLY}})
        self.assertEqual(result["events"][0]["event_type"], SCOPE_RESUMED)

    # -- 8. 回执幂等 --------------------------------------------------------

    def test_duplicate_receipt_does_not_disburse_twice(self) -> None:
        self.build_ready_project()
        cmd = {"command": "disburse_funds", "payload": {
            "project_id": self.pid, "receipt_id": "rcpt-100", "amount": 1000,
            "gate_type": "conclusion",
            "gate_ref": [CONSTRUCTION_COMPLETION, OPERATIONS_PLAN,
                         SAFETY_REGISTRATION]}}
        first = self.svc.handle(cmd)
        self.assertFalse(first["deduplicated"])
        second = self.svc.handle(dict(cmd))
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["events"], [])
        funds = self.svc.funds_view(self.pid)
        self.assertEqual(len(funds["disbursed"]), 1)

    def test_disbursement_blocked_while_gate_ineffective(self) -> None:
        self.register()
        with self.assertRaisesRegex(DomainError, "放款闸门未满足"):
            self.svc.handle({"command": "disburse_funds", "payload": {
                "project_id": self.pid, "receipt_id": "rcpt-x", "amount": 1,
                "gate_type": "conclusion",
                "gate_ref": SAFETY_REGISTRATION}})

    # -- 9. 纠正旧结论 -> 待追回 -> 追回到账 --------------------------------

    def test_correcting_passed_conclusion_creates_recovery(self) -> None:
        self.build_ready_project()
        self.svc.handle({"command": "disburse_funds", "payload": {
            "project_id": self.pid, "receipt_id": "rcpt-safety", "amount": 800,
            "gate_type": "conclusion", "gate_ref": SAFETY_REGISTRATION}})
        # 另一笔以竣工里程碑为闸门，不应受安全结论纠正影响
        self.svc.handle({"command": "report_milestone", "payload": {
            "project_id": self.pid, "kind": MILESTONE_COMPLETION,
            "occurred_at": iso(T0 + timedelta(days=38)),
            "reporter": "建设单位-张工"}})
        self.svc.handle({"command": "disburse_funds", "payload": {
            "project_id": self.pid, "receipt_id": "rcpt-build", "amount": 300,
            "gate_type": "milestone", "gate_ref": MILESTONE_COMPLETION}})

        # 新版本安全备案推翻旧的通过结论
        result = self.svc.handle({"command": "sign_conclusion", "payload": {
            "project_id": self.pid, "kind": SAFETY_REGISTRATION,
            "signer": "安全审查官-吴", "result": "fail",
            "evidence_refs": ["fire-1", "food-1"],
            "reason": "复查发现消防验收材料造假"}})
        types = [e["event_type"] for e in result["events"]]
        self.assertEqual(types, [EVIDENCE_SIGNED, FUNDS_RECONCILED])

        pending = self.svc.pending_recovery(self.pid)
        self.assertEqual([p["receipt_id"] for p in pending], ["rcpt-safety"])
        self.assertEqual(pending[0]["amount"], 800)
        funds = self.svc.funds_view(self.pid)
        self.assertEqual(funds["outstanding_recovery_total"], 800)
        self.assertEqual(len(funds["disbursed"]), 2)  # 原放款保留可溯

        # 再次不通过是合法新版本，但不再重复登记同一笔追回
        again = self.svc.handle({"command": "sign_conclusion", "payload": {
            "project_id": self.pid, "kind": SAFETY_REGISTRATION,
            "signer": "安全审查官-吴", "result": "fail",
            "evidence_refs": ["fire-1", "food-1"], "reason": "再次不通过"}})
        self.assertEqual([e["event_type"] for e in again["events"]],
                         [EVIDENCE_SIGNED])
        self.assertEqual(len(self.svc.pending_recovery(self.pid)), 1)

        # 追回到账
        result = self.svc.handle({"command": "recover_funds", "payload": {
            "project_id": self.pid, "receipt_id": "rcpt-safety",
            "recovered_by": "财政专员-冯"}})
        self.assertEqual(result["events"][0]["event_type"], FUNDS_RECOVERED)
        self.assertEqual(self.svc.pending_recovery(self.pid), [])
        self.assertEqual(self.svc.funds_view(self.pid)["recovered_total"], 800)

    # -- 10. 期限余量 -------------------------------------------------------

    def test_deadline_margins_and_overrun(self) -> None:
        self.register()
        view = self.svc.project_view(self.pid)
        start = view["deadlines"][MILESTONE_START]
        self.assertEqual(start["margin_human"], "10天")
        self.assertFalse(start["breached"])

        self.clock.advance(days=12)
        self.svc.handle({"command": "report_milestone", "payload": {
            "project_id": self.pid, "kind": MILESTONE_START,
            "occurred_at": iso(self.clock.at), "reporter": "建设单位-张工"}})
        view = self.svc.project_view(self.pid)
        self.assertFalse(view["deadlines"][MILESTONE_START]["on_time"])
        self.assertEqual(view["deadlines"][MILESTONE_START]["overrun_seconds"],
                         2 * 86400)
        self.assertIn("margin_seconds", view["deadlines"][MILESTONE_COMPLETION])

    # -- 11. 崩溃恢复 -------------------------------------------------------

    def test_recovery_reproduces_pending_review_and_recovery(self) -> None:
        self.build_ready_project()
        self.open_everything()
        self.svc.handle({"command": "disburse_funds", "payload": {
            "project_id": self.pid, "receipt_id": "rcpt-safety", "amount": 800,
            "gate_type": "conclusion", "gate_ref": SAFETY_REGISTRATION}})
        self.svc.handle({"command": "require_rectification", "payload": {
            "project_id": self.pid, "kind": SAFETY_REGISTRATION,
            "reason": "消防演练记录缺失", "raised_by": "监督员-郑"}})
        self.svc.handle({"command": "sign_conclusion", "payload": {
            "project_id": self.pid, "kind": SAFETY_REGISTRATION,
            "signer": "安全审查官-吴", "result": "fail",
            "evidence_refs": ["fire-1", "food-1"], "reason": "复查不通过"}})

        path = self.svc.store.path
        before = self.svc.project_view(self.pid)
        revived = ReadinessService(path, clock=self.clock)
        after = revived.project_view(self.pid)
        for key in ("pending_review", "pending_recovery", "funds", "scopes",
                    "deadlines", "conclusions"):
            self.assertEqual(before[key], after[key], f"恢复后 {key} 不一致")
        self.assertEqual(len(revived.pending_recovery(self.pid)), 1)

    def test_torn_tail_line_is_truncated_on_recovery(self) -> None:
        self.build_ready_project()
        self.open_everything()
        event_count_before = len(self.svc.store.load_all()[
            f"investment_project-{self.pid}"])
        path = self.svc.store.path
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"event_id": "evt-torn", "event_type": "SCOPE_')  # 断电撕裂
            fh.flush()
        revived = ReadinessService(path, clock=self.clock)
        self.assertEqual(revived.store.recovered_tail_events, 1)
        events = revived.store.load_all()[f"investment_project-{self.pid}"]
        self.assertEqual(len(events), event_count_before)
        # 恢复后服务可继续追加，版本不断裂
        result = revived.handle({"command": "raise_issue", "payload": {
            "project_id": self.pid, "service_types": [HOURLY],
            "severity": "minor", "description": "恢复后写入探针"}})
        self.assertFalse(result["deduplicated"])

    # -- 12. 信封版本连续且在合同清单内 ------------------------------------

    def test_envelopes_are_sequential_and_known_to_contract(self) -> None:
        contract = json.loads(
            (Path(__file__).resolve().parents[1] / "contracts" / "domain.json")
            .read_text(encoding="utf-8"))
        allowed = set(contract["events"])
        self.build_ready_project()
        envelopes = self.svc.store.load_all()[f"investment_project-{self.pid}"]
        self.assertEqual([e["version"] for e in envelopes],
                         list(range(1, len(envelopes) + 1)))
        for env in envelopes:
            self.assertIn(env["event_type"], allowed)
            for field in contract["envelope"]["required"]:
                self.assertIn(field, env)
        # 多事件命令内版本连续（签不通过结论同时产生纠正事件）
        self.svc.handle({"command": "disburse_funds", "payload": {
            "project_id": self.pid, "receipt_id": "rcpt-s", "amount": 10,
            "gate_type": "conclusion", "gate_ref": SAFETY_REGISTRATION}})
        self.svc.handle({"command": "sign_conclusion", "payload": {
            "project_id": self.pid, "kind": SAFETY_REGISTRATION,
            "signer": "安全审查官-吴", "result": "fail",
            "evidence_refs": ["fire-1", "food-1"], "reason": "x"}})
        envelopes = self.svc.store.load_all()[f"investment_project-{self.pid}"]
        self.assertEqual([e["version"] for e in envelopes],
                         list(range(1, len(envelopes) + 1)))

    # -- 13. 资金证据依据快照 ----------------------------------------------

    def test_funds_carry_evidence_basis_snapshot(self) -> None:
        self.build_ready_project()
        self.svc.handle({"command": "disburse_funds", "payload": {
            "project_id": self.pid, "receipt_id": "rcpt-1", "amount": 42,
            "gate_type": "conclusion", "gate_ref": SAFETY_REGISTRATION}})
        record = self.svc.funds_view(self.pid)["disbursed"][0]
        basis = record["basis"]
        self.assertTrue(basis["satisfied"])
        cited = basis["evidence"][0]
        self.assertEqual(cited["signer"], "安全审查官-吴")
        self.assertEqual({v["category"] for v in cited["validity"]},
                         {"fire_safety", "food_safety"})
        self.assertTrue(all(v["valid_until"] for v in cited["validity"]))


if __name__ == "__main__":
    unittest.main()
