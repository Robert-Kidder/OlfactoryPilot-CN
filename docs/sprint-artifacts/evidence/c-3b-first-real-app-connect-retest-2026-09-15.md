# C.3b-3 真实连接与 Global Stop 复测证据（部分通过，仍阻断）

> **结论：NI persistent DO lifecycle 与真实 Global Stop 核心复测通过，未再出现 `-200846`；但本轮 startup auto-connect 被历史 unsafe-shutdown latch 阻断，实际 acquisition 来自用户人工点击一次“重新连接”，且 connected 后和 Global Stop 后的观察时间均不足规范要求的 10 秒，因此不能把完整 C.3b-3 标为 PASS。**

## 范围与现场条件

- 日期：2026-09-15（Asia/Shanghai）
- 分支：`hil/c3b-commissioning`
- 被测基线包含：`436922c744c028d2b4f18015e43f373b560190b7`、`f6fc5584ef1997867d803c26777f247977f8b956`、`8516dd672bcfef16fc9ef7a53bcf88e3657af4fb`
- 启动命令：`python -m app.main --local-config config/local_config.json`
- 现场确认：无受试者、无气味样品、出口畅通，操作者知道如何立即停止/断电。
- 本轮未开始供气、未修改 Manual A/B/C、未选择或验证气口、未运行额外 HIL script。

第一次真实失败仍由 `c-3b-first-real-app-connect-2026-09-14.md` 保持原样记录：startup auto-connect PASS、Global Stop FAIL、DAQmx `-200846`、`RECOVERY_REQUIRED`。本文件不覆盖该历史结果。

## Minimal Alicat preflight

使用生产 `AlicatSerialSession`，单一健康 session 串行执行 A/B/C Poll、LSS；未发送 setpoint 或配置命令。

```text
A Poll: b'A +014.62 +028.33 +0.0000 +0.0000 +0.0000    Air\r'
A LSS:  b'A U\r'
B Poll: b'B +014.62 +028.41 +0.0000 +0.0000 +0.0000    Air\r'
B LSS:  b'B U\r'
C Poll: b'C +014.62 +028.20 +0.0000 +0.0000 +0.0000    Air\r'
C LSS:  b'C U\r'
```

- setpoint=`0/0/0 sccm`；mass flow=`0/0/0 sccm`；LSS=`U/U/U`。
- 6/6 transaction 成功；timeout、partial、mismatch、desync 均为 0。
- 查询后 COM6 正常关闭。
- HardwareProfile 未变化：Dev1/Dev2；启用气口 2/4/6/8/12/14/16/18；selector=`Dev2/P1.0`；20 路 valve + selector 共 21 个唯一受管输出，配置推导的 safe physical level 均为 LOW。

## Startup 与人工重新连接

15:35:19 App 正常显示窗口，但读取到第一次失败留下的未确认 shutdown record。产品安全门禁阻止 startup auto-connect acquisition，用户看到“设备未连接”。没有因此自动循环重试。

用户随后人工点击一次“重新连接”；UI 短暂显示“设备连接中……”后显示“设备已连接”。这次人工操作超出了本轮原计划的纯 startup acquisition 路径，但调用的是同一权威 connection transaction；本文件按实际发生事实记录，不把它写成 startup auto-connect PASS。

## 真实 NI safe acquisition

终端按以下顺序记录每个 port 的首次 safe image；每条 safe-image `result=success` 日志都先于对应的 `DO session running` 日志：

| 外部日志时间 | Device/port | Lines | Logical safe | Packed | Session | 结果与顺序 |
|---|---|---|---|---|---|---|
| 15:37:32.117 | Dev1/port0 | 0:7 | `00000000` | `0x0` | `do-1-Dev1-port0` | safe write success → session running |
| 15:37:32.117 | Dev1/port1 | 0:3 | `0000` | `0x0` | `do-1-Dev1-port1` | safe write success → session running |
| 15:37:32.117 | Dev2/port0 | 0:7 | `00000000` | `0x0` | `do-1-Dev2-port0` | safe write success → session running |
| 15:37:32.127 | Dev2/port1 | 0:0 | `0` | `0x0` | `do-1-Dev2-port1` | safe write success → session running |

PTY 捕获对很长的 monotonic 数字字段发生了换行重绘，不能可靠逐字转录 `started_ns/actual_ns`；因此本证据保留稳定的外部日志时间、session identity 和明确的日志先后顺序，不伪造纳秒值。四个 task 在连接期间没有重复创建或意外停止日志。

## Self-check、startup zero 与 idle

