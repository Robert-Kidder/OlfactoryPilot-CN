# Story 4.5：全局停止顺序与三通阀模型

Status: **done：独立复审 R-01–R-10 已全部整改并通过软件门禁；真实 HIL 仍须另行授权**
Date: 2026-08-17
Baseline commit: `49fd99ecd0b4247555d071e6fd7e0fff8b3205b9`

## 本次范围

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
- `scripts/hil_single_line_mapping.py` 仅包含一处 lint 空白调整；没有执行任何 HIL 入口。

## 独立复审结论与后续 HIL

Story 4.5 软件范围已满足 `done`：

1. 独立复审 R-01–R-10 已全部整改并逐项增加阻断回归；软件门禁全部通过。
2. 真实 Windows/NI HIL 不属于本轮授权，仍需另行验证 `Dev2/P1.0` 两个电平与实际 `odor` / `compensation` 路线一致。
3. HIL 应继续验证 A 非零门禁、A=0/selector 超时失败、断连、进程退出和 owner 卡住的现场恢复指引。
4. 旧 Story 4.1 的 21-target HIL 证据不得直接复用。
