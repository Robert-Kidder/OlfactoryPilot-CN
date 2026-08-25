---
title: '修复手动实验执行域串扰并收敛产品规范'
type: 'bugfix'
created: '2026-08-25'
status: 'done'
review_loop_iteration: 0
baseline_commit: '9ed18c5bcbcc5fb4aad664a4e6d53af612bfcb94'
context:
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/architecture.md'
  - '{project-root}/docs/ux-design.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `ActuationWorker` 把任意非 idle lease 当成 protocol context。MANUAL/MAINTENANCE 在 start/终态排队窗口会因“未加载协议”被错误 invalidate，epoch 被改写并启动 abnormal safe stop，合法 manual receipt 随后成为迟到证据与 `RECOVERY_REQUIRED`；UI 和文档还泄露或固化内部状态与旧进度。

**Approach:** 用明确且只接受 protocol-scoped 证据的 ownership predicate 隔离三个执行域，保持 receipt/SafeStopPlan 严格；以 5 秒全链回归证明修复，并同步日志、用户文案和权威文档。有审计价值的旧规划优先归档，只有通过完整删除门禁的重复资料才可删除。

## Boundaries & Constraints

**Always:** 保留 Worker/HAL 单写者、exact lease、generation/epoch/arm epoch、pending identity、monotonic deadline及 stale/late/conflicting receipt 拒绝。正常 Manual completion 保持“目标气口 close receipts 完整 → A=0 receipt → selector compensation receipt → 恢复既定供气 receipt → COMPLETED → 精确释放 MANUAL lease”；异常 SafeStopPlan 保持现有独立 fail-closed 偏序，不在本轮重新设计或与正常链混写。真正 active protocol readiness 丢失仍 fail closed；仅用 Mock/simulation。

**Ask First:** 若需改变 RealHAL、真实极性/时序、SafeStopPlan、HIL 结论或访问真实硬件，停止请求授权。

**Never:** simulation/字符串特判、UI cooldown、放宽证据、忽略恢复态、删安全日志、缩减关阀、视觉重构、创建 Epic/Story 或 push。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Manual | SAFE、既有供气、04/06、5 秒 | 共同 ready 起算；close receipts → A=0 → compensation → 恢复既定供气 → COMPLETED → lease 释放 | 真异常转入现有独立 SafeStopPlan |
| Isolation | MANUAL/MAINTENANCE lease、无 protocol、readiness 交错 | protocol epoch/event/safe-stop 不变 | 各域自身异常自行收敛 |
| Protocol | protocol lease 或其他明确 protocol-scoped active/armed 证据后 readiness 丢失 | invalidate、blocked、background safe stop | 严格证据不变 |

</frozen-after-approval>

## Code Map

- `app/workers/actuation_worker.py:2760-3275,4506-5295` -- 排队、域分发、错误 predicate、invalidation 与 manual 生命周期。
- `app/services/protocol_executor.py:410-427` -- 已限定运行态的 readiness 语义；`flow_worker.py` 与 manual/lease/safe-stop models 是不放宽的证据边界。
- `app/controllers/main_controller.py:3248-3507,4019-4033` -- owner handoff、用户状态和 crossing 日志。
- `app/views/main_window.py:75-306`, `app/views/manual_experiment_view.py:684-996` -- 连接重复、内部码与 normal phase InfoBar。
- `tests/test_manual_experiment_integration.py`, `tests/test_actuation_worker.py`, `tests/test_cleaning_state_machine.py` -- 全链及三域反例；UI/log tests 覆盖文案和级别。
- `README.md`, `docs/{index,project-context,prd,architecture,ux-design,project-structure}.md`, `docs/{sprint-artifacts,archive}/` -- 当前事实、状态/evidence 与历史边界。

## Tasks & Acceptance

