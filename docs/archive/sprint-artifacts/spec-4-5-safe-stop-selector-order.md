---
title: 'Story 4.5：全局停止顺序与三通阀模型'
type: 'feature'
created: '2026-08-17'
status: 'done'
baseline_commit: '49fd99ecd0b4247555d071e6fd7e0fff8b3205b9'
review_loop_iteration: 0
context:
  - '{project-root}/docs/sprint-artifacts/evidence/gas-path-requirements-2026-08-01.md'
  - '{project-root}/docs/sprint-artifacts/4-1-cleaning-automation.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 当前仍把 `Dev2/P1.0` 当作随气味阀“全关”的主阀，部分停止路径先写低 DO、后清零 A，可能在 A 非零时切换路线并误报安全。

**Approach:** 建立独立 selector 与 `SafeStopPlan`，统一停止路径的“A=0 匹配 receipt → selector 安全路线”门禁，并复用现有 owner/receipt 基础。

## Boundaries & Constraints

**Always:** 保护 Story 4.1 未提交资产；先阻止新动作并失效旧 epoch；仅匹配、非 stale、无冲突的 A=0 receipt 可授权 selector；关键失败/超时/迟到/冲突/未知均进入 `RECOVERY_REQUIRED`；普通阀集合仅 1–20；硬件写入仍由 Actuation/Flow owner 经 HAL 完成。

**Ask First:** 改变 selector 极性/安全路线、扩展范围、改写硬件底层或运行 HIL。

**Never:** 不实施 Story 4.6/UI/自动实验；不运行 HIL；不新增硬件旁路；不使用 View 定时；不执行破坏性 Git 或全仓格式化。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 正常停止 | 活动动作、A 非零、selector 在气味路线 | fence → A=0 receipt → selector 安全路线 → 其余收敛 | 完整证据后才报告安全 |
| A 清零失败/超时 | 拒绝、设备失败、无回执 | selector 不写入，气味阀尽力关闭 | `RECOVERY_REQUIRED` |
| 异常 A receipt | 身份不匹配、stale、迟到或冲突 | 不推进 selector | `RECOVERY_REQUIRED` |
| selector 不确定 | 失败、超时、冲突或未知 | 不宣称安全路线 | `RECOVERY_REQUIRED` |
| 抢占/handoff | stop 与普通动作交错 | 旧 epoch 不推进；原 owner 完成交还 | 不完整则恢复必需 |

</frozen-after-approval>

## Code Map

- `app/models/safe_stop.py` -- selector、停止身份/阶段与证据匹配。
- `app/models/app_state.py`、`config/default_config.json` -- selector 配置与 legacy 兼容。
- `app/services/valve_service.py` -- odor-only 集合和 selector 路由。
- `app/services/flow_service.py`、`app/workers/flow_worker.py` -- Owner 内 A=0 receipt。
- `app/workers/actuation_worker.py`、`app/services/shutdown_service.py` -- 停止偏序与 handoff。
- `tests/` -- 顺序和 fault matrix。
- `docs/archive/sprint-artifacts/4-5-safe-stop-selector-order.md` -- 实施与证据记录。

## Tasks & Acceptance

**Execution:**
- [x] `app/models/safe_stop.py` -- 建立纯状态/证据契约。
- [x] `app/models/app_state.py`、`config/*.json`、`app/services/valve_service.py` -- 独立 selector。
- [x] `app/services/flow_service.py`、`app/workers/flow_worker.py` -- A=0 owner receipt。
- [x] `app/workers/actuation_worker.py`、`app/services/shutdown_service.py` -- 统一偏序并适配清洗。
- [x] `tests/` -- 覆盖矩阵和 Story 4.1 回归。
- [x] `docs/archive/sprint-artifacts/4-5-safe-stop-selector-order.md`、`sprint-status.yaml` -- 记录证据，保持待审查/HIL。

**Acceptance Criteria:**
- Given global/异常/shutdown 停止，when 开始收敛，then 先 fence/失效 epoch，且 selector 严格晚于匹配 A=0 receipt。
- Given A receipt 失败/超时/迟到/stale/冲突，then selector 不获授权且结果为 `RECOVERY_REQUIRED`。
- Given selector 失败/超时/未知，then 不报告安全停止并保留原因。
- Given 生产配置，when 构造普通全关集合，then 只有 20 个气味阀，selector 仅经专用 API 出现。
- Given 自动化回归，then 既有 lease/epoch/receipt/抢占/handoff 保持且无硬件访问。

### Review Findings

