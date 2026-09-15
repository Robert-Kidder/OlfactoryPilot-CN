# C.3b HIL Commissioning Checklist（进行中）

> **状态：C.3b-3 最终严格零流量 startup auto-connect / Global Stop 生命周期复测 PASS。** 第一次真实 Global Stop 因 NI-DAQmx `-200846` 失败；2026-09-15 人工“重新连接”后的核心复测虽成功，但完整 C.3b-3 仍被 startup 路径与观察时长阻断；同日独立最终严格复测满足 startup 自动连接、两段 ≥15 秒观察、Global Stop、4/4 task release、用户正常 X 关闭及最终 Alicat 回查，解除此项 blocker。历史 [首次 FAIL](c-3b-first-real-app-connect-2026-09-14.md) 与 [中间核心 PASS／完整 BLOCKED](c-3b-first-real-app-connect-retest-2026-09-15.md) 保持原样；新证据见 [最终严格复测 PASS](c-3b-final-real-connect-global-stop-2026-09-15.md)。本文件仍不代表非零供气、真实气口验证或 timing evidence 完成。

## 现场门禁

- [x] **A. 洁净介质：** 现场确认无受试者、无气味样品。
- [x] **B. 出口畅通：** 现场确认待测试出口畅通，没有人为堵塞。
- [x] **C. 急停可达：** 操作者知道如何立即停止/断电。
- [ ] **D. 启动前确认：** 非 simulation/mock；local config、Dev1/Dev2、Alicat 串口/Unit ID、mapping、selector target/polarity 已核对；A/B/C setpoint 已规范化为安全初值。**气味阀1–20真实初始关闭状态仍未确认，因此本项保持未完成。**
- [ ] **E. 单口首轮：** 第一轮只验证一个气口，不批量验证8路。
- [ ] **F. 完整留证：** 记录 command、exact receipt、NI target、flow setpoint/readback、verification run identity、open/close 时间、safe-close 完成和用户现场观察。
- [ ] **G. 真实 timing evidence：** USB-6001 DO 为 software-timed；C.3b 实测 command→DAQ write receipt、early result→close receipt、timeout→close receipt latency，UI countdown/fake clock 不得替代真实 timing evidence。
- [x] **H. 异常即停：** 第一次真实 Global Stop 出现 NI-DAQmx `-200846`，selector/阀门关闭回执不确定；立即停止、禁止重试并由操作者断电。现场未观察到气流或异常动作，但软件回执仍不足以确认安全收口。

## 当前 preflight 状态

| 项目 | 状态 | 依据 / 备注 |
|---|---|---|
| NI Dev1 identity | 现场确认 | USB-6001；`0214581E`（十六进制）=`34887710`（十进制） |
| NI Dev2 identity | 现场确认 | USB-6001；`02145875`（十六进制）=`34887797`（十进制） |
| COM6 | 现场确认 | 本地配置与 2A 实际通信均为 COM6 |
| Alicat A Unit ID | 现场确认 | 面板 `A`；poll 返回 `A`；软件 `a` 大小写等价 |
| Alicat A Baud | 现场确认 | 面板与实际通信均为 19200 |
| Alicat A Setpoint Source | 规范化通过 | firmware `10v14.0-R24`；LSS `S → U`，最终为 unsaved Serial/Front Panel |
| Alicat B | 规范化通过 | firmware `10v14.0-R24`；LSS `S → U`，最终 setpoint=`0` |
| Alicat C | 规范化通过 | firmware `10v14.0-R24`；LSS `S → U`，最终 setpoint=`0` |
| 现场安全准备 | 通过 | 无受试者/气味样品，出口畅通，操作者可立即停止/断电 |
| 真实 App Connect | **最终严格复测通过** | 2026-09-15 新独立 run 由 startup auto-connect 完成首次 acquisition；4/4 safe images、自检、B/C/A zero 与 ≥15 秒 connected zero-flow idle 均通过；无人工 reconnect |
| A/B/C 安全初值 | **通过** | 2C 最终 setpoint=`0/0/0 sccm`、mass flow=`0/0/0 sccm` |
| Setpoint Source | **实机复测通过** | 2026-09-15 新 production transaction 共 57 条只读采样及本轮前后检查均确认 LSS=`U/U/U`、setpoint/flow=`0/0/0` |
| 真实 Global Stop | **首次失败；最终严格复测通过** | 历史 `-200846` FAIL 保留；新 run 人工点击一次，selector safe、20/20 valve close、A/B/C zero、4/4 DO task release、≥15 秒无自动重连、正常 X 关闭 code 0 与最终 Poll/LSS 均通过；C.3b-3 lifecycle blocker 已解除 |

