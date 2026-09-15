# C.3b-3 最终严格真实连接与 Global Stop 复测证据

> **结论：PASS。** 本次由 startup auto-connect 完成第一次接管；连接后零流量观察与 Global Stop 后无自动重连观察均超过 15 秒；用户人工点击一次 Global Stop、随后正常点击窗口 X；进程自然退出码为 0；最终 Alicat 只读回查保持 0/0/0 与 U/U/U。此结论仅覆盖 C.3b-3 零流量连接/停止生命周期，不代表非零供气、selector 两方向动作或任何气口验证完成。

## 基线、门禁与 preflight

- 日期：2026-09-15（Asia/Shanghai）；分支：`hil/c3b-commissioning`；工作区在启动前干净。
- 被测代码包含 `436922c744c028d2b4f18015e43f373b560190b7`（NI DO 生命周期）、`f6fc5584ef1997867d803c26777f247977f8b956`（Alicat CR-framed transaction）、`81a833afbe3ea2924d188ec9c0bf2641ce9f8875`（安全退出与 task release 审计）；此前 Alicat 只读 latency 证据为 `8516dd672bcfef16fc9ef7a53bcf88e3657af4fb`。
- 启动前 shutdown record：`result=success`、`valves_closed=true`、`a_zero_confirmed=true`、`selector_safe_confirmed=true`、`recovery_required=false`。未删除、编辑或忽略历史 record；当前成功记录没有阻止 startup auto-connect。
- 现场人工门禁：无受试者、无气味样品、出口畅通，操作者知道如何立即停止/断电。启动前未发现 OlfactoryPilot、probe、串口终端或 NI MAX 占用进程。
- `config/local_config.json`：`hal_mode=real`、`COM6 @ 19200`、A/B/C Unit ID `a/b/c`、NI `Dev1/Dev2`；HardwareProfile revision `50`。启用气口为 `2/4/6/8/12/14/16/18`，selector 为 `Dev2/P1.0`。20 路 valve target 与 selector 合计 21 个唯一受管输出；当前 profile 推导的安全物理电平均为 LOW。此项是配置/代码判断，不替代现场气口初始关闭验证。
- 启动前只用生产 `AlicatSerialSession` 严格串行执行 A Poll/LSS、B Poll/LSS、C Poll/LSS：6/6 完整 CR frame 与命令类型校验成功，timeout、partial、mismatch、desync 均为 0；查询后 COM6 正常关闭。

```text
A Poll: b'A +014.61 +030.36 +0.0000 +0.0000 +0.0000    Air\r'
A LSS:  b'A U\r'
B Poll: b'B +014.62 +030.51 +0.0000 +0.0000 +0.0000    Air\r'
B LSS:  b'B U\r'
C Poll: b'C +014.62 +030.16 +0.0000 +0.0000 +0.0000    Air\r'
C LSS:  b'C U\r'
```

启动前 setpoint=`0/0/0 sccm`，mass flow=`0/0/0 sccm`，LSS=`U/U/U`。预检后暂停并收到操作者明确批准，才启动正式 Real App。

## Startup auto-connect 与首次 NI 安全接管

- 实际启动命令：`python -m app.main --local-config config/local_config.json`；未加 simulation/no-worker。没有人工点击“重新连接”，没有第二次 connection acquisition。
- 16:43:55.392：Controller 启动 actuation owner 执行安全 DO acquisition。真实终端每个 port 只出现一条首次 safe-image 成功日志，随后才出现对应的 `DO session running`；所有 packed value 均为 `0x0`。

| Safe-image 成功日志 | Device/port | Lines | Logical safe states | Packed | Session | Safe write monotonic started/actual ns | Running 日志 |
|---|---|---|---|---|---|---|---|
| 16:43:55.533 | Dev1/port0 | 0:7 | `00000000` | `0x0` | `do-1-Dev1-port0` | `5580464491900` / `5580474873100` | 16:43:55.537 |
| 16:43:55.541 | Dev1/port1 | 0:3 | `0000` | `0x0` | `do-1-Dev1-port1` | `5580479633300` / `5580482021600` | 16:43:55.541，safe-image 日志在前 |
| 16:43:55.541 | Dev2/port0 | 0:7 | `00000000` | `0x0` | `do-1-Dev2-port0` | `5580482661800` / `5580483327000` | 16:43:55.542 |
| 16:43:55.545 | Dev2/port1 | 0:0 | `0` | `0x0` | `do-1-Dev2-port1` | `5580483893500` / PTY 换行重绘，末位未可靠转录 | 16:43:55.547 |

port1 的长 monotonic 字段在 PTY 捕获中发生换行重绘，因此没有猜测 `actual_ns`；该 port 的完整 safe-image success 日志顺序明确先于 running 日志。四个 session 在 connected 期间没有重复创建、提前停止或隐式 restart 日志。

## Self-check、B→C→A startup zero 与零流量 idle

- 16:43:55.743：Dev1、Dev2、COM6 自检 3/3 PASS；HardwareWorker 报 `ready=true`。
- B setpoint 0 command 与独立 poll readback 于 16:43:56.348 验证成功；C 于 16:43:56.507 成功；A 于 16:43:56.665 成功。三条 setpoint command 均先消费自身响应，再执行独立 readback；顺序为 B→C→A，读回均为 `0.000`。
- 16:43:56.667：zero flow event `result=success`；16:43:56.754：安全状态从 `DATA_STALE` 收敛到 `SAFE`（合法 idle 零流量）。启动 acquisition 来源仅为 startup auto-connect；用户没有人工 reconnect。日志中没有 Alicat timeout、partial、response mismatch 或 desync。
- 用户人工 Global Stop 的首个日志为 16:44:39.836。以较晚的 16:43:56.754 `SAFE` 日志作为保守零流量观察起点，至 Stop 开始为 **43.082 秒**，超过本轮 ≥15 秒门禁。此时未操作实验控件，未供气、调流、选口或释放气味。用户确认全程无气流、异常阀门/selector 动作或异常声响；没有 DAQmx/serial error 或自动第二次连接日志。

