# C.3b Alicat VE/LSS 只读查询证据（2026-09-10）

> **范围：仅查询固件版本与当前 Setpoint Source。** 本轮没有启动 OlfactoryPilot、没有创建或写入 NI task、没有切换 selector、没有开关气口，也没有修改 Alicat setpoint、Unit ID、baud、gas、tare、valve hold、streaming mode 或 Setpoint Source。

## 协议门禁

- Alicat 官方 [Serial Communication Tutorial](https://www.alicat.com/support/serial-communication-tutorial/) 将 `<unit>VE<CR>` 列为当前固件版本查询。
- Alicat 官方 [Serial Communications Primer](https://documents.alicat.com/Alicat-Serial-Primer.pdf) 规定 Setpoint Source 命令从 firmware `10v05` 起支持；无参数 `<unit>LSS<CR>` 查询当前模式，带 mode 的命令可修改配置。
- 本轮先只发送 `aVE\r`、`bVE\r`、`cVE\r`。三台均达到门槛后，才另行只发送无参数 `aLSS\r`、`bLSS\r`、`cLSS\r`。

## Firmware Version 原始返回

```text
A   10v14.0-R24 Feb 26 2024,13:06:19
B   10v14.0-R24 Feb 26 2024,13:06:19
C   10v14.0-R24 Feb 26 2024,13:06:19
```

串口原始字节分别为：

```text
b'A   10v14.0-R24 Feb 26 2024,13:06:19\r'
b'B   10v14.0-R24 Feb 26 2024,13:06:19\r'
b'C   10v14.0-R24 Feb 26 2024,13:06:19\r'
```

三台均为 `10v14.0-R24`，满足 `>= 10v05`，因此允许执行无参数 LSS 查询。

## Setpoint Source 原始返回

```text
A S
B S
C S
```

串口原始字节分别为：

```text
b'A S\r'
b'B S\r'
b'C S\r'
```

## 解释

| Alicat | Firmware | LSS raw response | Mode | 含义 |
|---|---|---|---|---|
| A | `10v14.0-R24` | `A S` | `S` | Setpoint Source 为 Serial/Front Panel；setpoint 变更会保存并可在重新上电后恢复 |
| B | `10v14.0-R24` | `B S` | `S` | Setpoint Source 为 Serial/Front Panel；setpoint 变更会保存并可在重新上电后恢复 |
| C | `10v14.0-R24` | `C S` | `S` | Setpoint Source 为 Serial/Front Panel；setpoint 变更会保存并可在重新上电后恢复 |

- 返回首 ID 分别为 A/B/C，与查询对象一致；没有 timeout、空响应或 ASCII 乱码。
- 三台均不是 `A`（Analog）模式，因此没有“串口 setpoint 不是有效控制来源”的 Setpoint Source blocker。
- 上一轮只读 poll 的 A/B/C setpoint=`1500/1500/500 sccm` 与 `S` 模式的保存/上电恢复行为相容，可以部分解释为什么设备在本轮开始时仍显示非零 setpoint；但本次查询不能证明这些值的来源、写入时间或最后写入者。
- 本轮没有把任何设备从 `S` 改成 `U`，也没有发送 setpoint 清零命令。是否统一改为 `U` 属于后续真实配置变更，必须另行人工批准。

## COM6 释放

VE 和 LSS 两个只读查询会话均由 `with serial.Serial(...)` 管理并正常输出 `PORT_CLOSED`；命令退出后未发现 OlfactoryPilot、Python probe 或常见 serial terminal 进程继续运行。未为“验证关闭”而再次打开 COM6。

## 当前结论

- A/B/C 的固件和 Setpoint Source 已有官方协议支持的只读证据。
- 已具备讨论 App Connect 的设备身份/通信/Setpoint Source 条件，但仍不具备直接执行条件：A/B/C 非零 setpoint 的安全处置、App Connect 的 NI DO task 启动及自动写零副作用仍需新的明确授权。

## 后续状态

2026-09-10 的 C.3b-2C 已在新的明确写操作授权下将 A/B/C setpoint 清零，并把 Setpoint Source 从 `S/S/S` 改为 `U/U/U`；详见 [安全初始状态规范化证据](c-3b-alicat-safe-state-normalization-2026-09-10.md)。本文件继续保留修改前的只读事实。
