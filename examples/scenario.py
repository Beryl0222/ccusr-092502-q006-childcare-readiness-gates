"""端到端联调场景：竣工不投运、分口径开放、证照失效隔离、放款追回。

运行：python3 -m examples.scenario
"""
from __future__ import annotations

import json

from src.readiness import ReadinessService
from src import policy

DAY = "2026-09-{:02d}T09:00:00+08:00"

SIGNERS = {
    policy.EV_MILESTONE_COMPLETION: ("u-construction", policy.ROLE_CONSTRUCTION),
    policy.EV_VENUE_ACCEPTANCE: ("u-venue", policy.ROLE_VENUE_INSPECTOR),
    policy.EV_FIRE_FILING: ("u-fire", policy.ROLE_FIRE_AUTHORITY),
    policy.EV_FOOD_FILING: ("u-food", policy.ROLE_FOOD_AUTHORITY),
    policy.EV_OPERATION_PLAN: ("u-regulator", policy.ROLE_REGULATOR),
    policy.EV_STAFF_QUALIFICATION: ("u-regulator", policy.ROLE_REGULATOR),
    policy.EV_SAFETY_SYSTEM: ("u-regulator2", policy.ROLE_REGULATOR),
    policy.EV_SERVICE_AGREEMENT: ("u-mc", policy.ROLE_MATERNAL_CHILD),
    policy.EV_TRIAL_CLEARANCE: ("u-regulator3", policy.ROLE_REGULATOR),
}


def main() -> None:
    svc = ReadinessService()
    pid = "investment_project-demo"
    svc.register_project(
        pid, "某市托育综合服务中心", "batch-2026-central-01",
        deadlines={
            "start_deadline": "2026-04-01T00:00:00+08:00",
            "completion_deadline": "2026-12-31T00:00:00+08:00",
            "operation_deadline": "2027-03-31T00:00:00+08:00",
        },
        at=DAY.format(10),
    )

    # 1) 工程开工、竣工 —— 此时没有任何班型开放
    svc.report_milestone(pid, policy.MILESTONE_STARTED, DAY.format(12), "u-construction", DAY.format(12))
    svc.report_milestone(pid, policy.MILESTONE_COMPLETED, DAY.format(18), "u-construction", DAY.format(18))

    # 2) 运营团队提交九类证据，各职责人（非提交者）签署
    for category in policy.EVIDENCE_CATEGORIES:
        valid_until = None
        if category == policy.EV_FIRE_FILING:
            valid_until = "2029-12-31T00:00:00+08:00"
        elif category == policy.EV_FOOD_FILING:
            valid_until = "2027-12-31T00:00:00+08:00"
        svc.submit_evidence(
            pid, f"ev-{category}", category, submitted_by="u-operator",
            valid_from=DAY.format(19), at=DAY.format(19), valid_until=valid_until,
        )
        uid, role = SIGNERS[category]
        svc.sign_evidence(pid, f"ev-{category}", uid, role, policy.CONCLUSION_PASS, DAY.format(20))

    # 3) 三个班型分别按口径开放
    for scope in policy.SERVICE_SCOPES:
        svc.open_scope(pid, scope, "u-regulator", DAY.format(21))

    # 4) 凭全部生效证据放款；回执重送幂等
    svc.disburse_funds(pid, "rcpt-001", "1000000.00", DAY.format(22), note="首批中央投资")
    svc.disburse_funds(pid, "rcpt-001", "1000000.00", DAY.format(22))  # 重送

    # 5) 消防备案复核：提交新版本，职责人签署不通过（旧结论保留）
    svc.submit_evidence(
        pid, "ev-fire_filing", policy.EV_FIRE_FILING, submitted_by="u-operator2",
        valid_from=DAY.format(27), at=DAY.format(27),
    )
    svc.sign_evidence(pid, "ev-fire_filing", "u-fire", policy.ROLE_FIRE_AUTHORITY, "fail", DAY.format(27))
    # 同步暂停：三个班型都依赖消防，全部暂停；rcpt-001 进入待追回
    svc.refresh_scope_states(pid, "scheduler", DAY.format(27))
    svc.recall_funds(pid, "rcpt-001", "recall-001", "400000", "消防备案复核不通过，按比例追回", DAY.format(28))

    view = svc.project_view(pid, DAY.format(28))
    summary = {
        "project": view["name"],
        "deadline_margin_days": {
            key: round(info["remaining_days"], 1) for key, info in view["deadlines"].items()
        },
        "scopes": {
            scope: {"operational": data["operational"], "suspended": data["active_suspensions"]}
            for scope, data in view["scopes"].items()
        },
        "review_queue": [item["type"] + ":" + item.get("category", item.get("issue_id", ""))
                         for item in view["review_queue"]],
        "funds": view["funds"],
        "pending_recovery": view["pending_recovery"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
