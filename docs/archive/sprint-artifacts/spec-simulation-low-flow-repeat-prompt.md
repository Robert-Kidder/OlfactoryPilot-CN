---
title: '修复模拟模式 LOW_FLOW 误布防与安全状态重复提示'
type: 'bugfix'
created: '2026-08-25'
status: 'done'
review_loop_iteration: 0
baseline_commit: '927e93a28193f136be3bf7c4c7ba39b08c21f39e'
context:
  - '{project-root}/docs/archive/sprint-artifacts/1-2-safe-start-airflow-interlock.md'
  - '{project-root}/docs/architecture.md'
  - '{project-root}/docs/ux-design.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** simulation 连接后执行 startup-zero，MockHAL 每 200 ms 合法返回 0。当前缺少 LOW_FLOW 的 application-level armed 条件，startup-zero receipt 又造成 AppState 与 interlock readiness 漂移，使 idle 被误报；多个 UI 源还为同一状态轮流创建 InfoBar，并高频写 INFO。

**Approach:** 保留 armed 时的 LOW_FLOW 判定，统一 idle/startup-zero 与供气阶段的监视生命周期；分离常驻状态、转换通知和审计日志。仅使用 Mock/simulation。

## Boundaries & Constraints

**Always:** armed LOW_FLOW 继续 fail-closed；stale/fault/断连始终阻断；interlock 与 AppState 一致；异常常驻显示；global stop 收敛期保留 polling、终态后停止；跑 Ruff、定向测试、完整 pytest，本地 commit、不 push。

**Ask First:** 若必须改变 RealHAL、真实关阀时序、owner/lease、安全停止偏序，或需要 HIL，停止并请求授权。

**Never:** 不让 MockHAL 在 idle 假报正流量，不按时间/文案粗暴 suppress，不降低阈值或吞掉真实异常，不重做页面视觉，不连接真实硬件。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 模拟 idle | startup-zero 成功，flow=0 | disarmed、idle/SAFE、可设流量、无 LOW_FLOW | readiness=false，危险阀禁用 |
| armed 低流 | 确认供气后 fresh low flow | 一次 LOW_FLOW 转换，原阻断/收敛 | 不绕过 |
| disarmed 异常 | NaN/stale/FAULT/断连 | 仍异常 | fail-closed |
| 持久异常 | 同 state 重复 snapshot | 状态常驻；InfoBar 一次；关闭不重弹 | 重复仅 DEBUG/节流 |
| 恢复/重入 | A→SAFE→A 或 A→B | 重入/新异常各提示一次 | 转换 INFO |
| global stop | 最终 success | 偏序不变；终态停 polling；迟到消息不覆盖 | 失败 RECOVERY_REQUIRED |

</frozen-after-approval>

## Code Map

- `app/services/mock_hal.py:15-88`, `app/workers/flow_worker.py:504-667` -- 合法 0 流量及 5 Hz polling；不伪造读数。
- `app/services/safety_manager.py:80-165` -- primitive；armed 后 0→LOW_FLOW 保留。
- `app/workers/hardware_worker.py:197-221,434-476` -- 双发布把派生状态当 hardware state。
- `app/workers/actuation_worker.py:162-257,3083-3122,5150-5194` -- interlock/latch/receipt；startup-zero 被误 armed。
- `app/controllers/main_controller.py:724-762,3356-3425,4587-4979` -- startup-zero、snapshot、安全转换与 INFO。
- `app/views/main_window.py:183-231`, `app/views/manual_experiment_view.py:661-892` -- 提示源竞争，render/clear 清除 dismissal。
- `app/services/shutdown_service.py:206-378` -- read-only 安全偏序依据。
- `tests/` -- safety/interlock/simulation/manual UI/app 回归入口。

## Tasks & Acceptance

**Execution:**
- [x] `app/services/safety_manager.py`, `app/workers/actuation_worker.py`, `app/workers/hardware_worker.py`, `app/controllers/main_controller.py` -- 建立 armed 生命周期，修复 readiness 漂移/状态来源；保持 primitive、latch、危险动作与 safe-stop。
- [x] `app/views/main_window.py`, `app/views/manual_experiment_view.py` -- 用 safety-state transition key 驱动 InfoBar，状态/原因常驻显示；关闭后同态不重建，恢复后可重入。
- [x] `app/controllers/main_controller.py` -- INFO 仅记录 from/to/source/reason 转换；同态降 DEBUG/节流，保留 shutdown/RECOVERY 审计。
- [x] `tests/` -- 覆盖矩阵；修正与 Story 1.2 AC2 冲突的 application 断言，保留 primitive/armed 测试。
- [x] Git -- 仅提交本任务，message 为 `fix(ui): 修复模拟模式安全状态重复提示`；不 push。

