"""证据门口径：班型、证据类别、签署职责与生效规则。

本模块只登记"什么服务需要什么证据、谁能签署"，不持有业务状态。
竣工（工程里程碑）与运营准入（服务范围开放）是两套口径：
竣工只证明工程事实，班型开放必须按本矩阵逐项核对证据生效情况。
"""
from __future__ import annotations

# --- 投资批次内的项目时限 -------------------------------------------------
# 中央投资下达时确定，单位为日历日要求；实际值随 PROJECT_REGISTERED 登记。
DEADLINE_KINDS = ("start_deadline", "completion_deadline", "operation_deadline")

# --- 工程里程碑 -----------------------------------------------------------
MILESTONE_STARTED = "started"          # 开工
MILESTONE_COMPLETED = "completed"      # 竣工
MILESTONE_KINDS = (MILESTONE_STARTED, MILESTONE_COMPLETED)

# --- 服务范围（班型） ------------------------------------------------------
SCOPE_FULL_DAY = "full_day"   # 全日托
SCOPE_HALF_DAY = "half_day"   # 半日托
SCOPE_HOURLY = "hourly"       # 计时托
SERVICE_SCOPES = (SCOPE_FULL_DAY, SCOPE_HALF_DAY, SCOPE_HOURLY)

# --- 证据类别 -------------------------------------------------------------
EV_MILESTONE_COMPLETION = "milestone_completion"  # 工程竣工结论（建设部门）
EV_VENUE_ACCEPTANCE = "venue_acceptance"          # 场地验收（含消防条件）
EV_FIRE_FILING = "fire_filing"                    # 消防备案
EV_FOOD_FILING = "food_filing"                    # 食品经营备案
EV_OPERATION_PLAN = "operation_plan"              # 运营方案（班型/编制/制度）
EV_STAFF_QUALIFICATION = "staff_qualification"    # 人员资质（从业/保育/卫生保健）
EV_SAFETY_SYSTEM = "safety_system"                # 安全制度（应急/晨检/卫生）
EV_SERVICE_AGREEMENT = "service_agreement"        # 妇幼机构服务包协议
EV_TRIAL_CLEARANCE = "trial_clearance"            # 试运行结论（问题清零确认）

EVIDENCE_CATEGORIES = (
    EV_MILESTONE_COMPLETION,
    EV_VENUE_ACCEPTANCE,
    EV_FIRE_FILING,
    EV_FOOD_FILING,
    EV_OPERATION_PLAN,
    EV_STAFF_QUALIFICATION,
    EV_SAFETY_SYSTEM,
    EV_SERVICE_AGREEMENT,
    EV_TRIAL_CLEARANCE,
)

# 结论
CONCLUSION_PASS = "pass"
CONCLUSION_FAIL = "fail"
CONCLUSIONS = (CONCLUSION_PASS, CONCLUSION_FAIL)

# 暂停原因（只暂停依赖该来源的班型）
SUSPEND_EXPIRED = "evidence_expired"          # 证照/备案过期
SUSPEND_RECTIFICATION = "major_rectification"  # 重大整改
SUSPEND_AGREEMENT_ENDED = "agreement_ended"   # 合作协议终止
SUSPEND_REASONS = (SUSPEND_EXPIRED, SUSPEND_RECTIFICATION, SUSPEND_AGREEMENT_ENDED)

# 撤销原因 → 暂停口径
REVOCATION_AGREEMENT_ENDED = "agreement_terminated"
REVOCATION_LICENSE_REVOKED = "license_revoked"
REVOCATION_OTHER = "other"
REVOCATION_REASONS = (
    REVOCATION_AGREEMENT_ENDED,
    REVOCATION_LICENSE_REVOKED,
    REVOCATION_OTHER,
)

# --- 签署职责矩阵 ---------------------------------------------------------
# 每个证据类别只能由对应职责的责任人签署验收结论；提交者不得签署自己的材料
# （提交/签署同人校验在领域服务中执行）。
ROLE_CONSTRUCTION = "construction_department"   # 建设部门
ROLE_VENUE_INSPECTOR = "venue_inspector"        # 场地验收职责人
ROLE_FIRE_AUTHORITY = "fire_authority"          # 消防主管部门
ROLE_FOOD_AUTHORITY = "food_authority"          # 食品主管部门
ROLE_MATERNAL_CHILD = "maternal_child_agency"   # 妇幼保健机构
ROLE_REGULATOR = "municipal_regulator"          # 市级主管部门
ROLE_OPERATOR = "operator"                      # 运营团队（仅提交，不签署准入结论）