## 本轮记录

- 执行日期/人员：2026-09-10；操作者姓名待补
- 实验台与 local config 标识：`config/local_config.json`；`hal_mode=real`
- Dev1/Dev2 身份：USB-6001；Dev1 `0214581E`=`34887710`，Dev2 `02145875`=`34887797`
- Alicat 串口及 A/B/C Unit ID：`COM6 @ 19200`；poll 返回 `A/B/C`
- C.3b-2A 原始返回与解析：`c-3b-2a-alicat-read-only-poll-2026-09-10.md`
- Firmware / Setpoint Source 原始返回：`c-3b-alicat-ve-lss-read-only-2026-09-10.md`
- Alicat 安全初始状态规范化：`c-3b-alicat-safe-state-normalization-2026-09-10.md`
- 首次真实 App 自动连接与安全停止：`c-3b-first-real-app-connect-2026-09-14.md`（Global Stop 失败，C.3b-3 阻断）
- Power-cycle 后 LSS 只读诊断：`c-3b-lss-readonly-diagnostic-2026-09-14.md`（A/B/C LSS timeout；随后 `aVE` 收到 `A U`，响应归属不可信，Real App 未启动）
- Alicat CR-framed transaction/latency：`c-3b-alicat-transaction-latency-2026-09-15.md`（57/57 transaction 成功，LSS=`U/U/U`，无 timeout/mismatch/desync）
- 真实连接与 Global Stop 复测：`c-3b-first-real-app-connect-retest-2026-09-15.md`（人工重新连接后的 Global Stop 核心 PASS；完整 C.3b-3 因 startup 路径及观察时长不足仍阻断）
- 最终严格真实连接与 Global Stop：`c-3b-final-real-connect-global-stop-2026-09-15.md`（startup auto-connect、4/4 safe image、B/C/A zero、43.082 秒零流量观察、人工 Global Stop、selector/20 路阀门回执、4/4 release、21.516 秒无自动重连、用户 X 关闭 code 0、最终 A/B/C 0 与 U；C.3b-3 PASS）
- verification run identity：
- 验证气口 / NI target / polarity：
- flow setpoint / readback：
- command 与 exact receipt 记录位置：
- open / close / safe-close 时间：
- 用户现场观察：首次自动连接和 zero-flow idle 期间 UI 显示“设备已连接”；没有气口出气，没有异常阀门/selector 动作或设备声响。2026-09-15 中间核心复测及最终严格复测同样无气流、阀门/selector 异常动作或异常声响；最终严格复测的 Global Stop 与窗口 X 由用户人工执行，Stop 后 UI 为“设备未连接 + 重新连接”，用户未点击重新连接。
- 异常与 global stop/recovery 记录：2A 发现 A/B/C setpoint=`1500/1500/500 sccm`、LSS=`S/S/S`；2C 规范化为 setpoint=`0/0/0`、LSS=`U/U/U`。2026-09-14 首次真实 Global Stop 因 NI-DAQmx `-200846` 进入 `RECOVERY_REQUIRED` 并人工断电。串口 framing 修复后，2026-09-15 read-only latency HIL 解除 response-attribution blocker；同日中间核心复测因 startup 路径/观察窗不足继续 BLOCKED。新的最终严格复测无 `-200846`、无 serial desync、无 `RECOVERY_REQUIRED`；Global Stop、release、自然退出与最终回查均 PASS。进程退出时有一条 Python GC `ResourceWarning`，已在最终 evidence 中记录，不是 DAQmx/serial failure。
- 结论与审批签名：
