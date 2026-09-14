---
title: 'C.3b-3C NI On-Demand DO 生命周期与 Global Stop 修复'
type: 'bugfix'
created: '2026-09-14'
status: 'complete'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '13d053092102c94f96e129a99941dcd61e0f8818'
context:
  - '{project-root}/docs/architecture.md'
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/sprint-artifacts/spec-c3b-hil-commissioning.md'
  - '{project-root}/docs/sprint-artifacts/evidence/c-3b-first-real-app-connect-2026-09-14.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 第一次真实 C.3b-3 中 startup auto-connect、self-check、B→C→A zero 和 zero-flow idle 均通过，但 Global Stop 的首个 selector safe write 因 On-Demand DO task 不在 Running 状态触发 NI-DAQmx `-200846`，后续阀门关闭回执均不确定并进入 `RECOVERY_REQUIRED`。

**Approach:** 保留“first physical image = polarity-aware safe packed image”，在该写入成功后显式建立并维持 persistent Running task；Global Stop 在原 owner 上完成全部 safe writes 后才释放 task。以 stateful fake nidaqmx 复现实机错误并覆盖异常停止，不向普通 UI 暴露驱动技术文本。

## Boundaries & Constraints

**Always:** 每个 device/port session 明确区分 prepared、safe image written、running、released；首次 physical write 必须先于 persistent start；后续写固定 `auto_start=False`；意外停止只返回 uncertain/fail-closed，不自动 restart/retry；Global Stop 的 safe writes 全部先于 DO release。真实失败 evidence 永久保持 FAIL。

**Never:** 不把所有写改为 `auto_start=True`；不硬编码 LOW；不改变 Alicat、B→C→A startup zero、auto-connect/reconnect、Manual、Protocol、lease、receipt 或 HardwareProfile；本轮不接触真实硬件、COM6、HIL scripts 或 RealHAL smoke。

## I/O & Edge-Case Matrix

| 场景 | 输入 / 状态 | 预期行为 | 异常处理 |
|---|---|---|---|
| 首次接管 | task created、通道已加入 | safe packed write(auto-start) → explicit start → running | 任一步失败则 Connect 失败并同 owner cleanup |
| 会话写入 | session running | valve/selector write(auto-start=False) 成功 | 不隐式 restart |
| Global Stop | connected、sessions running | selector/odor safe writes → zero/confirm → release | write 失败为 uncertain/RECOVERY_REQUIRED |
| 意外 task stop | session 预期 running、driver 已停止 | 下一写复现 `-200846` 等价失败 | 不重发、不自动 start |
| Reconnect | 旧 session 已释放 | 新 task、新 safe image、新 running session | 不复用 closed task |
| UI 严重异常 | shutdown 技术错误 | 只显示“设备未能正常停止，请立即关闭设备电源” | 原始错误仅入日志/evidence |

</frozen-after-approval>

## Code Map

- `app/services/real_hal.py` — `_DOPortSession`、`prepare_do_output()`、`write_digital_ack()`、`release_do_output()`；根因处和 safe-image 审计日志入口。
- `app/workers/actuation_worker.py` — DO owner 在 `run()` 内 acquire，并在 safety commands 排空后 `release_do_output()`；保持唯一 owner。
- `app/services/shutdown_service.py` — 既有顺序为 A zero → selector → odors → B/C/A zero → owner handoff/release → serial；无需重排，只需由 running session 支撑。
- `app/controllers/main_controller.py` — safe-stop receipt 的原始 driver message 当前可进入 Header/InfoBar；Global Stop 终态已有统一人工动作提示。
- `app/views/main_window.py`、`app/views/product_text.py` — 技术错误到用户安全提示的 presentation 边界。
- `tests/test_do_lifecycle.py` — 建立可复现 auto-start 单样本结束后 not-running 的 stateful fake，并覆盖 acquire/write/release/reconnect/failure。
- `tests/test_shutdown_actuation.py`、`tests/test_startup_connection.py`、`tests/test_product_ui.py` — Global Stop 顺序、fail-closed 与 UI 技术文本隔离。

## Tasks & Acceptance

**Execution:**
- [x] `app/services/real_hal.py` — 加入明确 session lifecycle、safe image 后 persistent start、一次性 structured audit log 和保守失败处理。
- [x] `app/controllers/main_controller.py` / `app/views/main_window.py` — 阻止 DAQmx 原始错误进入普通 UI，严重安全失败只保留一个可执行提示。
- [x] `tests/test_do_lifecycle.py` — 用 stateful fake 复现旧 bug并覆盖正常/失败/reconnect/polarity/顺序。
- [x] `tests/test_shutdown_actuation.py` / UI tests — 证明 safe writes 先于 release，失败继续 RECOVERY_REQUIRED 且 UI 无技术泄漏。
- [x] `docs/sprint-artifacts/spec-c3b-hil-commissioning.md` — 记录最终根因与 C.3b-3 仍待真实重跑；不得改写历史 FAIL。