SIGNING_ROLES: dict[str, tuple[str, ...]] = {
    EV_MILESTONE_COMPLETION: (ROLE_CONSTRUCTION, ROLE_REGULATOR),
    EV_VENUE_ACCEPTANCE: (ROLE_VENUE_INSPECTOR, ROLE_REGULATOR),
    EV_FIRE_FILING: (ROLE_FIRE_AUTHORITY,),
    EV_FOOD_FILING: (ROLE_FOOD_AUTHORITY,),
    EV_OPERATION_PLAN: (ROLE_REGULATOR,),
    EV_STAFF_QUALIFICATION: (ROLE_REGULATOR, ROLE_MATERNAL_CHILD),
    EV_SAFETY_SYSTEM: (ROLE_REGULATOR, ROLE_FIRE_AUTHORITY),
    EV_SERVICE_AGREEMENT: (ROLE_MATERNAL_CHILD,),
    EV_TRIAL_CLEARANCE: (ROLE_REGULATOR,),
}

# --- 班型 × 证据需求矩阵 ---------------------------------------------------
# 全日托口径最严：在园用餐 → 食品备案；时间最长 → 全员资质与安全制度。
# 半日托不用餐 → 不要求食品备案。
# 计时托按弹性时段托管，人员资质按值守班组口径覆盖即可（仍需资质证据），
# 不强制妇幼服务包与试运行清零之外的全日制度项。
SCOPE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    SCOPE_FULL_DAY: (
        EV_MILESTONE_COMPLETION,
        EV_VENUE_ACCEPTANCE,
        EV_FIRE_FILING,
        EV_FOOD_FILING,
        EV_OPERATION_PLAN,
        EV_STAFF_QUALIFICATION,
        EV_SAFETY_SYSTEM,
        EV_SERVICE_AGREEMENT,
        EV_TRIAL_CLEARANCE,
    ),
    SCOPE_HALF_DAY: (
        EV_MILESTONE_COMPLETION,
        EV_VENUE_ACCEPTANCE,
        EV_FIRE_FILING,
        EV_OPERATION_PLAN,
        EV_STAFF_QUALIFICATION,
        EV_SAFETY_SYSTEM,
        EV_SERVICE_AGREEMENT,
        EV_TRIAL_CLEARANCE,
    ),
    SCOPE_HOURLY: (
        EV_MILESTONE_COMPLETION,
        EV_VENUE_ACCEPTANCE,
        EV_FIRE_FILING,
        EV_STAFF_QUALIFICATION,
        EV_SAFETY_SYSTEM,
        EV_TRIAL_CLEARANCE,
    ),
}

# 证据类别 → 失效时适用哪种暂停口径
CATEGORY_SUSPEND_REASON = {
    EV_SERVICE_AGREEMENT: SUSPEND_AGREEMENT_ENDED,
    EV_FIRE_FILING: SUSPEND_EXPIRED,
    EV_FOOD_FILING: SUSPEND_EXPIRED,
    EV_STAFF_QUALIFICATION: SUSPEND_EXPIRED,
    EV_VENUE_ACCEPTANCE: SUSPEND_RECTIFICATION,
    EV_MILESTONE_COMPLETION: SUSPEND_RECTIFICATION,
    EV_OPERATION_PLAN: SUSPEND_RECTIFICATION,
    EV_SAFETY_SYSTEM: SUSPEND_RECTIFICATION,
    EV_TRIAL_CLEARANCE: SUSPEND_RECTIFICATION,
}

# 问题严重度
ISSUE_MINOR = "minor"
ISSUE_MAJOR = "major"
ISSUE_SEVERITIES = (ISSUE_MINOR, ISSUE_MAJOR)


def scopes_depending_on(category: str) -> tuple[str, ...]:
    """返回依赖某证据类别的班型（证照失效时只暂停这些班型）。"""
    return tuple(
        scope
        for scope, required in SCOPE_REQUIREMENTS.items()
        if category in required
    )
