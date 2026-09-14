# C.3b LSS 只读响应错位诊断证据（2026-09-14）

> **结论：本次 power-cycle 后的 Alicat 只读 preflight 因响应边界与归属无法确认而阻断。** A/B/C live-data poll 均确认 setpoint 与 mass flow 为 0，但三台无参数 LSS 查询均 timeout；随后发送 `aVE\r` 却收到 LSS 形态的 `A U\r`，构成 delayed/stale response 错归到下一条命令的强证据。Real App 未启动，NI task 未创建，本轮没有发送任何硬件写命令。

## 范围与安全门禁

- 分支：`hil/c3b-commissioning`
- 仅允许读取 A/B/C live data、无参数 LSS，并在受控诊断中发送固件版本查询 `VE`。
- 未发送 setpoint、带 mode 的 LSS、tare、gas、Unit ID、baud、streaming 或 valve-hold 命令。
- 未启动 OlfactoryPilot Real App，未创建或写入 NI task，未进行 selector/气味阀动作。

## Power-cycle 后有效 live-data poll

实际查询命令均以 CR 结束；原始响应为：

```text
A +014.70 +024.92 +0.0000 +0.0000 +0.0000    Air
B +014.70 +024.83 +0.0000 +0.0000 +0.0000    Air
C +014.70 +024.84 +0.0000 +0.0000 +0.0000    Air
```

按当前项目字段约定解析：

| 设备 | setpoint | mass flow |
|---|---:|---:|
| A | 0 sccm | 0 sccm |
| B | 0 sccm | 0 sccm |
| C | 0 sccm | 0 sccm |

这一结果与 U 模式 power-cycle 后不保存 setpoint 的行为相容，但不能替代本轮 LSS 的直接查询证据。

## LSS timeout

历史成功和本次失败最终写入串口的 TX bytes 完全一致：

```text
b'aLSS\r'
b'bLSS\r'
b'cLSS\r'
```

本次原始返回：

```text
A LSS raw=b''
B LSS raw=b''
C LSS raw=b''
```

- 串口参数为 COM6、19200 baud、8 data bits、no parity、1 stop bit。
- `xonxoff=False`、`rtscts=False`、`dsrdtr=False`。
- 本次 probe 与正式 RealHAL 均使用 `readline()`；pyserial 默认等待 LF，而 Alicat ASCII 单行响应以 CR 结束。
- timeout 不能解释为 A/S/U 中的任何 mode。因此 power-cycle 后的 LSS 仍未直接确认。

## VE 控制实验与响应错位

在新的独占只读诊断会话中，读取改为等待 CR，并且每条命令完成后才允许下一条。实际发送：

```text
TX=b'aVE\r'
RX=b'A U\r'
```

`A U` 不是合法的 VE firmware response，而是 LSS Setpoint Source response 形态。它既不能作为 A 的 firmware 返回，也不能作为本轮 A LSS transaction 的直接证据。A VE 门禁失败后立即停止，因此没有发送 B/C VE，也没有在该诊断会话中重新发送 A/B/C LSS。

此现象说明问题不只是 LF terminator 导致等待超时：迟到的完整 CR frame 可以在下一条命令后被读取，并被错误归属到新请求。当前正式代码还存在 setpoint 写入后不消费命令响应、随后再 poll 的相同风险。

## 资源收尾与当前 blocker

- 诊断 serial handle 已关闭，COM6 已释放；未为确认关闭再次打开串口。
- Real App 未启动，Dev1/Dev2 未接管，没有任何硬件写操作。
- 历史 2026-09-10 的 `U/U/U` 规范化证据保持有效，但本轮 power-cycle 后尚无可靠 LSS direct readback。
- 在正式 runtime 与 probe 统一为 CR-framed、one-command/one-response、command-aware validation 的 transaction contract，并完成离线验证前，不得重新进入真实 C.3b-3。