**Acceptance Criteria:**
- Given 新建 On-Demand DO task，when prepare，then 第一次 physical write 是完整 polarity-aware safe image，且 explicit start 发生在其后。
- Given persistent running session，when valve、selector 或 Global Stop 写入，then 使用 `auto_start=False` 成功且 release 晚于最后 safe write。
- Given task 意外停止，when 后续写入，then 返回 uncertain 并保持 fail-closed，不自动重启或重发。
- Given Global Stop 发生驱动错误，when UI 呈现，then 原始 DAQmx 文本仅在日志/evidence，普通用户只看到统一断电提示。
- Given离线验证全部通过，when审阅历史 evidence，then 2026-09-14 Global Stop 仍为 FAIL，C.3b-3 仍为 blocked。

## Implementation Notes

- NI/nidaqmx 官方语义：`Task.start()` 显式进入 Running；未显式使用 Start/Stop 而重复 Read/Write 会反复 start/stop；单样本 `write` 默认 auto-start。实机 `-200846` 证明旧 safe write 返回后 task 未保持 Running。
- 候选顺序经官方 task state model 与实机证据确认：add channel → safe packed write(`auto_start=True`) → explicit `task.start()` → later writes(`auto_start=False`) → final safe writes → close。
- 官方依据：[nidaqmx Task API](https://nidaqmx-python.readthedocs.io/en/latest/task.html#nidaqmx.task.Task.start) 明确 `start()` 进入 Running，省略显式 Start/Stop 的重复写会反复启动/停止；同页 `write()` 说明 On-Demand 是未配置 timing 时的默认类型，且 `auto_start` 只决定未显式 start 时是否自动启动。项目锁定的 nidaqmx 0.9.0 本地 docstring 与该 contract 一致。
- `close()` 是 clear 的别名，会 abort（如必要）并释放 task reservation；因此 Global Stop 必须先在原 Running session 上取得最终 safe receipts，再由原 owner close/release。
- 实现没有把所有写改成 `auto_start=True`：只有每个 port acquisition 的 first safe image 使用一次；随后显式 start，正常动作和 Global Stop 全部保持 `auto_start=False`。明确 `-200846` 会把 session 锁存为非 Running，之后 lifecycle gate 拒绝再次触碰 driver。
- 独立审查发现并移除了 release 后重新 acquisition 的旧 fallback；selector/odor safe receipt 失败现在继续释放资源但保持 `RECOVERY_REQUIRED`，不能用第二套 task 覆盖原失败证据。
- 测试进程会隔离仓库默认的 shutdown runtime record，避免 Mock/offscreen 退出覆盖现场 `RECOVERY_REQUIRED` 标记；显式临时路径的持久化测试仍正常。

## Spec Change Log

- 2026-09-14：完成 C.3b-3C 离线修复与三路独立复审；历史 C.3b-3 Global Stop 仍为 FAIL，真实重跑未执行。

## Review Triage Log

| 来源 | 严重度 | 处理 | 结论 |
|---|---|---|---|
| NI lifecycle review | medium | direct-fix | 明确 `-200846` 后锁存 `running=False`；第二次写在 HAL gate 被拒绝，不 restart/retry。 |
| Shutdown review | high | direct-fix | 删除 release 后 fallback reacquisition；原 safe receipt 失败保持 `RECOVERY_REQUIRED`。 |
| Shutdown review | medium | direct-fix | stateful fake 增加同一 Running session 多次 safe write 后才 close 的顺序回归。 |
| UI/test review | high | direct-fix | pytest 同时隔离仓库 HIL shutdown record 的读取和写入，现场恢复标记不再被 Mock 测试覆盖。 |
| UI/test review | medium | direct-fix | 只有完整 Retry transaction 进入 CONNECTED 后才清除持续断电提示；失败仍保留。 |
| Final reviews | — | pass | 三路复审均确认无剩余代码 blocker。 |

## Verification

**Commands:**
- `python -m ruff check .`
- `python -m pytest tests/test_do_lifecycle.py tests/test_shutdown_actuation.py tests/test_safe_stop.py tests/test_actuation_worker.py tests/test_startup_connection.py tests/test_product_ui.py`
- `python -m pytest`
- `git diff --check`
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 build`
- Mock/offscreen simulation smoke（不得运行 real 模式）。

**Results:**
- 定向 lifecycle / Global Stop / SafeStop / ActuationWorker / connection / UI：`176 passed`，0 failures。
- 完整 pytest：`1341 passed, 1 skipped`，0 failures；skip 为既有环境条件项。
- Ruff：`python -m ruff check .` 通过。
- `git diff --check` 通过；仅 Git 提示工作区换行规范，不是 diff error。
- PyInstaller：`scripts/run-ci.ps1 build` 成功，必需 EXE/default config/用户手册产物存在。
- 隔离的 MockHAL/offscreen simulation：startup auto-connect exactly once，connected/ready，正常 lifecycle shutdown；现场 HIL shutdown marker 的 SHA256 前后不变。
- simulation 不覆盖真实 nidaqmx task state，也不构成 C.3b-3 真实复测证据。
