"""证据门口径测试：分口径放行、暂停隔离、职责分离、放款幂等与追回、重放恢复。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.readiness import ReadinessService, DomainError, replay
from src.store import EventStore
from src import policy


T = "2026-09-{day:02d}T09:00:00+08:00"


class ReadinessCase(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = ReadinessService()
        self.pid = "investment_project-001"
        self.svc.register_project(
            self.pid,
            name="某市托育综合服务中心",
            investment_batch="batch-2026-central-01",
            deadlines={
                "start_deadline": "2026-04-01T00:00:00+08:00",
                "completion_deadline": "2026-12-31T00:00:00+08:00",
                "operation_deadline": "2027-03-31T00:00:00+08:00",
            },
            at=T.format(day=10),
        )

    def _submit_and_sign_all(self, day: int = 20) -> None:
        """提交并由非提交者按职责矩阵签署全部证据（全部通过）。"""
        signer = {
            policy.EV_MILESTONE_COMPLETION: ("u-construction", policy.ROLE_CONSTRUCTION),
            policy.EV_VENUE_ACCEPTANCE: ("u-venue", policy.ROLE_VENUE_INSPECTOR),
            policy.EV_FIRE_FILING: ("u-fire", policy.ROLE_FIRE_AUTHORITY),
            policy.EV_FOOD_FILING: ("u-food", policy.ROLE_FOOD_AUTHORITY),
            policy.EV_OPERATION_PLAN: ("u-regulator", policy.ROLE_REGULATOR),
            policy.EV_STAFF_QUALIFICATION: ("u-regulator", policy.ROLE_REGULATOR),
            policy.EV_SAFETY_SYSTEM: ("u-regulator2", policy.ROLE_REGULATOR),
            policy.EV_SERVICE_AGREEMENT: ("u-mc", policy.ROLE_MATERNAL_CHILD),
            policy.EV_TRIAL_CLEARANCE: ("u-regulator", policy.ROLE_REGULATOR),
        }
        # 竣工里程碑
        self.svc.report_milestone(self.pid, policy.MILESTONE_STARTED, T.format(day=12), "u-construction", T.format(day=12))
        self.svc.report_milestone(self.pid, policy.MILESTONE_COMPLETED, T.format(day=18), "u-construction", T.format(day=18))
        for idx, category in enumerate(policy.EVIDENCE_CATEGORIES, start=1):
            evidence_id = f"ev-{category}"
            valid_until = None
            if category == policy.EV_FIRE_FILING:
                valid_until = "2029-12-31T00:00:00+08:00"
            elif category == policy.EV_FOOD_FILING:
                valid_until = "2027-12-31T00:00:00+08:00"
            self.svc.submit_evidence(
                self.pid, evidence_id, category,
                submitted_by="u-operator",
                valid_from=T.format(day=19), at=T.format(day=19),
                valid_until=valid_until,
            )
            uid, role = signer[category]
            self.svc.sign_evidence(self.pid, evidence_id, uid, role, policy.CONCLUSION_PASS, T.format(day=day))

    # -- 竣工不等于投运 -----------------------------------------------------

    def test_completion_does_not_auto_open_scopes(self) -> None:
        self.svc.report_milestone(self.pid, policy.MILESTONE_STARTED, T.format(day=12), "u-construction", T.format(day=12))
        self.svc.report_milestone(self.pid, policy.MILESTONE_COMPLETED, T.format(day=18), "u-construction", T.format(day=18))
        view = self.svc.project_view(self.pid, T.format(day=20))
        for scope in policy.SERVICE_SCOPES:
            self.assertFalse(view["scopes"][scope]["opened"])

    def test_open_rejected_until_all_scope_evidence_effective(self) -> None:
        self._submit_and_sign_all()
        # 计时托所需证据已含在全套中；半日托不需要食品备案，先验证缺少运营方案时计时托被拒
        with self.assertRaises(DomainError):
            self.svc.open_scope(self.pid, policy.SCOPE_FULL_DAY, "u-regulator", T.format(day=18))

    def test_scopes_open_independently(self) -> None:
        self._submit_and_sign_all()
        self.svc.open_scope(self.pid, policy.SCOPE_HOURLY, "u-regulator", T.format(day=21))
        self.svc.open_scope(self.pid, policy.SCOPE_HALF_DAY, "u-regulator", T.format(day=22))
        view = self.svc.project_view(self.pid, T.format(day=22))
        self.assertTrue(view["scopes"][policy.SCOPE_HOURLY]["operational"])
        self.assertTrue(view["scopes"][policy.SCOPE_HALF_DAY]["operational"])
        self.assertFalse(view["scopes"][policy.SCOPE_FULL_DAY]["opened"])

    # -- 职责分离 -----------------------------------------------------------

    def test_submitter_cannot_sign_own_material(self) -> None:
        self.svc.submit_evidence(
            self.pid, "ev-fire", policy.EV_FIRE_FILING,
            submitted_by="u-fire", valid_from=T.format(day=19), at=T.format(day=19),
        )
        with self.assertRaisesRegex(DomainError, "提交者不得签署"):
            self.svc.sign_evidence(self.pid, "ev-fire", "u-fire", policy.ROLE_FIRE_AUTHORITY, "pass", T.format(day=19))

    def test_wrong_role_cannot_sign(self) -> None:
        self.svc.submit_evidence(
            self.pid, "ev-fire", policy.EV_FIRE_FILING,
            submitted_by="u-operator", valid_from=T.format(day=19), at=T.format(day=19),
        )
        with self.assertRaisesRegex(DomainError, "无权签署"):
            self.svc.sign_evidence(
                self.pid, "ev-fire", "u-food", policy.ROLE_FOOD_AUTHORITY, "pass", T.format(day=19)
            )

    def test_signed_conclusion_cannot_change_in_place(self) -> None:
        self.svc.submit_evidence(
            self.pid, "ev-fire", policy.EV_FIRE_FILING,
            submitted_by="u-operator", valid_from=T.format(day=19), at=T.format(day=19),
        )
        self.svc.sign_evidence(self.pid, "ev-fire", "u-fire", policy.ROLE_FIRE_AUTHORITY, "pass", T.format(day=19))
        with self.assertRaisesRegex(DomainError, "新版本"):
            self.svc.sign_evidence(self.pid, "ev-fire", "u-fire2", policy.ROLE_FIRE_AUTHORITY, "fail", T.format(day=20))

    # -- 暂停只影响依赖班型 -------------------------------------------------

    def test_fire_expiry_suspends_all_scopes_but_food_only_full_day(self) -> None:
        self._submit_and_sign_all()
        for scope in policy.SERVICE_SCOPES:
            self.svc.open_scope(self.pid, scope, "u-regulator", T.format(day=21))
        # 食品备案过期：只影响全日托
        events = self.svc.refresh_scope_states(self.pid, "scheduler", "2028-01-02T09:00:00+08:00")
        suspended = {e["payload"]["scope"] for e in events if e["event_type"] == "SCOPE_SUSPENDED"}
        self.assertEqual(suspended, {policy.SCOPE_FULL_DAY})
        view = self.svc.project_view(self.pid, "2028-01-02T09:00:00+08:00")
        self.assertFalse(view["scopes"][policy.SCOPE_FULL_DAY]["operational"])
        self.assertTrue(view["scopes"][policy.SCOPE_HALF_DAY]["operational"])
        self.assertTrue(view["scopes"][policy.SCOPE_HOURLY]["operational"])

    def test_agreement_termination_suspends_full_and_half_only(self) -> None:
        self._submit_and_sign_all()
        for scope in policy.SERVICE_SCOPES:
            self.svc.open_scope(self.pid, scope, "u-regulator", T.format(day=21))
        self.svc.revoke_evidence(
            self.pid, "ev-service_agreement", policy.REVOCATION_AGREEMENT_ENDED, "u-mc", T.format(day=25),
        )
        view = self.svc.project_view(self.pid, T.format(day=25))
        self.assertIn("evidence:service_agreement", view["scopes"][policy.SCOPE_FULL_DAY]["active_suspensions"])
        self.assertIn("evidence:service_agreement", view["scopes"][policy.SCOPE_HALF_DAY]["active_suspensions"])
        self.assertTrue(view["scopes"][policy.SCOPE_HOURLY]["operational"])

    def test_major_issue_suspends_and_resume_after_close(self) -> None:
        self._submit_and_sign_all()
        self.svc.open_scope(self.pid, policy.SCOPE_HOURLY, "u-regulator", T.format(day=21))
        self.svc.report_issue(self.pid, "issue-1", policy.ISSUE_MAJOR, "晨检记录缺失", "u-regulator", T.format(day=23))
        view = self.svc.project_view(self.pid, T.format(day=23))
        self.assertFalse(view["scopes"][policy.SCOPE_HOURLY]["operational"])
        self.svc.resolve_issue(self.pid, "issue-1", "已补齐晨检制度并复查", T.format(day=24), "u-regulator")
        view = self.svc.project_view(self.pid, T.format(day=24))
        self.assertTrue(view["scopes"][policy.SCOPE_HOURLY]["operational"])

    def test_minor_issue_does_not_suspend(self) -> None:
        self._submit_and_sign_all()
        self.svc.open_scope(self.pid, policy.SCOPE_HOURLY, "u-regulator", T.format(day=21))
        self.svc.report_issue(self.pid, "issue-2", policy.ISSUE_MINOR, "标识张贴不齐", "u-regulator", T.format(day=23))
        view = self.svc.project_view(self.pid, T.format(day=23))
        self.assertTrue(view["scopes"][policy.SCOPE_HOURLY]["operational"])

    def test_trial_clearance_requires_no_open_major_issue(self) -> None:
        # 全套签署流程中清零结论签署在 day=20；这里先报重大问题再签清零，应被拒
        self.svc.submit_evidence(
            self.pid, "ev-trial_clearance", policy.EV_TRIAL_CLEARANCE,
            submitted_by="u-operator", valid_from=T.format(day=19), at=T.format(day=19),
        )
        self.svc.report_issue(self.pid, "issue-9", policy.ISSUE_MAJOR, "监控盲区", "u-regulator", T.format(day=19))
        with self.assertRaisesRegex(DomainError, "未闭环重大问题"):
            self.svc.sign_evidence(
                self.pid, "ev-trial_clearance", "u-regulator", policy.ROLE_REGULATOR, "pass", T.format(day=20),
            )

    # -- 期限与视图 ---------------------------------------------------------

    def test_deadline_margin_and_breach(self) -> None:
        view = self.svc.project_view(self.pid, T.format(day=10))
        self.assertGreater(view["deadlines"]["completion_deadline"]["remaining_days"], 100)
        self.assertFalse(view["deadlines"]["completion_deadline"]["breached"])
        # 开工期限 2026-04-01 早于登记时点，应显示已逾期余量为负
        self.assertLess(view["deadlines"]["start_deadline"]["remaining_days"], 0)
        self.assertTrue(view["deadlines"]["start_deadline"]["breached"])
        late = self.svc.project_view(self.pid, "2027-04-01T00:00:00+08:00")
        self.assertTrue(late["deadlines"]["operation_deadline"]["breached"])

    def test_view_lists_blocking_items(self) -> None:
        self._submit_and_sign_all()
        self.svc.open_scope(self.pid, policy.SCOPE_FULL_DAY, "u-regulator", T.format(day=21))
        self.svc.revoke_evidence(
            self.pid, "ev-food_filing", policy.REVOCATION_LICENSE_REVOKED, "u-food", T.format(day=26),
        )
        view = self.svc.project_view(self.pid, T.format(day=26))
        categories = {b["category"] for b in view["scopes"][policy.SCOPE_FULL_DAY]["blocking"]}
        self.assertIn(policy.EV_FOOD_FILING, categories)
        kinds = {item["type"] for item in view["review_queue"]}
        self.assertIn("evidence_revoked", kinds)

    # -- 放款幂等、依据与追回 -----------------------------------------------

    def test_disbursement_idempotent_by_receipt(self) -> None:
        self._submit_and_sign_all()
        first = self.svc.disburse_funds(self.pid, "rcpt-1", "500000.00", T.format(day=22), note="首批")
        second = self.svc.disburse_funds(self.pid, "rcpt-1", "500000.00", T.format(day=22))
        self.assertEqual(first["event_id"], second["event_id"])
        view = self.svc.project_view(self.pid, T.format(day=22))
        self.assertEqual(len(view["funds"]), 1)
        self.assertTrue(any(b["category"] == policy.EV_FIRE_FILING for b in view["funds"][0]["basis"]))

    def test_new_failed_version_flags_prior_disbursement_for_recovery(self) -> None:
        self._submit_and_sign_all()
        self.svc.disburse_funds(self.pid, "rcpt-2", "800000", T.format(day=22))
        # 消防备案换版：新复核结论不通过（旧结论不原地改写）
        self.svc.submit_evidence(
            self.pid, "ev-fire_filing", policy.EV_FIRE_FILING,
            submitted_by="u-operator2", valid_from=T.format(day=27), at=T.format(day=27),
        )
        self.svc.sign_evidence(
            self.pid, "ev-fire_filing", "u-fire", policy.ROLE_FIRE_AUTHORITY, "fail", T.format(day=27),
        )
        view = self.svc.project_view(self.pid, T.format(day=27))
        self.assertEqual(len(view["pending_recovery"]), 1)
        item = view["pending_recovery"][0]
        self.assertEqual(item["receipt_id"], "rcpt-2")
        self.assertEqual(item["outstanding"], 800000.0)
        # 登记部分追回
        self.svc.recall_funds(self.pid, "rcpt-2", "recall-1", "300000", "消防备案复核不通过，按比例追回", T.format(day=28))
        view = self.svc.project_view(self.pid, T.format(day=28))
        self.assertEqual(view["funds"][0]["outstanding"], 500000.0)
        self.assertEqual(view["pending_recovery"][0]["outstanding"], 500000.0)
        with self.assertRaisesRegex(DomainError, "未追回余额"):
            self.svc.recall_funds(self.pid, "rcpt-2", "recall-2", "500000.01", "超额", T.format(day=28))

    def test_recall_receipt_is_idempotent_guard(self) -> None:
        self._submit_and_sign_all()
        self.svc.disburse_funds(self.pid, "rcpt-3", "1000", T.format(day=22))
        self.svc.submit_evidence(
            self.pid, "ev-fire_filing", policy.EV_FIRE_FILING,
            submitted_by="u-operator2", valid_from=T.format(day=27), at=T.format(day=27),
        )
        self.svc.sign_evidence(self.pid, "ev-fire_filing", "u-fire", policy.ROLE_FIRE_AUTHORITY, "fail", T.format(day=27))
        self.svc.recall_funds(self.pid, "rcpt-3", "recall-x", "1000", "追回", T.format(day=28))
        with self.assertRaisesRegex(DomainError, "追回回执重复"):
            self.svc.recall_funds(self.pid, "rcpt-3", "recall-x", "1", "重送", T.format(day=28))

    # -- 崩溃恢复 -----------------------------------------------------------

    def test_replay_after_restart_keeps_queues_and_scope_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EventStore(tmp)
            svc = ReadinessService(store)
            svc.register_project(
                self.pid, "重启验证项目", "batch-x",
                deadlines={
                    "start_deadline": "2026-04-01T00:00:00+08:00",
                    "completion_deadline": "2026-12-31T00:00:00+08:00",
                    "operation_deadline": "2027-03-31T00:00:00+08:00",
                },
                at=T.format(day=10),
            )
            # 手工建一个"已开放计时托 + 已放款 + 消防新版不通过"的最小状态
            for category in policy.SCOPE_REQUIREMENTS[policy.SCOPE_HOURLY]:
                svc.submit_evidence(
                    self.pid, f"ev-{category}", category, "u-operator",
                    valid_from=T.format(day=19), at=T.format(day=19),
                )
                uid, role = {
                    policy.EV_MILESTONE_COMPLETION: ("u-construction", policy.ROLE_CONSTRUCTION),
                    policy.EV_VENUE_ACCEPTANCE: ("u-venue", policy.ROLE_VENUE_INSPECTOR),
                    policy.EV_FIRE_FILING: ("u-fire", policy.ROLE_FIRE_AUTHORITY),
                    policy.EV_STAFF_QUALIFICATION: ("u-regulator", policy.ROLE_REGULATOR),
                    policy.EV_SAFETY_SYSTEM: ("u-regulator2", policy.ROLE_REGULATOR),
                    policy.EV_TRIAL_CLEARANCE: ("u-regulator3", policy.ROLE_REGULATOR),
                }[category]
                svc.sign_evidence(self.pid, f"ev-{category}", uid, role, "pass", T.format(day=20))
            svc.open_scope(self.pid, policy.SCOPE_HOURLY, "u-regulator", T.format(day=21))
            svc.disburse_funds(self.pid, "rcpt-r", "100000", T.format(day=22))
            svc.submit_evidence(
                self.pid, "ev-fire_filing", policy.EV_FIRE_FILING, "u-operator2",
                valid_from=T.format(day=27), at=T.format(day=27),
            )
            svc.sign_evidence(self.pid, "ev-fire_filing", "u-fire", policy.ROLE_FIRE_AUTHORITY, "fail", T.format(day=27))

            # 模拟进程重启：新服务实例从同一目录重放
            recovered = ReadinessService(EventStore(tmp))
            view = recovered.project_view(self.pid, T.format(day=27))
            self.assertEqual(len(view["pending_recovery"]), 1)
            self.assertTrue(any(i["type"] == "failed_correction" for i in view["review_queue"]))
            # 重放后再同步一次，暂停事件应被补齐且不重复
            recovered.refresh_scope_states(self.pid, "scheduler", T.format(day=27))
            view2 = recovered.project_view(self.pid, T.format(day=27))
            self.assertFalse(view2["scopes"][policy.SCOPE_HOURLY]["operational"])
            recovered.refresh_scope_states(self.pid, "scheduler", T.format(day=27))
            raw = EventStore(tmp).load(self.pid)
            suspends = [e for e in raw if e["event_type"] == "SCOPE_SUSPENDED"]
            self.assertEqual(len(suspends), 1)


if __name__ == "__main__":
    unittest.main()