## 用户人工 Global Stop、精确回执与 task release

- 用户确认 16:44:39 的 Global Stop 由其人工点击一次；Codex 未点击任何 UI，也没有补救或 retry。
- A=0 command/独立 poll 于 16:44:39.995 成功；selector safe write receipt 为 `safe-stop-selector-1-1000002`，`result=success`，`measurement_point=daqmx_write_ack`，`actual_ns=5624939305200`。
- 气味阀 1–20 的 `safe-stop-odor-close-*` exact receipts **20/20 `result=success`**，均为 `daqmx_write_ack`，无 uncertain/stale receipt。最后一个 valve20 close receipt `actual_ns=5624944472500`。按 port，最后一个安全数字写入分别为：Dev1/port0 valve8 `5624941822900`；Dev1/port1 valve12 `5624942752200`；Dev2/port0 valve20 `5624944472500`；Dev2/port1 selector `5624939305200`。
- 随后 B=0 于 16:44:40.182、C=0 于 16:44:40.342、A=0 于 16:44:40.502 均完成 command response 与独立 readback，均为 `0.000`。
- 4 个 port 的 `DO session release` 审计均为 `result=success`、`reason=owner_handoff`；每个 port 的 close start 都晚于该 port 最终安全写入的 `actual_ns`：

| Session | Device/port | 最终 safe write actual ns | close_started_ns | close_actual_ns | Release |
|---|---|---:|---:|---:|---|
| `do-1-Dev1-port0` | Dev1/port0 | `5624941822900` | `5625444675100` | `5625445223600` | success |
| `do-1-Dev1-port1` | Dev1/port1 | `5624942752200` | `5625445414800` | `5625445752000` | success |
| `do-1-Dev2-port0` | Dev2/port0 | `5624944472500` | `5625445891900` | `5625446203400` | success |
| `do-1-Dev2-port1` | Dev2/port1 | `5624939305200` | `5625446335800` | `5625446692300` | success |

- 16:44:40.577：shutdown event `source=stop`、`result=success`、`valves_closed=true`、`a_zero_confirmed=true`、`selector_safe_confirmed=true`、`recovery_required=false`。未出现 NI-DAQmx `-200846`、其他 DAQmx error、Alicat desync 或 `RECOVERY_REQUIRED`。
- 用户确认 Stop 后 UI 为“设备未连接 + 重新连接”，没有点击“重新连接”；无不必要的技术错误通知。

## ≥15 秒无自动重连、正常窗口关闭与最终只读确认

- 从 16:44:40.577 shutdown success 到 16:45:02.093 正常 worker teardown 日志为 **21.516 秒**。这段时间未出现第二次 connection acquisition、NI safe-image、COM6 acquisition 或自动重连日志；用户没有点击“重新连接”。
- 用户确认随后由其点击窗口右上角 X 正常关闭。Codex 未向 GUI 终端发送 Ctrl+C、Ctrl+Break、terminate 或 kill；进程自然退出 `exit code=0`，无 KeyboardInterrupt 或 traceback。终端最后出现一条 Python GC `ResourceWarning: 27 uncollectable objects at shutdown`；这是非硬件资源警告，已如实留存，不构成 DAQmx/serial error，也未影响正常退出码。退出后未发现残留 OlfactoryPilot/Python probe/NI MAX 进程；COM6 handle 与四个 DO task 均已释放。
- App 完全退出后另开一次生产 `AlicatSerialSession`，严格串行只读 Poll/LSS A、B、C：6/6 transaction 成功，timeout、partial、mismatch、desync 均为 0；完成后 COM6 再次关闭，`is_open=false`。

```text
A Poll: b'A +014.62 +030.42 +0.0000 +0.0000 +0.0000    Air\r'
A LSS:  b'A U\r'
B Poll: b'B +014.61 +030.57 +0.0000 +0.0000 +0.0000    Air\r'
B LSS:  b'B U\r'
C Poll: b'C +014.61 +030.22 +0.0000 +0.0000 +0.0000    Air\r'
C LSS:  b'C U\r'
```

最终 A/B/C setpoint=`0/0/0 sccm`，mass flow=`0/0/0 sccm`，LSS=`U/U/U`。

## 验收边界与历史证据

本次 C.3b-3 严格零流量启动/停止生命周期为 **PASS**：startup auto-connect、4/4 first safe images、persistent sessions、3/3 self-check、B→C→A zero、≥15 秒 zero-flow idle、Global Stop selector 与 20/20 valve receipts、4/4 release、≥15 秒 no-auto-reconnect、用户 X 正常关闭、自然 code 0、最终只读 Alicat 0/U 及资源释放均具备证据。

历史结果保持原样：`c-3b-first-real-app-connect-2026-09-14.md` 记录第一次 Global Stop 因 `-200846` **FAIL**；`c-3b-first-real-app-connect-retest-2026-09-15.md` 记录人工重新连接后的 Global Stop 核心 **PASS**、但完整 C.3b-3 **BLOCKED**。本文件是新的独立最终复测，不改写前两次执行事实。

尚未授权/验证：非零供气、selector 两方向实测、单气口及 8 路气口验证、Manual release、timing benchmark、Auto/Protocol/Cleaning/Maintenance。此处立即停止，不进入下一阶段。
