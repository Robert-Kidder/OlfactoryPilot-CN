# C.3b HIL Commissioning Checklist（进行中）

> **状态：C.3b-1、C.3b-2A、VE/LSS 只读查询、C.3b-2C 与新的 Alicat CR-framed transaction 实机只读验证已完成。第一次真实 Global Stop 因 NI-DAQmx `-200846` 失败；2026-09-15 人工“重新连接”后的 Global Stop 核心复测已成功，未再出现 `-200846`，selector 与气味阀关闭回执均成功。但本次不是 startup auto-connect acquisition，且两个 10 秒观察窗不足，因此完整 C.3b-3 仍阻断，等待一次严格同规格重跑。** 证据见 [首次真实连接/停止失败](c-3b-first-real-app-connect-2026-09-14.md)、[LSS 响应错位诊断](c-3b-lss-readonly-diagnostic-2026-09-14.md)、[Alicat transaction/latency](c-3b-alicat-transaction-latency-2026-09-15.md) 和 [真实连接/Global Stop 复测](c-3b-first-real-app-connect-retest-2026-09-15.md)。本文件不代表真实气口验证完成。

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
| 真实 App Connect | **连接 transaction 通过；完整 startup 复测未完成** | 2026-09-15 startup 被历史 unsafe-shutdown latch 阻断；人工“重新连接”后 self-check、B/C/A zero 与 connected 通过 |
| A/B/C 安全初值 | **通过** | 2C 最终 setpoint=`0/0/0 sccm`、mass flow=`0/0/0 sccm` |
| Setpoint Source | **实机复测通过** | 2026-09-15 新 production transaction 共 57 条只读采样及本轮前后检查均确认 LSS=`U/U/U`、setpoint/flow=`0/0/0` |
| 真实 Global Stop | **首次失败；核心复测通过** | 2026-09-14 因 `-200846` 失败；2026-09-15 人工重新连接后 selector safe、20/20 valve close、A/B/C zero 和 shutdown success，未复现 `-200846`；完整 C.3b-3 仍待严格重跑 |

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
- verification run identity：
- 验证气口 / NI target / polarity：
- flow setpoint / readback：
- command 与 exact receipt 记录位置：
- open / close / safe-close 时间：
- 用户现场观察：首次自动连接和 zero-flow idle 期间 UI 显示“设备已连接”；没有气口出气，没有异常阀门/selector 动作或设备声响。2026-09-15 人工重新连接及 Global Stop 核心复测期间同样未观察到气流、阀门/selector 动作或异常声响。
- 异常与 global stop/recovery 记录：2A 发现 A/B/C setpoint=`1500/1500/500 sccm`、LSS=`S/S/S`；2C 规范化为 setpoint=`0/0/0`、LSS=`U/U/U`。2026-09-14 首次真实 Global Stop 因 NI-DAQmx `-200846` 进入 `RECOVERY_REQUIRED` 并人工断电。串口 framing 修复后，2026-09-15 read-only latency HIL 解除 response-attribution blocker；同日人工重新连接后的 Global Stop 未再出现 `-200846`，selector/20 路 valve/A-B-C zero 回执及 shutdown record 均成功，但完整 C.3b-3 因 startup 路径和观察时长不足仍待重跑。
- 结论与审批签名：