**Acceptance Criteria:**
- Given simulation 连接并 startup-zero，when 连续轮询 0，then 保持 idle、可开始供气且无 LOW_FLOW/重复 InfoBar。
- Given armed，when fresh flow 跌破阈值，then LOW_FLOW 先锁存并执行原阻断/收敛，危险 OPEN 不放行。
- Given 持久异常，when 关闭提示且 snapshot 继续，then 常驻状态可见且不重弹；恢复重入才新建。
- Given 同态和转换，then INFO 记录转换，重复至多 DEBUG/节流，审计不丢。
- Given global stop success，then worker 终态停止，迟到 telemetry 不覆盖 success，偏序/receipt 测试通过。

## Spec Change Log

## Design Notes

`SafetyManager.evaluate_state()` 保持阈值 primitive；armed 属于 owner readiness。startup/final zero 后 disarm，确认供气后 arm；fresh SAFE 后才开放危险路径。state 常驻，transition 才创建 InfoBar。

## Verification

**Commands:**
- `python -m ruff check .` -- 全部通过。
- `python -m pytest -q tests/test_safety_manager.py tests/test_actuation_worker.py tests/test_ttl_input.py tests/test_simulation_mode.py tests/test_manual_experiment_integration.py tests/test_manual_experiment_view.py tests/test_product_ui.py tests/test_app.py tests/test_shutdown_actuation.py tests/test_cleaning_state_machine.py tests/test_cleaning_view.py tests/test_flow_controls.py tests/test_manual_experiment.py tests/test_integration_gating.py tests/test_protocol_trigger_integration.py --maxfail=20` -- 405 passed，1 条第三方弃用 warning。
- `python -m pytest -q` -- 966 passed，1 条第三方弃用 warning。
- `git diff --check` -- 无 whitespace error。
- `git status --short`; `git log -1 --oneline` -- 工作区干净且本地提交信息正确。

## Suggested Review Order

**安全监视生命周期**

- 闲置零流量明确撤防。
  [`safety_manager.py:167`](../../../app/services/safety_manager.py#L167)

- owner 统一 armed 生命周期。
  [`actuation_worker.py:183`](../../../app/workers/actuation_worker.py#L183)

- 新鲜样本门禁危险动作。
  [`actuation_worker.py:270`](../../../app/workers/actuation_worker.py#L270)

- 应用安全态独立发布。
  [`hardware_worker.py:434`](../../../app/workers/hardware_worker.py#L434)

**状态消费与审计**

- 过期载荷不回滚安全态。
  [`main_controller.py:740`](../../../app/controllers/main_controller.py#L740)

- 稳态 DEBUG，转换 INFO。
  [`main_controller.py:5115`](../../../app/controllers/main_controller.py#L5115)

- stop 明确结束预检等待。
  [`main_controller.py:660`](../../../app/controllers/main_controller.py#L660)

- STOPPED 重启保持触发语义。
  [`protocol_executor.py:151`](../../../app/services/protocol_executor.py#L151)

**常驻状态与转换提示**

- 状态区持续呈现异常。
  [`main_window.py:203`](../../../app/views/main_window.py#L203)

- 转换键控制提示生命周期。
  [`main_window.py:219`](../../../app/views/main_window.py#L219)

- dismissed 同态不重建。
  [`manual_experiment_view.py:841`](../../../app/views/manual_experiment_view.py#L841)

- snapshot 不覆盖安全提示。
  [`manual_experiment_view.py:763`](../../../app/views/manual_experiment_view.py#L763)

**回归验证**

- armed 与样本时序覆盖。
  [`test_actuation_worker.py:1198`](../../../tests/test_actuation_worker.py#L1198)

- LOW_FLOW 消费者均 fail-closed。
  [`test_cleaning_state_machine.py:299`](../../../tests/test_cleaning_state_machine.py#L299)

- 手动危险路径保持关闭。
  [`test_manual_experiment.py:163`](../../../tests/test_manual_experiment.py#L163)

- 预检停止正确收尾。
  [`test_flow_controls.py:290`](../../../tests/test_flow_controls.py#L290)

- 转换提示支持恢复重入。
  [`test_product_ui.py:151`](../../../tests/test_product_ui.py#L151)

- 日志与过期载荷覆盖。
  [`test_app.py:1660`](../../../tests/test_app.py#L1660)

- 旧 writer 不污染新代。
  [`test_protocol_trigger_integration.py:746`](../../../tests/test_protocol_trigger_integration.py#L746)
