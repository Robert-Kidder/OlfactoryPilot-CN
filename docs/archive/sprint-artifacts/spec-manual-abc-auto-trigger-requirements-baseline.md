---
title: '校正 Manual A/B/C 气流模型并固化自动实验触发需求'
type: 'feature'
created: '2026-08-26'
status: 'completed'
review_loop_iteration: 1
baseline_commit: '6a8e90b56ddfbbb1e0327d34bbf18d81cd4031d1'
context:
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/architecture.md'
  - '{project-root}/docs/ux-design.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Manual 仍以可编辑 T 和 `B=T-A` 为 authority，供气/恢复也未表达 `A+C/B/C`；权威文档把已确认的 Auto USB-6501/8-bit 协议写成未知，普通通知则永久驻留。

**Approach:** 将 Manual setpoint 改为独立 A/B/C，以 `A+B` 仅作派生总量，在既有 Worker/HAL/receipt 安全链内实现 baseline、stimulus、restore 目标；统一通知 duration policy，并只在权威文档固化 Auto TXT、顺序执行、外部触发、冲突和呼吸门控需求。

## Boundaries & Constraints

**Always:** B 全程等于用户输入；selector 独立于气味阀 1–20；只开启已选 available 气口。保留 Worker/HAL 单写者、lease、epoch/generation、exact receipt/deadline、迟到/冲突拒绝、正常 completion 偏序及独立 SafeStopPlan。USB-6501、8-bit、Trig.In、`10000001 → 1-based 第128行` 已确认，仅 DAQmx ingress 细节待 HIL。当前 Manual runtime 仍以已实现/已验收的 Dev1/Dev2 工作链为准；USB-6501 不加入本轮 Manual required devices、startup self-check、connection readiness 或连接成功门禁。

**Ask First:** 若需要真实硬件/HIL、改变 selector 极性或 SafeStopPlan/正常收尾偏序，或仓库证据不足以区分 total delivery、B MFC、sample A setpoint 与 compensation 阶段 A-controller 上限，停止并请求授权；不得为 schema migration 猜测硬件能力。

**Never:** 创建 Epic/Story；实现 Settings/Auto UI、USB-6501 reader、SuperLab/breath 接入；扩改 protocol parser；视觉重构；View 控硬件；真实 HIL、push 或批量改写历史。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 普通供气/恢复 | A=500、B=1000、C=500 | MFC=`1000/1000/500`，selector=compensation，气味阀全关，derived total=1500 | 任一匹配回执/收敛失败走既有 Recovery，不冒充完成 |
| 气味刺激 | 同参数，选择 available 气口 | MFC=`500/1000/0`，selector=odor，仅所选可用气口开启，derived total=1500 | 不可用气口、越界或无效输入在硬件 intent 前拒绝 |
| 正常完成 | duration 到期 | close receipts→A=0→compensation→`1000/1000/500`→COMPLETED→精确释放 lease，无 Recovery | 保持 SafeStopPlan 为异常独立路径 |
| 非行动型通知 | success / info / warning | 分别约 2500 / 3000 / 5000ms 自动关闭 | 保持 identity/dedupe/dismiss/priority |
| 行动型通知 | 任意视觉 severity；含 safety/recovery/全局停止/人工处理 | 不因 timeout 消失；保持到用户关闭或 condition resolved | `actionable` 高于视觉 severity，warning 也不得误设 5000ms |

</frozen-after-approval>

## Code Map

- `app/models/{hardware_profile,manual_experiment}.py`、配置/settings draft -- T/A 权威、total delivery/B MFC/sample A/A-controller 上限及旧配置语义兼容落点。
- `app/controllers/main_controller.py:3270-3410`、`app/workers/actuation_worker.py:4785-5320` -- plan 与 receipt-owned flow/completion 链；只换 targets，不动偏序。
- `app/workers/flow_worker.py:578-657`、`app/services/flow_service.py` -- A/B/C serial owner；注意 `rest` 会计算 A+C，避免重复补偿。
- `app/views/manual_experiment_view.py:339-402,580-610,753-864,1017-1175`、`notification_coordinator.py` -- T/B 控件及唯一 managed InfoBar。
- `app/services/{protocol_parser}.py`、`app/models/protocol.py` -- 旧单 valve/trigger parser 与新 TXT 冲突，只记录。
- `docs/{prd,architecture,project-context,ux-design,index}.md`、相关 Manual/notification tests -- 权威需求与回归落点。

## Tasks & Acceptance