**Execution:**
- [x] `app/workers/actuation_worker.py` -- Protocol lease 是最强权威证据；其他运行、布防、pending、active、possibly-open 或 safe-transition 证据只有在身份/owner/category/context 明确证明属于 Protocol 时才可补充证明 protocol context。MANUAL/MAINTENANCE 的 lease、valve、possibly-open、flow ready、pending command 或 active state 均不得触发 protocol invalidation。
- [x] 三类 worker/integration tests -- 覆盖先供气、04/06、5 秒 fake clock 全链，以及 Manual/Maintenance 隔离和 active Protocol fail-closed。
- [x] `main_controller.py` 与 logging tests -- 普通 `threshold_cross` 降 DEBUG，保留 trigger、拒绝、异常、safe-stop 的 INFO/WARNING。
- [x] 两个产品 View 与 UI tests -- 删除常驻内部码和重复连接动作；正常状态默认不新增没有操作价值的说明，不用“系统正常/当前就绪/安全正常/手动模式”等近义文案替代内部码继续常驻。只显示设备是否可用、当前正在执行什么、当前能做什么及是否需要行动；InfoBar 只提示新异常/必要结果，恢复文案不泄露诊断词。
- [x] 当前权威 docs/README -- 写入执行域、用户语言和文档层级，删除动态 Story 进度及未定页面布局。
- [x] `docs/index.md`, `sprint-status.yaml`, `docs/archive/` -- 旧 Story/评审/复盘等有审计价值资料优先归档。任何删除候选必须先执行 repository-wide 引用检查，并确认不含独有产品决策、硬件事实、安全/HIL/极性/发布证据，且不被代码、测试、CI、README、BMAD 配置或当前权威文档引用；任一项不确定即保留或归档。本次 execution spec 必须保留。`bmm-workflow-status.yaml` 仅在确认它属于旧 BMAD 遗留且当前新版 BMAD 不再依赖，并修复全部引用后才可删除，否则保留。

**Acceptance Criteria:**
- Given 无 protocol 的 MANUAL/MAINTENANCE ownership，when readiness 在 start 前、运行中或终态释放前到达，then protocol epoch、blocked event 和 background safe stop 不变。
- Given protocol lease 或其他明确 protocol ownership-scoped 的 active context，when readiness 丢失，then 原 protocol invalidation、全关和恢复门禁仍生效；任何 Manual/Maintenance 状态不得满足该前提。
- Given simulation 已供气，when 04/06 释放 5 秒，then 最终 COMPLETED、目标关闭、possibly-open 为空、供气恢复、lease 释放，且无 protocol 文案/blocked/abnormal safe stop。
- Given 普通页面与文档，when 检查正常/异常状态和仓库引用，then 正常顶部可仅显示“设备已连接”，不新增内部码或无操作价值的正常态近义词；同态提示不重弹，权威层简洁、历史可追溯，且只有完整删除门禁通过的文件才被删除。

## Spec Change Log

- 2026-08-25：完成三执行域隔离、5 秒 Manual 全链回归、日志和用户文案收敛；当前权威文档完成对齐，历史资料按删除门禁迁入 archive/evidence，没有直接丢弃不确定资料。
- 2026-08-25：构建脚本补充 PyInstaller 非零退出门禁及可重复覆盖生成目录，避免旧产物掩盖 COLLECT 失败。
- 2026-08-25：完成 step-04 patch 整改：补齐 pending Protocol load 证据、异常顶部持续中文状态、Manual completion 实际 receipt 偏序、Manual/Maintenance 三个消息窗口及 4-6 动态状态一致性回归。

## Design Notes

Protocol lease 是最强权威证明。其他补充 predicate 必须能通过 protocol owner/lease token、protocol command category/identity、protocol executor context 或 protocol safe-transition identity 证明其专属于 Protocol；泛化的 running/armed/pending/active/valve/possibly-open 条件不能证明 Protocol。仅 document、非零 epoch、任意 non-idle lease、普通 flow ready，以及 Manual/Maintenance 拥有的任何 lease、command、valve 或状态，都不是 protocol execution ownership。

