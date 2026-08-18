# Story 4.5：全局停止顺序与三通阀模型

Status: **done：软件门禁与 2026-08-18 normal 动作偏序 HIL 已通过；compensation 物理出口映射限制已记录**
Implementation date: 2026-08-17
HIL acceptance date: 2026-08-18
Baseline commit: `49fd99ecd0b4247555d071e6fd7e0fff8b3205b9`

## 本次范围（2026-08-17 软件开发阶段）

- 仅实施软件模型、owner/worker 收敛逻辑和自动化测试；未连接、操作或探测真实硬件，未运行 HIL。
- `Dev2/P1.0` 建模为独立二选一 selector：`odor` / `compensation`，不计入 1–20 气味阀集合，也不存在“全关”状态。
- 全局 shutdown、异常失效与清洗停止共享关键偏序：fence/epoch 失效 → MFC A=0 匹配 receipt → selector 补偿路线 → 气味阀与其余 owner 收敛。
- A 清零或 selector 的失败、超时、stale、迟到、冲突、身份不匹配或未知状态均锁定 `RECOVERY_REQUIRED`，不得报告已安全停止。
- 保留现有 HAL、Worker、lease、epoch、receipt、紧急抢占和 owner handoff；未实施 Story 4.6，未改版 UI，未重写硬件底层。

## 实施摘要

1. `SafeStopPlan` 提供纯状态/证据门禁，按 `operation_id + generation + execution_epoch + command_id` 核对 A=0 与 selector receipt。
2. `SelectorConfig` 固定安全语义为补偿出口，支持显式电平配置，并拒绝 selector 与气味阀物理 line 冲突。
3. Flow owner 增加仅清零 A 的相关命令与 receipt；新 safe-stop epoch 可抢占旧身份，但旧/同代冲突身份不能回滚当前门禁。
4. Actuation owner 在 selector 写入前验证 A=0 证据；selector 使用专用 safety command，普通/紧急“全关”只覆盖气味阀 1–20。
5. shutdown 先取得 fence 和 A=0 receipt，再请求 selector；owner 异常、超时和 handoff 不完整统一留在恢复态。
6. 既有清洗停止适配相同偏序；新增 A=0 receipt timeout 和迟到 receipt 防护，未继续旧 Story 4.1 真实 HIL。
7. 根据独立审查 F-01–F-15 完成第二轮整改：公共 selector 写入封口、完整 command/receipt identity、重复 receipt fail-closed、单调 deadline、跨 variant 关闭、精确 lease token handoff，以及 session receipt 身份持久化。
8. 根据整改后独立复审 R-01–R-10 完成最终收敛：业务 selector 仅允许内部 odor-route plan、完整 receipt identity、重复 stop handoff 门禁、maintenance 三方 fence、protocol/cleaning lease 清理、flow 冲突清零、反向极性记录和 HIL benchmark 统一 teardown。

## 本次文件清单

核心实现：

- `app/models/safe_stop.py`
- `app/models/actuation.py`
- `app/models/app_state.py`
- `app/models/__init__.py`
- `app/services/flow_service.py`
- `app/services/actuation_do_adapter.py`
- `app/services/real_hal.py`
- `app/services/session_file_service.py`
- `app/services/shutdown_service.py`
- `app/services/valve_service.py`
- `app/workers/flow_worker.py`
- `app/workers/actuation_worker.py`
- `app/controllers/main_controller.py`
- `app/workers/session_writer.py`
- `config/default_config.json`

自动化测试：

- `tests/test_safe_stop.py`
- `tests/test_flow_worker.py`
- `tests/test_shutdown_actuation.py`
- `tests/test_actuation_worker.py`
- `tests/test_cleaning_state_machine.py`
- `tests/test_app.py`
- `tests/test_do_lifecycle.py`
- `tests/test_actuation_do_adapter.py`
- `tests/test_protocol_trigger_integration.py`
- `tests/test_session_file_service.py`
- `tests/test_session_writer.py`
- `tests/test_cleaning_view.py`
- `tests/test_hil_actuation_benchmark.py`
- `scripts/hil_single_line_mapping.py`（旧 21-target 入口已退役；未运行脚本）
- `scripts/hil_actuation_benchmark.py`（已适配统一 safe-stop/handoff；未运行脚本）

流程与证据：

- `_bmad-output/implementation-artifacts/epic-4-context.md`
- `_bmad-output/implementation-artifacts/spec-4-5-safe-stop-selector-order.md`
- `_bmad-output/implementation-artifacts/spec-4-5-code-review-remediation.md`
- `_bmad-output/implementation-artifacts/review-4-5-blind-hunter-prompt.md`
- `_bmad-output/implementation-artifacts/review-4-5-edge-case-hunter-prompt.md`
- `docs/sprint-artifacts/sprint-status.yaml`
- `docs/sprint-artifacts/4-5-code-review.md`
- 本文件

