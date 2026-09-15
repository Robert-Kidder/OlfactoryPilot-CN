# C.3b Alicat 只读串口事务与响应时延实机证据（2026-09-15）

## 范围与安全边界

- 被测代码：`f6fc5584ef1997867d803c26777f247977f8b956`（`fix(hil): 修复 Alicat 串口帧边界与响应归属`）
- 分支：`hil/c3b-commissioning`
- 串口：`COM6 @ 19200 baud, 8-N-1, xonxoff/rtscts/dsrdtr=false`
- 单帧接收 deadline：`alicat_timeout_s = 0.2 s`（provisional，未在本轮修改）
- 查询实现：生产 `app/services/alicat_serial.py::AlicatSerialSession`；仅调用 `poll()`、`query_firmware()`、`query_setpoint_source()`。
- 未启动 OlfactoryPilot Real App；未创建 NI task；未执行 setpoint、LSS mode 或其他设备配置/动作命令。

## Initial resynchronization

| Session | 打开时间（Asia/Shanghai） | initial sync 到首个 TX 的观测用时 | 配置 quiet requirement | bounded budget | 丢弃 stale bytes |
|---|---|---:|---:|---:|---:|
| 1 | 2026-09-15 15:25:03.619201+08:00 | 397.859 ms | 400.000 ms | 800.000 ms | 0 |
| 2（clean reopen） | 2026-09-15 15:25:05.307711+08:00 | 391.881 ms | 400.000 ms | 800.000 ms | 0 |

观测用时由调用侧 monotonic 时钟记录到首个 transaction TX；quiet 完成由生产 session 的 initial-sync contract 判定。两次均未观察到需排空的旧数据。

## 第一阶段：单次 correctness pass

严格顺序执行 `A Poll → A VE → A LSS → B Poll → B VE → B LSS → C Poll → C VE → C LSS`；每条完整 CR frame 读取并通过 command-aware validation 后才发送下一条。

| Unit | 查询 | 原始返回 | latency |
|---|---|---|---:|
| A | Poll | `b'A +014.62 +027.86 +0.0000 +0.0000 +0.0000    Air\r'` | 26.879 ms |
| A | VE | `b'A   10v14.0-R24 Feb 26 2024,13:06:19\r'` | 21.544 ms |
| A | LSS | `b'A U\r'` | 13.336 ms |
| B | Poll | `b'B +014.62 +027.89 +0.0000 +0.0000 +0.0000    Air\r'` | 26.755 ms |
| B | VE | `b'B   10v14.0-R24 Feb 26 2024,13:06:19\r'` | 21.557 ms |
| B | LSS | `b'B U\r'` | 13.200 ms |
| C | Poll | `b'C +014.62 +027.71 +0.0000 +0.0000 +0.0000    Air\r'` | 26.756 ms |
| C | VE | `b'C   10v14.0-R24 Feb 26 2024,13:06:19\r'` | 21.506 ms |
| C | LSS | `b'C U\r'` | 13.323 ms |

解析结果：A/B/C setpoint 均为 `0.0000` device unit（`0 sccm`）；mass flow 均为 `0.0000` device unit（`0 sccm`）；固件均为 `10v14.0-R24`；LSS 均为 `U`。

## 第二阶段：5-cycle latency sample

每个 cycle 仍按 A/B/C 各自 Poll、VE、LSS 严格串行，共 45 个只读 transaction。下表由 `AlicatFrame.tx_ns/rx_ns` 计算，单位为 ms。

| Unit | 查询 | samples | min | median | max | mean |
|---|---|---:|---:|---:|---:|---:|
| A | Poll | 5 | 26.752 | 26.795 | 26.827 | 26.793 |
| A | VE | 5 | 21.509 | 21.520 | 22.448 | 21.706 |
| A | LSS | 5 | 13.213 | 13.234 | 13.249 | 13.230 |
| B | Poll | 5 | 26.705 | 26.764 | 26.802 | 26.758 |
| B | VE | 5 | 21.441 | 21.463 | 21.495 | 21.471 |
| B | LSS | 5 | 13.200 | 13.297 | 13.458 | 13.313 |
| C | Poll | 5 | 26.791 | 26.817 | 26.849 | 26.823 |
| C | VE | 5 | 21.521 | 21.538 | 22.057 | 21.638 |
| C | LSS | 5 | 13.145 | 13.238 | 14.006 | 13.439 |

本次 5-sample/组合的最大值为 `26.849 ms`；全部 57 个 transaction 中观测到的最大值为 correctness pass 的 A Poll `26.879 ms`。该小样本只说明本次实验台、COM6/ATEN bridge 与 Windows 环境下未见 `0.2 s` 的明显余量风险，不能证明设备最大响应时间，也不据此修改生产 timeout。

## Clean close / reopen

Session 1 正常关闭并确认 `is_open=False`，随后只打开一次新 session。新 session 完成 bounded initial resynchronization 后严格执行：

| Unit | 查询 | 原始返回 | latency |
|---|---|---|---:|
| A | Poll | `b'A +014.62 +027.86 +0.0000 +0.0000 +0.0000    Air\r'` | 26.793 ms |
| A | VE | `b'A   10v14.0-R24 Feb 26 2024,13:06:19\r'` | 21.557 ms |
| A | LSS | `b'A U\r'` | 13.501 ms |

Session 2 正常关闭并确认 `is_open=False`；未打开第三次 session。

## 结果

- 总 transaction：57（correctness 9 + latency 45 + clean reopen 3）。
- timeout：0。
- partial CR frame：0。
- wrong Unit ID / wrong response type：0。
- response mismatch：0。
- desynchronized session：0。
- 跨命令响应错位：未观察到；未再次出现 `LSS timeout → VE 收到 A U`。
- clean reopen：通过。
- 最终状态：A setpoint/mass flow `0/0 sccm`；A firmware `10v14.0-R24`；A LSS `U`。B/C 在 session 1 最后采样中同样保持 `0/0 sccm`、`10v14.0-R24`、`U`。
- COM6：两个 handle 均正常关闭；收尾进程检查未发现 Python diagnostic、OlfactoryPilot、串口终端或 NI MAX 残留进程。
- NI：本轮未创建 task，未访问或写入 Dev1/Dev2。

结论：新的 CR-framed、one-command/one-response、command-aware transaction 在本轮真实只读样本中成立；串口 framing/response-attribution 的只读 preflight blocker 已解除。此前真实 Global Stop 失败记录保持不变；是否重新执行 C.3b-3 仍需单独人工批准。
