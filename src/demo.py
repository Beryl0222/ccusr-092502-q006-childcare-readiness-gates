"""端到端联调演示：一个托育综合服务中心从立项到投运、纠偏与追回的全过程。

运行：python3 -m src.demo
仅使用标准库，事件落盘到临时文件，结束时打印监管视图。
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import (
    CONSTRUCTION_COMPLETION, FULL_DAY, HALF_DAY, HOURLY,
    MILESTONE_COMPLETION, MILESTONE_START, OPERATIONS_PLAN, SAFETY_REGISTRATION,
)
from src.service import ReadinessService

TZ = timezone(timedelta(hours=8))


def main() -> None:
    tmp = tempfile.TemporaryDirectory()
    clock = {"at": datetime(2026, 9, 1, 9, 0, tzinfo=TZ)}
    svc = ReadinessService(Path(tmp.name) / "events.jsonl",
                           clock=lambda: clock["at"])
    pid = "demo-001"

    def send(command: str, **payload) -> dict:
        return svc.handle({"command": command,
                           "payload": {"project_id": pid, **payload}})

    # 1) 立项：中央投资批次 + 开工/竣工/投运时限
    send("register_project", name="河西区托育综合服务中心",
         batch="2026-中央投资-第二批",
         deadlines={
             "start_construction": "2026-09-11T09:00:00+08:00",
             "completion": "2026-10-11T09:00:00+08:00",
             "operation": "2026-10-26T09:00:00+08:00",
         })

    # 2) 里程碑上报
    clock["at"] += timedelta(days=10)
    send("report_milestone", kind=MILESTONE_START,
         occurred_at=clock["at"].isoformat(), reporter="建设单位-张工")
    clock["at"] += timedelta(days=28)
    send("report_milestone", kind=MILESTONE_COMPLETION,
         occurred_at=clock["at"].isoformat(), reporter="建设单位-张工")

    # 3) 四类证据备案（各有提交人与有效期）
    def record(eid: str, category: str, submitter: str, days: int) -> None:
        send("record_evidence", evidence_id=eid, category=category,
             submitted_by=submitter,
             valid_from=(clock["at"] - timedelta(days=1)).isoformat(),
             valid_until=(clock["at"] + timedelta(days=days)).isoformat())

    record("site-1", "site_acceptance", "建设单位-张工", 3650)
    record("roster-1", "staff_roster", "运营方-李主管", 365)
    record("fire-1", "fire_safety", "消防经办人-王", 365)
    record("food-1", "food_safety", "食堂经办人-赵", 60)

    # 4) 三条结论链由不同职责人签署；提交者不能签自己的材料
    send("sign_conclusion", kind=CONSTRUCTION_COMPLETION, signer="验收官-陈",
         result="pass", evidence_refs=["site-1"])
    send("sign_conclusion", kind=OPERATIONS_PLAN, signer="运营审查官-周",
         result="pass", evidence_refs=["roster-1"],
         staff_confirmed=[FULL_DAY, HALF_DAY])
    send("sign_conclusion", kind=SAFETY_REGISTRATION, signer="安全审查官-吴",
         result="pass", evidence_refs=["fire-1", "food-1"])

    # 5) 与妇幼机构的服务包协议按班型签署
    send("sign_protocol", protocol_id="proto-a", counterparty="河西区妇幼保健院",
         covers=[FULL_DAY, HALF_DAY], signed_by="院长-孙",
         valid_from=clock["at"].isoformat())
    send("sign_protocol", protocol_id="proto-b", counterparty="河西区妇幼保健院",
         covers=[HOURLY], signed_by="院长-孙",
         valid_from=clock["at"].isoformat())

    # 6) 全日托、半日托开放；计时托因运营方案未确认被阻断
    send("open_scope", service_type=FULL_DAY)
    send("open_scope", service_type=HALF_DAY)
    blocked = svc.project_view(pid)["scopes"][HOURLY]["blockers"]
    print("计时托阻断项：", [b["code"] for b in blocked])

    # 7) 运营方案新版本确认计时托后开放
    send("sign_conclusion", kind=OPERATIONS_PLAN, signer="运营审查官-周",
         result="pass", evidence_refs=["roster-1"],
         staff_confirmed=[FULL_DAY, HALF_DAY, HOURLY])
    send("open_scope", service_type=HOURLY)

    # 8) 凭安全备案结论放款；同一回执重送不重复放款
    send("disburse_funds", receipt_id="rcpt-safety-001", amount=800_000,
         gate_type="conclusion", gate_ref=SAFETY_REGISTRATION)
    again = send("disburse_funds", receipt_id="rcpt-safety-001", amount=800_000,
                 gate_type="conclusion", gate_ref=SAFETY_REGISTRATION)
    print("回执重送：", "幂等去重" if again["deduplicated"] else "重复放款（错误）")

    # 9) 食品证 60 天后到期：三个班型派生暂停，仅续办后才能恢复
    clock["at"] += timedelta(days=61)
    svc.sweep(pid)
    send("record_evidence", evidence_id="food-1", category="food_safety",
         submitted_by="食堂经办人-赵",
         valid_from=(clock["at"] - timedelta(days=1)).isoformat(),
         valid_until=(clock["at"] + timedelta(days=365)).isoformat())
    send("sign_conclusion", kind=SAFETY_REGISTRATION, signer="安全审查官-吴",
         result="pass", evidence_refs=["fire-1", "food-1"])
    for st in (FULL_DAY, HALF_DAY, HOURLY):
        send("resume_scope", service_type=st)

    # 10) 复查推翻旧安全结论：自动登记对 rcpt-safety-001 的待追回
    corrected = send("sign_conclusion", kind=SAFETY_REGISTRATION,
                     signer="安全审查官-吴", result="fail",
                     evidence_refs=["fire-1", "food-1"],
                     reason="复查发现消防验收材料造假")
    print("纠正产生事件：", [e["event_type"] for e in corrected["events"]])
    send("recover_funds", receipt_id="rcpt-safety-001", recovered_by="财政专员-冯")

    # 11) 监管视图
    view = svc.project_view(pid)
    summary = {
        "批次": view["batch"],
        "投运期限余量": view["deadlines"]["operation"].get("margin_human"),
        "各班型状态": {view["scopes"][s]["label"]: view["scopes"][s]["status"]
                   for s in (FULL_DAY, HALF_DAY, HOURLY)},
        "待复核项数": len(view["pending_review"]),
        "待追回": view["pending_recovery"],
        "已追回合计": view["funds"]["recovered_total"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # 12) 故障恢复演示
    revived = ReadinessService(svc.store.path, clock=lambda: clock["at"])
    assert revived.pending_recovery(pid) == svc.pending_recovery(pid) == []
    print("崩溃恢复后待复核/待追回事项一致，事件总数：",
          sum(len(v) for v in revived.store.load_all().values()))
    tmp.cleanup()


if __name__ == "__main__":
    main()