## 自动化证据

| Gate | Result |
|---|---|
| Story 4.5 扩大定向测试 | `415 passed in 11.39s` |
| 完整 pytest | `762 passed in 19.59s` |
| Ruff | `All checks passed!` |
| `git diff --check` | 通过；仅有 LF→CRLF 工作副本提示，无 whitespace error |

定向命令：

```powershell
python -m pytest -q tests/test_safe_stop.py tests/test_valve_service.py tests/test_flow_service.py tests/test_flow_worker.py tests/test_shutdown_actuation.py tests/test_actuation_worker.py tests/test_cleaning_state_machine.py tests/test_flow_controls.py tests/test_app.py tests/test_do_lifecycle.py tests/test_actuation_do_adapter.py tests/test_protocol_trigger_integration.py tests/test_session_file_service.py
```

完整门禁：

```powershell
python -m pytest -q
python -m ruff check .
git diff --check
```

## 工作树保护说明

- 修改前已检查当前 diff，并以 baseline commit 记录既有脏工作树。
- 未执行 `git reset`、`git checkout`、`git clean`、全仓格式化、commit、push 或覆盖式还原。
- 为接入统一停止顺序，确实在已有未提交 Story 4.1 改动上增量编辑了 `main_controller.py`、`valve_service.py`、`shutdown_service.py`、`actuation_worker.py`、`flow_worker.py`、`test_flow_worker.py` 与 `test_cleaning_state_machine.py` 等重叠文件；未删除或回退既有功能和证据。
- `scripts/hil_single_line_mapping.py` 仅包含一处 lint 空白调整；在该软件开发轮次没有执行任何 HIL 入口。

## 独立复审结论与后续 HIL（2026-08-17 软件阶段记录）

> 本节保留软件开发收口时的历史结论；其中“真实 HIL 仍须验证”的状态已由下方 2026-08-18 验收补充取代。

Story 4.5 软件范围已满足 `done`：

1. 独立复审 R-01–R-10 已全部整改并逐项增加阻断回归；软件门禁全部通过。
2. 真实 Windows/NI HIL 不属于本轮授权，仍需另行验证 `Dev2/P1.0` 两个电平与实际 `odor` / `compensation` 路线一致。
3. HIL 应继续验证 A 非零门禁、A=0/selector 超时失败、断连、进程退出和 owner 卡住的现场恢复指引。
4. 旧 Story 4.1 的 21-target HIL 证据不得直接复用。

## 真实硬件 HIL 验收补充（2026-08-18）

Story 4.5 的 `normal` 场景已在无气味材料、无受试者、仅使用洁净 Air 的受控条件下完成。候选 commit 为 `154e379da400b326162f84097b79270a5f455e0c`，tree 为 `5096759d93f64542a6fa72c7a6d7ced3743d3440`，授权 payload digest 为 `2b2013664f802d4fbda9351e6a902e5ee788c046fe59e0beafd94dcdc793af92`。

- 现场参数：A=`2500 sccm`、B=C=`0`，稳定观察窗口 `20 s`；NI 为 Dev1/Dev2，selector 为 `Dev2/P1.0`。
- 操作者在停止前的 `odor` 路线阶段独立观察到阀 2 有“持续气流”；该观察与 DAQ 电子 ack 分开记录，不证明停止后的 `compensation` 路线机械位置。
- 自动停止严格验证了有效 A=0 receipt 先于 selector 切换至 `compensation`，随后关闭气味阀 1–20、完成 A/B/C 清零与 owner handoff。
- 最终只读回读为 A/B/C setpoint=`0`、mass flow=`0`、gas=`Air`、无状态码；selector 软件证据为 `compensation`，气味阀 1–20 最后成功请求均为 LOW。
- `safe_stop_status=completed`、`verification_passed=true`、授权违规 `0`、审计错误 `0`、证据哈希不匹配 `0`；maintenance、DO、lease、AI 与 serial handoff 完整。
- live runner 候选的离线全仓门禁为 `820 passed in 28.49s`，详见 [`spec-4-5-hil-live-execution.md`](../../_bmad-output/implementation-artifacts/spec-4-5-hil-live-execution.md#actual-results-2026-08-18全部离线-fakemock)。
- 运行结束后操作者已关闭上游 Air；当前不需要继续操作硬件。

归档索引见 [Story 4.5 normal HIL 证据](evidence/story-4-5-hil-normal-20260818/README.md)。本次验收完成了 normal 动作偏序、`odor` 出口气流观察和最终软件/电子收敛验证；`compensation` 物理出口映射仍是明确限制，不以电子 ack 冒充机械确认。失败、超时、stale/late receipt 与 selector 不确定等故障场景继续由确定性离线注入覆盖；除非安全契约、硬件映射或 live runner 发生实质变化，不主动在真实硬件上制造这些故障。