正常 Manual completion 与异常 SafeStopPlan 是两条独立路径。前者只沿已验证的 Manual receipts 完成关闭、A=0、补偿、供气恢复、COMPLETED 和 lease release；后者只在真实异常时沿既有 fail-closed 契约执行。本轮不移动、合并或重新排序二者的动作步骤。

## Verification

**Commands:**
- `python -m ruff check .`; 定向 pytest；`python -m pytest`; `git diff --check` -- 全通过。
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 build` -- 构建成功。
- `python -m app.main --simulation` -- Mock 启动烟测；全操作链由确定性 integration test 验证。
- Markdown/路径引用检查；中文本地 commit 后 `git status --short` -- 无断链、无 `_bmad-output` 依赖、工作区干净。

**Results (2026-08-25):**
- 定向回归：179 passed；覆盖 Manual 先供气后 04/06 共同 ready 起算 5 秒，并从实际 DO/flow receipts、COMPLETED snapshot 与 exact token release 记录断言严格偏序；Manual/Maintenance 均覆盖 start 前排队、active、terminal 未释放三个窗口；Protocol lease/active/pending command/pending load context 均保持 fail-closed。
- 全量回归：978 passed，1 个第三方 qfluentwidgets/SciPy 弃用警告；另有解释器退出时 ResourceWarning，无项目测试失败。
- `python -m ruff check .` 与 `git diff --check` 通过。
- CI build 通过，单文件 EXE 与 COLLECT 目录均重建成功；simulation 应用入口在 offscreen 模式启动后稳定运行 8 秒，再仅终止本次 smoke 的精确进程。
- 全仓 Markdown 相对链接检查 `BROKEN_COUNT=0`；33 个原位置迁移项均在 archive/evidence 有保留副本，当前权威层不依赖 `_bmad-output`；4-6 `development_status/current_gates` 一致为 `review` 并有自动化回归。

## Suggested Review Order

**执行域边界**

- 先看 Protocol-scoped ownership predicate 如何消除跨域失效。
  [`actuation_worker.py:3382`](../../app/workers/actuation_worker.py#L3382)

- 架构层固定三域证据与两条安全收敛路径。
  [`architecture.md:64`](../architecture.md#L64)

**完整生命周期证据**

- 04/06 五秒链断言真实 receipt 偏序和精确 lease 释放。
  [`test_manual_experiment_integration.py:103`](../../tests/test_manual_experiment_integration.py#L103)

- Manual 三个排队窗口证明 Protocol 状态不被改写。
  [`test_manual_experiment_integration.py:189`](../../tests/test_manual_experiment_integration.py#L189)

- Maintenance 复用同一反串扰矩阵。
  [`test_cleaning_state_machine.py:271`](../../tests/test_cleaning_state_machine.py#L271)

**用户状态与日志**

- 顶部将内部状态映射为持续、可行动的自然中文。
  [`main_window.py:167`](../../app/views/main_window.py#L167)

- 恢复提示隐藏 receipt 与内部终态术语。
  [`manual_experiment_view.py:63`](../../app/views/manual_experiment_view.py#L63)

- 普通阈值 crossing 降噪但保留正式安全审计路径。
  [`main_controller.py:4021`](../../app/controllers/main_controller.py#L4021)

**文档层级与外围门禁**

- UX 权威改为可用性、任务和行动导向。
  [`ux-design.md:5`](../ux-design.md#L5)

- 索引只保留权威、状态、证据与历史入口。
  [`index.md:3`](../index.md#L3)

- 归档声明历史资料不覆盖当前事实。
  [`README.md:1`](../archive/README.md#L1)

- 动态状态双路径由自动化测试保持一致。
  [`test_documentation_status.py:7`](../../tests/test_documentation_status.py#L7)

- 构建门禁确保 PyInstaller 非零退出不被旧产物掩盖。
  [`run-ci.ps1:24`](../../scripts/run-ci.ps1#L24)
