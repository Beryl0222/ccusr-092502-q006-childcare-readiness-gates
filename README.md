# 托育建设投运证据门

面向获得中央投资的托育综合服务中心，本项目是一套**服务端证据门（readiness gate）**：
把投资批次、开工/竣工/投运里程碑、场地验收、运营方案（人员与班型）、消防食品等安全备案、
服务包协议与试运行问题，按各自生效期关联为可追溯的就绪判定，杜绝"竣工即投运"的错误放行。

## 核心口径

- **工程竣工不代表运营准入。** 全日托、半日托、计时托分别按依赖矩阵判定，所需证据
  全部有效时才允许开放：
  1. 工程竣工验收、运营方案审查、安全备案三条结论链均通过且当前有效；
  2. 运营方案明确确认该班型的人员与班型配置；
  3. 该班型存在当前有效的服务包协议（如与妇幼机构）；
  4. 该班型没有未关闭的阻断性试运行问题。
- **生效期求值。** 证据与协议带 `valid_from/valid_until`，在观察时点判断是否有效；
  证照到期自动派生为阻断，巡检补记暂停审计事件。
- **范围性暂停，不株连。** 证照过期、重大整改、合作协议终止只暂停实际依赖它的班型；
  条件恢复后须显式走恢复流程并重新核验全部阻断项，不自动放行。
- **签署职责分离。** 三条结论链由建设、运营、安全不同职责人签署；
  提交者不得批准自己提交的材料，签署时强制校验。
- **资金幂等与可溯。** `receipt_id` 是回执幂等键，同一回执重送不重复放款；
  每笔放款的 `basis` 固化闸门类型、所引用结论版本与证据有效期快照。
- **纠正反映资金影响。** 业务事实只追加、不原地改写；新版本结论推翻旧的通过结论时，
  对依赖该结论链的已放款自动产生待追回记录，追回到账后核销。
- **崩溃恢复不丢事项。** 仅追加事件日志（fsync）重放重建全部状态；
  待复核、待追回均为派生视图；末行断电撕裂自动回截。

## 模块

| 文件 | 职责 |
| --- | --- |
| `contracts/domain.json` | 聚合、班型、结论链、里程碑、16 类事件与信封不变量 |
| `src/domain.py` | 纯函数核心：命令决策（decide）、就绪判定、事件折叠（replay） |
| `src/store.py` | 仅追加 JSONL 事件存储：原子批量写、fsync、版本连续校验、撕裂恢复 |
| `src/service.py` | 服务外观：命令受理、信封包装、监管视图（期限余量/阻断项/资金依据） |
| `src/envelope.py` | 跨模块共用的事件信封基础校验（不含业务流程） |
| `src/demo.py` | 端到端联调演示：立项→投运→证照到期→纠正→追回→恢复 |

## 命令与事件

命令（`handle({"command": ..., "payload": {...}})`）：
`register_project`、`report_milestone`、`record_evidence`、`sign_conclusion`、
`require_rectification`、`clear_rectification`、`sign_protocol`、`terminate_protocol`、
`raise_issue`、`close_issue`、`open_scope`、`resume_scope`、
`disburse_funds`、`recover_funds`，以及服务侧巡检 `sweep(project_id)`。

事件以 `investment_project-<项目编号>` 为聚合，版本在聚合内严格递增；
一条命令可原子产生多事件（如纠正结论同时登记资金影响）。完整语义见
`contracts/domain.json` 的 `rules`。

## 监管视图

`project_view(project_id)` 按项目返回：

- `deadlines`：开工/竣工/投运时限、距观察时点的余量（`margin_human`）、是否逾期；
- `scopes`：三个班型的 `open/suspended/never_opened` 状态与逐项阻断原因；
- `conclusions` / `evidence` / `protocols`：结论版本、证据与协议的当前有效性；
- `pending_review`：待复核事项（整改、试运行问题、未签/不通过结论、失效证据、暂停班型）；
- `pending_recovery`：待追回资金及对应的纠正/放款事件；
- `funds`：每笔放款的证据依据快照、纠正记录、已追回与待追回合计。

## 测试与构建

```bash
python3 -m unittest discover -s tests     # 信封合同 + 19 个证据门业务用例
python3 -m compileall -q src tests        # 编译检查
python3 -m src.demo                       # 端到端联调演示
```

全部仅使用 Python 标准库（≥3.11），可在单个 Linux 应用容器中直接执行。

## 领域边界

事件标识、聚合标识、发生时间与版本用于不同模块之间的确定交接；业务事实一旦被接收
就不原地改写，更正由后续版本事件表达（`supersedes` 指向被替代事件）。
对外接口（HTTP/消息）、身份认证与多实例部署编排由正式服务在本核心之外适配。
