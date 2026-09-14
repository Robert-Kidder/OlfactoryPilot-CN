# C.3b HIL Commissioning Checklist（进行中）

> **状态：C.3b-1、C.3b-2A、VE/LSS 只读查询和 C.3b-2C 已完成；C.3b-3 首次真实自动连接通过，但第一次真实 Global Stop 因 NI-DAQmx `-200846` 无法确认阀门/selector 安全收口，当前阻断。Global Stop 离线修复后的复测 preflight 又发现 Alicat CR framing/迟到响应归属问题，因此 Real App 仍未重新启动。** 证据见 [live-data poll](c-3b-2a-alicat-read-only-poll-2026-09-10.md)、[VE/LSS 查询](c-3b-alicat-ve-lss-read-only-2026-09-10.md)、[2C 规范化](c-3b-alicat-safe-state-normalization-2026-09-10.md)、[首次真实连接/停止](c-3b-first-real-app-connect-2026-09-14.md) 和 [LSS 只读响应错位诊断](c-3b-lss-readonly-diagnostic-2026-09-14.md)。本文件不代表真实气口验证完成。

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
| 真实 App Connect | **连接通过，停止阻断** | 2026-09-14 startup auto-connect exactly once；self-check 与 B/C/A zero 通过；Global Stop 进入 `RECOVERY_REQUIRED` |
| A/B/C 安全初值 | **通过** | 2C 最终 setpoint=`0/0/0 sccm`、mass flow=`0/0/0 sccm` |
| Setpoint Source | **复测阻断** | 2C 最终 LSS=`U/U/U`；power-cycle 后 poll 为零，但本次 LSS direct query 因 timeout/响应错位未能可靠确认 |
| 第一次真实 Global Stop | **失败/阻断** | A/B/C zero 成功；NI-DAQmx `-200846`；selector 与气味阀关闭回执不确定；操作者已断电 |

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
- verification run identity：
- 验证气口 / NI target / polarity：
- flow setpoint / readback：
- command 与 exact receipt 记录位置：
- open / close / safe-close 时间：
- 用户现场观察：首次自动连接和 zero-flow idle 期间 UI 显示“设备已连接”；没有气口出气，没有异常阀门/selector 动作或设备声响。Global Stop 失败后操作者已断电，仍未观察到上述异常。
- 异常与 global stop/recovery 记录：2A 发现 A/B/C setpoint=`1500/1500/500 sccm`、LSS=`S/S/S`；2C 在明确授权下逐台规范化为 setpoint=`0/0/0`、LSS=`U/U/U`。2026-09-14 首次真实 Global Stop 的 A/B/C zero 成功，但 NI-DAQmx `-200846` 导致 selector/阀门关闭回执不确定并进入 `RECOVERY_REQUIRED`；本轮立即停止并人工断电。离线修复后重新 preflight 时，A/B/C poll 仍为零，但三台 LSS 均 timeout，且后续 `aVE` 收到 `A U`，因此按 response attribution blocker 停止，没有启动 Real App。
- 结论与审批签名：