- [x] [Review][Patch] 通用业务 category 的 `valve=0` 命令仍可绕过 `SafeStopPlan` 与 A=0 receipt 直接写 selector [`app/workers/actuation_worker.py:682`](../../../app/workers/actuation_worker.py#L682)
- [x] [Review][Patch] safety/selector receipt 的 owner 校验遗漏 `arm_epoch`、`expected_ns`、`safety_generation` 与 `action_kind`，可接受身份损坏的安全证据 [`app/workers/actuation_worker.py:4365`](../../../app/workers/actuation_worker.py#L4365)
- [x] [Review][Patch] handoff 请求等待期间再次 stop 可提前进入 `STOPPED`，绕过 `SafeStopPlan.complete()` 与 lease handoff [`app/workers/actuation_worker.py:2883`](../../../app/workers/actuation_worker.py#L2883)
- [x] [Review][Patch] 全局 shutdown 抢占 cleaning 时可把单一 actuation fence 当成完整 maintenance handoff，并遗留失效 cleaning lease/token 状态 [`app/workers/actuation_worker.py:489`](../../../app/workers/actuation_worker.py#L489)
- [x] [Review][Patch] 活动协议全局 shutdown 成功后 Controller 保留幽灵 `_protocol_lease_epoch`，重连后继续发布 `device_lease=protocol` [`app/controllers/main_controller.py:3221`](../../../app/controllers/main_controller.py#L3221)
- [x] [Review][Patch] 无协议 lease 时执行 protocol stop 会遗留 `FlowWorker._safe_stop_identity`，后续协议 lease 永久拒绝 [`app/controllers/main_controller.py:3567`](../../../app/controllers/main_controller.py#L3567)
- [x] [Review][Patch] cleaning 对内容完全相同的重复 receipt 静默接受，未按整改契约锁定 `RECOVERY_REQUIRED` [`app/workers/actuation_worker.py:3483`](../../../app/workers/actuation_worker.py#L3483)
- [x] [Review][Patch] cleaning 的 `flow_start` receipt 身份冲突后只关闭 odor，不提交 A/B/C 清零，非零 setpoint 可持续存在 [`app/workers/actuation_worker.py:3395`](../../../app/workers/actuation_worker.py#L3395)
- [x] [Review][Patch] 反向 selector 极性会生成 `WARMUP/CLOSE`，但 session validator 仅接受 `WARMUP/OPEN`，录制协议无法启动 [`app/services/session_file_service.py:227`](../../../app/services/session_file_service.py#L227)
- [x] [Review][Patch] 可执行 HIL benchmark 的异常 teardown 与 LOW_FLOW recovery 未适配统一 safe-stop/handoff，可能保留非零 A 或形成循环等待 [`scripts/hil_actuation_benchmark.py:1140`](../../../scripts/hil_actuation_benchmark.py#L1140)

## Spec Change Log

## Design Notes

`SafeStopPlan` 只管理阶段和证据，不访问硬件。气味阀和其他 owner 可 best-effort 收敛，但关键证据缺失即锁定恢复态。

## Verification

**Commands:**
- Story 4.5 定向 pytest -- `415 passed in 11.39s`。
- `python -m pytest -q` -- `762 passed in 19.59s`。
- `python -m ruff check .` -- `All checks passed!`。
- `git diff --check` -- 无空白错误；仅有工作副本 LF→CRLF 提示。
- 独立复审的 R-01–R-10 已全部整改并通过软件门禁；未运行 HIL，真实硬件验证仍须另行授权。

## Suggested Review Order

**停止偏序与 owner 边界**

- 从异常停止入口理解 fence、A=0 与 selector 的统一收敛。
  [`actuation_worker.py:1241`](../../../app/workers/actuation_worker.py#L1241)

- 全局 shutdown 严格按证据推进，并在 handoff 后才允许兜底。
  [`shutdown_service.py:191`](../../../app/services/shutdown_service.py#L191)

- Flow owner 生成相关 A=0 receipt 并拒绝旧身份。
  [`flow_worker.py:314`](../../../app/workers/flow_worker.py#L314)

**selector 与证据模型**

- 独立二选一 selector 定义安全路线和电平约束。
  [`safe_stop.py:15`](../../../app/models/safe_stop.py#L15)

- 纯证据门禁拒绝失败、迟到、stale 与冲突 receipt。
  [`safe_stop.py:83`](../../../app/models/safe_stop.py#L83)

- selector 专用 API 与普通气味阀关闭集合彻底分离。
  [`valve_service.py:157`](../../../app/services/valve_service.py#L157)

- 配置解析拒绝无效路线及 selector/气味阀 line 冲突。
  [`app_state.py:68`](../../../app/models/app_state.py#L68)

**测试与交付证据**

- 单元矩阵直接覆盖 selector 与 receipt 边界。
  [`test_safe_stop.py:136`](../../../tests/test_safe_stop.py#L136)

- worker 测试验证异常顺序及 selector receipt 身份冲突。
  [`test_actuation_worker.py:1481`](../../../tests/test_actuation_worker.py#L1481)

- shutdown 测试验证 owner 异常仍进入恢复态。
  [`test_shutdown_actuation.py:322`](../../../tests/test_shutdown_actuation.py#L322)

- 实施记录汇总文件、门禁和剩余 HIL。
  [`4-5-safe-stop-selector-order.md:1`](4-5-safe-stop-selector-order.md#L1)
