---
title: 'C.3b 设备连接状态与重新连接交互收敛'
type: 'bugfix'
created: '2026-09-14'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context:
  - '{project-root}/docs/ux-design.md'
  - '{project-root}/docs/architecture.md'
  - '{project-root}/docs/sprint-artifacts/spec-c3b-hil-commissioning.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Global Stop 成功后，内部终止标志会阻止同进程重新连接，`DISCONNECTED` 展示又隐藏连接按钮；普通界面还把 startup failure、runtime disconnect、自检失败等内部差异暴露成多套文案和重复通知。

**Approach:** 保留详细内部安全状态机，但在 View 中集中映射为“正在连接…”、“设备已连接”、“设备未连接”三种产品表现；所有未连接且无连接事务的普通状态统一显示“重新连接”，并继续复用唯一连接 transaction。Global Stop 或断线安全收口成功后允许人工重新连接且不恢复旧动作；只有无法确认安全收敛时持续显示面向使用者的高优先级断电提示。

</frozen-after-approval>

## Implementation Notes

- 无 intent gap，无不可逆操作；本轮仅修改 Controller/View、相关测试、长期文档及 MockHAL simulation 截图证据。
- `MainWindow.render_connection_phase()` 是集中 presentation 入口；Header 的固定 status/action slots 必须保留。
- Global Stop 成功后需解除旧的进程终止连接门禁，失败时仍保留 fail-closed/`RECOVERY_REQUIRED`；任何重新连接仍进入 `request_hardware_connection()`。
- Global Stop/runtime disconnect 安全完成后清除 Manual actual-open 表现和旧执行身份；selected 与 A/B/C/duration draft 可保留，但不得自动执行。
- 截图使用 MockHAL simulation 与 owned temp session；不运行 clean-clone、HIL、RealHAL 或任何真实硬件入口。
- 实现将所有内部连接 phase 通过 `connection_presentation_for_phase()` 映射到三种产品状态；普通失败不创建 InfoBar，安全收口无法确认时只显示固定的持续断电提示。
- Global Stop 成功会完整释放资源并清除 Manual actual-open/旧执行身份；`_telemetry_terminal_stopped` 只拦截排队旧结果，用户点击“重新连接”后仍进入唯一 `request_hardware_connection(source="retry")`。
- MockHAL simulation 证据覆盖首次连接、Global Stop 后等待 10 秒无自动重连、人工重连成功三个画面；截图不构成真实硬件证据。
- 独立审查发现的安全事件误分类、unsafe retry 过早清锁、晚到 Manual snapshot、非有限流量展示、未知 phase 默认可重连以及证据发布完整性问题均已补回归并修复。

## Review Triage Log

| ID | Verdict | 处理与证据 |
|---|---|---|
| Blind R1 | high | patch：`render_last_shutdown()` 只有在事件明确包含失败 `result` 时才显示持续断电提示；普通 LOW_FLOW/DATA_STALE 事件不再误报关闭失败。 |
| Blind R2 | high | patch：删除低流量测试中对 `connection-safety` 的人工清除，直接验证实际 telemetry 后仍是“气流不足”提示。 |
| Blind R3 | high | patch：unsafe retry 不再预先清除 latch/提示；只有完整 readiness 成功后清除，重试任一步失败继续进入 `RECOVERY_REQUIRED`。 |
| Blind R4 | false | 现有 telemetry 使用 interlock sample timestamp 拒绝旧样本，flow result 由 execution epoch 与 connection request context 隔离；新增 Global Stop/runtime reconnect 中注入旧 connected payload 的回归，未能改变新事务。 |
| Blind R5 | high | patch：Global Stop 成功后推进 Manual generation fence，Controller 忽略 stop 前排队的旧 identity snapshot。 |
| Blind R6 | medium | patch：Global Stop 测试注入带 actual-open/recovery 证据的旧 Manual snapshot，验证 stop 与 reconnect 后均不恢复旧身份、DO 或非零 flow。 |
| Blind R7 | low | patch：测试 fixture 的 `open_confirmed` 改用 external port，符合 View 消费语义。 |
| Blind R8 | medium | patch：非有限 airflow 将 presentation 标记为未连接/暂无数据，不再把缺失观测伪造成 0 测量值。 |
| Blind R9 | medium | patch：connection phase 映射改为穷举；未知 phase 抛错且不会显示可操作的“重新连接”。 |
| Blind R10 | false | 产品决策明确普通连接失败只需“设备未连接”；配置/极性 blocker 仍禁用 transaction、保留日志且 Settings 始终可进入，增加另一套 Header 文案会违背本轮三态规则。 |
| Blind R11 | medium | patch：截图先写 owned temp，全部断言成功后原子发布目录；既有 evidence 目录存在时直接拒绝覆盖，失败会清理本次 session。 |
| Blind R12 | medium | patch：新增受测试的 manifest，校验三张截图 SHA-256、MockHAL/无真实硬件标记、10 秒等待及连接请求计数 1→1→2。 |
