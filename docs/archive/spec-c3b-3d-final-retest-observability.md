---
title: 'C.3b-3D 最终严格复测前审计与最小观测补强'
type: 'bugfix'
created: '2026-09-15'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context:
  - '{project-root}/docs/sprint-artifacts/spec-c3b-hil-commissioning.md'
  - '{project-root}/docs/sprint-artifacts/evidence/c-3b-first-real-app-connect-retest-2026-09-15.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 上一轮真实 Global Stop 核心复测已通过，但完整 C.3b-3 仍因历史 unsafe-shutdown latch、两个观察窗不足、缺少逐 DO task release 直接时间证据，以及 Ctrl+C 导致非正常退出而阻断。

**Approach:** 离线确认成功 Global Stop 对持久化 shutdown record 与下一进程 startup auto-connect 的解除语义；在不改变 NI 控制顺序的前提下增加每个 DO port session 的低频 release/close 审计，并补齐跨进程 latch、正常窗口关闭及资源释放回归。最终实机 runbook 使用连接后和停止后各至少 15 秒的操作余量，并只接受用户关闭窗口后的自然零退出。

</frozen-after-approval>

## Implementation Notes

- 审计 `ShutdownService.shutdown()`、`_persist_event()`、`MainController._handle_shutdown_event()` 与 `build_application()`：成功 Global Stop 会在同一 record path 写入新的 `result=success` 记录并清除当前进程 latch；下一进程载入成功记录时不会建立 unsafe latch。持久化失败仍保守保留旧记录并在下一启动阻断，不绕过门禁。
- `RealHAL.release_do_output()` 复用单一 close helper；每个 session 在原有 `Task.close()` 前后记录同一 monotonic clock 的 `close_started_ns` / `close_actual_ns`、session、device、port、reason 与 result。正常 owner handoff 和 prepare rollback 都使用该审计，不增加 start/stop/write 或改变释放顺序。
- 新增跨进程式回归：历史 unsafe record 阻止 startup 零 acquisition；人工 recovery 连接并成功 Global Stop 后 record 被成功结果替代；下一应用实例 startup auto-connect exactly once。
- 新增正常 GUI 退出回归：CONNECTED → Global Stop success → DISCONNECTED → `window.close()` → Qt event loop 自然返回 0；关闭后不重新 acquisition，worker、DO 与 serial 均保持释放，不依赖 KeyboardInterrupt。
- 最终实机 runbook：minimal Alicat preflight；确认旧 latch 已解除；启动 Real App 且只接受 startup auto-connect acquisition；CONNECTED 后等待 ≥15 秒；用户点击一次 Global Stop；确认完整安全结果与“设备未连接”；等待 ≥15 秒确认无自动重连；用户点击窗口 X；不得发送 Ctrl+C/terminate/kill；进程须自然 exit code=0；核验每个 DO task release success；最终只读 Poll/LSS；确认 COM6、NI 与 Python 进程全部释放。任一步失败即 HALT，不以人工重新连接或临时补救把同一 run 算 PASS。
- 完整验证：相关定向测试 155 passed；`python -m pytest` 为 1394 passed、1 skipped；Ruff、`git diff --check`、PyInstaller 和 offscreen Mock simulation smoke 均通过。simulation 仅证明产品生命周期，不替代真实 NI release evidence。
- 完整 pytest 首次发现既有已完成串口 framing spec 仍留在活动目录；按仓库文档层级将其原样归档到 `docs/archive/` 并修正 deferred-work 引用，随后 repository hygiene 与完整套件通过。
- 历史复测 evidence 的 Git blob hash 在实施前后均为 `4d44a7a0ffd71c7039ddb8fd85e22c4f1b676bd1`，事实结论未改写。
- 本地隔离复审确认：未改变 NI write/start/stop 次数、Global Stop 偏序、Alicat transaction、B→C→A、HardwareProfile 或 UI；release 失败继续保留 session/owner 并 fail closed。当前会话未授权子代理委派，因此未运行 BMAD Blind Hunter 子代理层。
