# C.3b HIL Commissioning Checklist（进行中）

> **状态：C.3b-1、C.3b-2A、VE/LSS 只读查询和 C.3b-2C Alicat 安全初始状态规范化已完成；真实 App Connect 与任何 NI/selector/气味阀动作仍未授权。** 证据见 [live-data poll](c-3b-2a-alicat-read-only-poll-2026-09-10.md)、[VE/LSS 查询](c-3b-alicat-ve-lss-read-only-2026-09-10.md) 和 [2C 规范化](c-3b-alicat-safe-state-normalization-2026-09-10.md)。本文件不代表真实气口验证完成。

## 现场门禁

- [x] **A. 洁净介质：** 现场确认无受试者、无气味样品。
- [x] **B. 出口畅通：** 现场确认待测试出口畅通，没有人为堵塞。
- [x] **C. 急停可达：** 操作者知道如何立即停止/断电。
- [ ] **D. 启动前确认：** 非 simulation/mock；local config、Dev1/Dev2、Alicat 串口/Unit ID、mapping、selector target/polarity 已核对；A/B/C setpoint 已规范化为安全初值。**气味阀1–20真实初始关闭状态仍未确认，因此本项保持未完成。**
- [ ] **E. 单口首轮：** 第一轮只验证一个气口，不批量验证8路。
- [ ] **F. 完整留证：** 记录 command、exact receipt、NI target、flow setpoint/readback、verification run identity、open/close 时间、safe-close 完成和用户现场观察。
- [ ] **G. 真实 timing evidence：** USB-6001 DO 为 software-timed；C.3b 实测 command→DAQ write receipt、early result→close receipt、timeout→close receipt latency，UI countdown/fake clock 不得替代真实 timing evidence。
- [ ] **H. 异常即停：** 出现 unexpected valve/flow、通信中断、receipt mismatch、安全状态变化、mapping 与现场出口不符或无法安全归零/关闭，立即停止本轮并执行既有 global stop/recovery，不得自动继续下一气口。

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
| 真实 App Connect | 未授权 | 不得执行 |
| A/B/C 安全初值 | **通过** | 2C 最终 setpoint=`0/0/0 sccm`、mass flow=`0/0/0 sccm` |
| Setpoint Source | **通过** | 2C 最终 LSS=`U/U/U` |

## 本轮记录

- 执行日期/人员：2026-09-10；操作者姓名待补
- 实验台与 local config 标识：`config/local_config.json`；`hal_mode=real`
- Dev1/Dev2 身份：USB-6001；Dev1 `0214581E`=`34887710`，Dev2 `02145875`=`34887797`
- Alicat 串口及 A/B/C Unit ID：`COM6 @ 19200`；poll 返回 `A/B/C`
- C.3b-2A 原始返回与解析：`c-3b-2a-alicat-read-only-poll-2026-09-10.md`
- Firmware / Setpoint Source 原始返回：`c-3b-alicat-ve-lss-read-only-2026-09-10.md`
- Alicat 安全初始状态规范化：`c-3b-alicat-safe-state-normalization-2026-09-10.md`
- verification run identity：
- 验证气口 / NI target / polarity：
- flow setpoint / readback：
- command 与 exact receipt 记录位置：
- open / close / safe-close 时间：
- 用户现场观察：
- 异常与 global stop/recovery 记录：2A 发现 A/B/C setpoint=`1500/1500/500 sccm`、LSS=`S/S/S`；2C 在明确授权下逐台规范化为 setpoint=`0/0/0`、LSS=`U/U/U`，没有触发异常停止。未启动 App Global Stop
- 结论与审批签名：