**Execution:**
- [x] 领域/配置 -- A/B/C 为 authority，`derived_total=A+B`，删除 `A≤T`；保留旧配置可读取，并按证据区分 `max_total_sccm`（若它是已认可的 total-delivery ceiling，则约束 A+B）、独立 B MFC 上限、sample A 用户上限与 compensation 阶段 A-controller `A+C` 上限。不得把旧 `total` key 静默改义为 B，证据不足的独立上限不猜测并触发 Ask First。
- [x] Controller/Worker -- stimulus=`A/B/0`，baseline/restore=`A+C/B/C`，B 恒定；不移动 owner/receipt/lease/completion 偏序。
- [x] Manual View -- 只改为可编辑 A/B/C（可弱显 A+B），不改 Card/图表/PortTile/Header/Navigation。
- [x] NotificationCoordinator/InfoBar -- `actionable` 是 duration 的最终 authority；仅 non-actionable success/info/warning 使用 2500/3000/5000ms，任何 actionable severity 均持久。自动关闭正确退休 event，condition 不被 timeout 掩盖；仅稳定复现时修 animation。
- [x] 权威 docs -- PRD 完整记录 Auto TXT 的 20 个独立 0/1 columns（`channel_01`…`channel_20`，不得默认打包成 bitmask/integer）+ A/B/C + trailing unused NULL columns、顺序执行、SuperLab→c-pod→USB-6501、Trig.In/row、越界拒绝、queue/latest 冲突、future breath refractory interval、Settings/physical verification；同时分开记录 Manual Dev1/Dev2 当前 runtime 基线与未来 Auto USB-6501 ingress（reader/readiness/HIL 未实施）。
- [x] tests -- 锁定 500/1000/500 三阶段、B invariant、available valves、COMPLETED/lease/无 Recovery及通知策略。

**Acceptance Criteria:**
- Given 任意合法独立 A/B/C，when Manual 从供气到刺激再正常完成，then 三阶段只使用规定公式，B 从未被 T 或阶段切换改写，最终阀全关且 lease 释放。
- Given 旧配置与现有硬件证据，when 迁移 flow limits，then editable T 被删除但经证实的 `max_total_sccm` 仍可约束 A+B，且不会被静默重解释为 B MFC 上限；sample A 与 A-controller `A+C` 上限保持不同语义，缺证据时不猜测。
- Given 输入负数或违反已有明确上限，when 构造/提交 intent，then 在任何硬件命令前明确拒绝；不猜测其他联合限制。
- Given 任意 actionable 通知使用 info、warning 或 error 视觉样式，when timeout 到期，then 通知仍保持；只有 non-actionable success/info/warning 自动消失。
- Given 检查权威文档，when 搜索 T/USB-6501/trigger/row，then 旧 Manual 规则消失，确认事实与 parser/DAQmx ingress HIL 待办严格分开。
- Given 当前 Manual 启动与连接，when USB-6501 未连接，then Dev1/Dev2 既有工作链不受影响；未来 Auto Build 才依据 NI MAX alias、port、DAQmx task 与 HIL 建立 USB-6501 readiness。

## Spec Change Log

- 2026-08-26：完成独立 A/B/C Manual runtime、三阶段 flow receipt 链、通知 duration policy、Auto 权威需求基线与对应回归测试。

## Design Notes

`FlowCommand.a/b/c` 是实际 controller target；baseline/restore 写 `A+C/B/C` 时须用不再二次补偿的 mode。正常结束仍是 close→A=0（B 保持）→compensation→restore。

删除 editable total 与迁移 safety limits 是两个问题：旧 `max_total_sccm` 只有在现有配置/硬件证据表明其为总送风 ceiling 时才继续约束 `A+B`，绝不机械改名为 `max_main_b_sccm`。B MFC、sample A 和 compensation A-controller 的独立上限都必须有各自证据；backward compatibility 保留原语义，不能用 schema migration 创造硬件事实。

Notification lifecycle 先判断是否 actionable，再选 duration；视觉 severity 只决定样式/优先级。Auto TXT 的 20 个 channel states 是 20 个独立 0/1 columns，不是 packed 20-bit integer。USB-6501 是已确认的未来 Auto ingress 硬件，但在 reader/HIL 完成前不是当前 Manual readiness 依赖。

## Verification

**Commands:**
- `python -m ruff check .` -- 全通过。
- Manual/flow/selector/compensation/notification/controller/simulation/UI 定向 pytest -- `388 passed`；review 修复后的通知/产品 UI 回归继续通过，且无稳定 QPropertyAnimation 生命周期警告。
- `python -m pytest` -- 共 1001 项：`996 passed`；其中 4 个 HIL 失败由仓库内 `--basetemp` 触发证据目录保护，改到仓库外后 `4 passed`；唯一剩余 `test_slow_finalize_timeout_claims_terminal_result_and_never_publishes` 属于未改动 SessionWriter 的 30ms 基线时序测试，独立重跑 3 次仍失败，本轮未越界修改。
- `git diff --check` -- 通过。
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 build` -- 通过，成功生成 `dist/OlfactoryPilot.exe`。
- `python -m app.main --simulation` -- offscreen 启动、自检和三个 Worker 启动成功，未连接真实硬件；冒烟后终止进程。