- 15:37:32.407：Dev1、Dev2、COM6 self-check 全部 PASS，hardware worker 报 `ready=True`。
- 15:37:32.415–15:37:33.002：B setpoint command 自身响应及独立 poll readback 均成功，readback=`0.000`。
- 15:37:33.004–15:37:33.164：C=`0`，同样通过新 CR-framed transaction。
- 15:37:33.164–15:37:33.325：A=`0`，同样通过。
- 15:37:33.729：安全状态转为 `SAFE`，UI 已显示“设备已连接”。
- 全程未出现 serial timeout、partial、response mismatch 或 desync。

从 `SAFE/connected` 日志到 Global Stop 首个 A-zero command 约 `8.881 s`，不足批准规格要求的至少 10 秒，因此 zero-flow idle 只能记录为“约 8.9 秒稳定”，不能标为该门禁 PASS。用户确认期间无气流、无异常 valve/selector 动作、无异常声响。

## 真实 Global Stop

用户人工点击一次“全局停止”。

- 15:37:42.610–15:37:42.769：A=0 command response 与独立 poll readback 成功。
- selector safe receipt：`result=success`，measurement point=`daqmx_write_ack`。
- odor valve 1–20 close receipts：20/20 `result=success`，均为 `daqmx_write_ack`，无 uncertain receipt。
- 15:37:42.797–15:37:43.275：B→C→A=0 全部 command response 与独立 readback 成功。
- 15:37:43.350 shutdown event：`result=success`、`valves_closed=True`、`a_zero_confirmed=True`、`selector_safe_confirmed=True`、`recovery_required=False`。
- 没有 `-200846`，没有其他 DAQmx error，没有 serial desync，没有进入 `RECOVERY_REQUIRED`。
- UI 最终显示“设备未连接”；用户确认没有气流、阀门/selector 异常动作或异常声响。

所有 safe digital receipts 和 A/B/C zero 均出现在 shutdown success 之前；程序随后进入 worker shutdown。终端没有独立输出每个 DO task close/release timestamp，因此不能把“每个 task 的精确 release 时刻”写成直接日志证据。Global Stop success record、程序退出和最终无残留进程共同支持资源已释放，但该时间顺序仍应在下一次严格重跑中补充更直接的 audit evidence。

Global Stop success 到 App 关闭日志约 `7.661 s`，不足批准规格要求的至少 10 秒，所以只能确认这段时间未发生自动 reconnect，不能宣称完成 10 秒观察门禁。

## App shutdown 与最终只读确认

在用户最初报告 UI 为“设备未连接”后，Codex 曾向承载 App 的终端发送两次 `Ctrl+C`，Qt 回调分别打印 `KeyboardInterrupt`；它们没有立即终止 GUI。用户随后关闭窗口，worker shutdown 完成，但终端最终退出码为 `1`。这些 traceback 是本轮测试控制过程产生的中断记录，不是 DAQmx 或 Alicat transport 错误；不过因此“App 正常零退出”不能判为 PASS。

App 关闭后无残留 Python/OlfactoryPilot、probe、串口终端或 NI MAX 进程。随后使用生产 transaction 完成最终只读检查：

```text
A Poll: b'A +014.62 +028.62 +0.0000 +0.0000 +0.0000    Air\r'
A LSS:  b'A U\r'
B Poll: b'B +014.62 +028.72 +0.0000 +0.0000 +0.0000    Air\r'
B LSS:  b'B U\r'
C Poll: b'C +014.62 +028.49 +0.0000 +0.0000 +0.0000    Air\r'
C LSS:  b'C U\r'
```

- 最终 setpoint=`0/0/0 sccm`；mass flow=`0/0/0 sccm`；LSS=`U/U/U`。
- 6/6 transaction 成功，无 desync；COM6 再次正常关闭。

## 判定与后续 blocker

- NI first safe image：真实 4-port packed `0x0` 写入成功，且日志顺序均为 safe write → persistent running。
- NI persistent DO / Global Stop `-200846` 修复：**真实核心复测 PASS**。
- selector safe receipt：PASS；odor valve close receipts：20/20 PASS；最终 Alicat zero：PASS。
- 完整 C.3b-3：**仍阻断**。原因是本次 acquisition 来自人工“重新连接”而非 startup auto-connect，两个规定的 10 秒观察窗均未满足，缺少逐 task release 的直接时间日志，且 App 受测试控制 `KeyboardInterrupt` 影响以 code 1 退出。
- 下一次只需在新的成功 shutdown record 基线上严格重跑相同的 startup auto-connect → ≥10 s idle → 人工 Global Stop → ≥10 s no-auto-reconnect；仍不得供气、选择/验证气口或测试重新连接。
