---
title: 'Story 4.5：代码审查阻断项整改'
type: 'bugfix'
created: '2026-08-17'
status: 'done'
baseline_commit: '49fd99ecd0b4247555d071e6fd7e0fff8b3205b9'
review_loop_iteration: 0
context:
  - '{project-root}/docs/archive/sprint-artifacts/4-5-code-review.md'
  - '{project-root}/docs/archive/sprint-artifacts/4-5-safe-stop-selector-order.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 独立审查确认 10 个 High、5 个 Medium 缺口：HAL/emergency 旁路可提前写 selector；stop、shutdown、cleaning 的 receipt、deadline、lease 和 handoff 证据不一致，并会误报安全终态。

**Approach:** 修复 F-01–F-15，把 selector 唯一写入、A/B/C、完整 receipt identity、deadline 和 owner/lease handoff 纳入统一 fail-closed 契约，并为每项发现增加回归测试。

## Boundaries & Constraints

**Always:** selector 仅经 SafeStopPlan 写入；所有停止先 fence，再取得匹配 A=0 receipt；冲突、重复、迟到、超时或 handoff 不完整均锁定 `RECOVERY_REQUIRED`；odor best-effort 覆盖全部 variant；状态不得早于证据。

**Ask First:** 改变现场 selector 安全路线/极性、改变产品范围、运行真实 Windows/NI HIL，或需要真实硬件授权。

**Never:** 不运行 HIL、不实施 Story 4.6、不重构 UI、不新增跨 owner HAL 旁路、不覆盖 Story 4.1 工作、不执行破坏性 Git 或全仓格式化。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| selector 旁路 | emergency/HAL/脚本 | 通用 API 拒绝；合法路径验证 A=0 identity | 无写入并恢复必需 |
| stop/shutdown | protocol、异常或 owner 失败 | fence → A=0 → selector → odor → B/C → handoff | 缺证据不得完成 |
| cleaning/receipt | 冲突、失败、超时、迟到 | 不切 selector，仍关闭 odor | 保持恢复态与 fence |
| 配置/极性 | 0/21、line alias、safe-high | 拒绝越界/冲突；合法极性端到端一致 | 无效配置禁用 selector |

</frozen-after-approval>

## Code Map

- `real_hal.py`、HIL 脚本、DO adapter -- selector 旁路与极性。
- `actuation_worker.py`、`flow_worker.py`、`shutdown_service.py` -- 停止、receipt、deadline、handoff。
- `app_state.py`、`valve_service.py`、session validator -- 配置与记录边界。
- `main_controller.py` -- stop 编排与 cleaning handoff 显示。
- `tests/` -- F-01–F-15 fault-injection 回归。

## Tasks & Acceptance

**Execution:**
- [x] HAL/HIL、DO adapter、ValveService -- 封死 selector 旁路并统一极性。
- [x] ActuationWorker、Controller -- 统一 stop，跨 variant 收敛且不提前完成。
- [x] ShutdownService、FlowWorker -- 全 owner/lease/serial handoff 后再完成。
- [x] cleaning -- 完整 identity、失败 odor close、真实 handoff 后确认。
- [x] 配置/session validator -- 限制 1–20、规范化 alias、支持专用 safe-high receipt。
- [x] `tests/` 与 Story 记录 -- 每个 F 项有证据并运行完整门禁。

**Acceptance Criteria:**
- Given 任一停止入口，when A=0 匹配 receipt 尚未成立，then selector 不发生写入且不能报告安全完成。
- Given owner、lease、receipt 或 deadline 有任何冲突/不确定，when 收敛继续，then odor best-effort 执行但终态保持 `RECOVERY_REQUIRED`。
- Given A/B/C、selector、odor 与 owner handoff 完整，when 计划结束，then 才允许报告 completed/success。
- Given合法 safe-high selector 配置，when 执行与持久化 receipt，then DO 电平和 session contract 均接受同一专用安全动作。

## Spec Change Log

- 2026-08-17：完成 F-01–F-15 与第二轮 adversarial findings 的软件整改；Story 保持 `review`，等待独立复审和 HIL。

## Design Notes

`completed` 代表气路与 owner 证据完整，不代表“已尽力”。失败后可继续关闭 odor，但不能补造关键证据。deadline 以提交时的单调时钟为准，receipt 到达后仍比较完成时间。

## Verification

**Commands:**
- Story 4.5 扩大定向 pytest -- `408 passed in 11.28s`。
- `python -m pytest -q` -- `755 passed in 19.37s`，未执行 HIL。
- `python -m ruff check .` -- `All checks passed!`。
- `git diff --check` -- 无 whitespace error；仅 LF→CRLF 工作副本提示。

## Suggested Review Order

**统一停止证据门禁**

- 从全局 fence 进入唯一的 epoch 失效与 cleaning 抢占入口。
  [`actuation_worker.py:1826`](../../../app/workers/actuation_worker.py#L1826)

- 以完整 flow identity 和 deadline 决定是否允许 selector。
  [`actuation_worker.py:1434`](../../../app/workers/actuation_worker.py#L1434)

- protocol 只有精确 lease handoff 后才从 BLOCKED 进入 STOPPED。
  [`actuation_worker.py:2949`](../../../app/workers/actuation_worker.py#L2949)

**Owner 与硬件边界**

- shutdown 按 A、selector、odor、final zero、handoff 顺序收敛。
  [`shutdown_service.py:65`](../../../app/services/shutdown_service.py#L65)

- Flow owner 保留原 lease token 并在 deadline 后拒绝成功。
  [`flow_worker.py:337`](../../../app/workers/flow_worker.py#L337)

- DO adapter 仅接受配置目标与专用 selector 身份。
  [`actuation_do_adapter.py:34`](../../../app/services/actuation_do_adapter.py#L34)

- HAL 将 selector 从所有 variant 的 odor 关闭集合中隔离。
  [`real_hal.py:753`](../../../app/services/real_hal.py#L753)

**模型与持久化**

- 纯模型集中拒绝失败、迟到、冲突和重复 receipt。
  [`safe_stop.py:103`](../../../app/models/safe_stop.py#L103)

- session contract 识别专用 selector receipt 和反向极性。
  [`session_file_service.py:195`](../../../app/services/session_file_service.py#L195)

- writer 持久化 operation、generation、step 与 action identity。
  [`session_writer.py:1474`](../../../app/workers/session_writer.py#L1474)

**关键回归**

- 公共提交 API 无法伪造 selector 安全动作。
  [`test_actuation_worker.py:1488`](../../../tests/test_actuation_worker.py#L1488)

- queued cleaning 在任何开阀前被全局停止 fence。
  [`test_cleaning_state_machine.py:163`](../../../tests/test_cleaning_state_machine.py#L163)

- 同步 A 清零晚于 deadline 时拒绝 receipt。
  [`test_flow_worker.py:155`](../../../tests/test_flow_worker.py#L155)
