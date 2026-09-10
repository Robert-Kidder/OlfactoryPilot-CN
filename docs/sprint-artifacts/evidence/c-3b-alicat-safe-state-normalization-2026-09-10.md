# C.3b-2C Alicat 安全初始状态规范化证据（2026-09-10）

> **结果：成功。** 仅对 Alicat A/B/C 执行获批的 setpoint 清零和 Setpoint Source `S → U`；没有启动 OlfactoryPilot real app，没有创建或写入 NI task，没有切换 selector，没有开关任何气味阀，也没有执行其他硬件动作。

## 现场与协议门禁

- 现场无受试者、无气味样品，待测试出口畅通，操作者可以立即停止/断电。
- 分支为 `hil/c3b-commissioning`；开始前仅有本轮 C.3b spec/evidence 变更，均已保留。
- `config/local_config.json` 为 `COM6 @ 19200`，A/B/C 地址为 `a/b/c`；开始前未发现 OlfactoryPilot、Python probe 或常见 serial terminal 占用进程。
- A/B/C firmware 均为 `10v14.0-R24`，满足 LSS 的 `10v05` 门槛。
- Alicat 官方 [Serial Communication Tutorial](https://www.alicat.com/support/serial-communication-tutorial/) 规定 setpoint 命令为 `<unit>S<floating point><CR>`；官方 [Serial Communications Primer](https://documents.alicat.com/Alicat-Serial-Primer.pdf) 规定无参数 `<unit>LSS<CR>` 查询 source，`<unit>LSS U<CR>` 设置 unsaved serial。
- 项目单位换算为设备 NLPM × `1000` = sccm，因此 `S0.000` 表示 `0 sccm`。
- 串口执行器不做重试。任一 timeout、ID mismatch、非 Air、状态码、setpoint 偏差超过 `0.5 sccm` 或 mass flow 绝对值超过 `5 sccm` 即终止，不继续下一台。

## 修改前只读确认

### Poll 原始返回

```text
A +014.73 +044.01 +0.0016 +0.0014 +1.5000    Air
B +014.73 +044.19 +0.0006 +0.0006 +1.5000    Air
C +014.73 +043.08 +0.0006 +0.0006 +0.5000    Air
```

| 设备 | setpoint | mass flow | 判断 |
|---|---:|---:|---|
| A | `1500 sccm` | `1.4 sccm` | ID/Gas 正确，无状态码，安全门禁通过 |
| B | `1500 sccm` | `0.6 sccm` | ID/Gas 正确，无状态码，安全门禁通过 |
| C | `500 sccm` | `0.6 sccm` | ID/Gas 正确，无状态码，安全门禁通过 |

### LSS 原始返回

```text
A S
B S
C S
```

三台修改前均为 saved Serial/Front Panel 模式 `S`。

## 逐台清零

### Alicat A

- 发送：`aS0.000\r`；语义：仅将 A setpoint 设置为 `0 sccm`。
- 写命令返回：

```text
A +014.73 +044.03 +0.0018 +0.0016 +0.0000    Air
```

- 写后立即 poll：

```text
A +014.73 +044.04 +0.0018 +0.0016 +0.0000    Air
```

- 结果：ID=A，setpoint=`0`，mass flow=`1.6 sccm`，通过；继续 B。

### Alicat B

- 发送：`bS0.000\r`；语义：仅将 B setpoint 设置为 `0 sccm`。
- 写命令返回：

```text
B +014.73 +044.22 +0.0008 +0.0008 +0.0000    Air
```

- 写后立即 poll：

```text
B +014.73 +044.23 +0.0008 +0.0006 +0.0000    Air
```

- 结果：ID=B，setpoint=`0`，mass flow=`0.6 sccm`，通过；继续 C。

### Alicat C

- 发送：`cS0.000\r`；语义：仅将 C setpoint 设置为 `0 sccm`。
- 写命令返回：

```text
C +014.73 +043.12 +0.0008 +0.0008 +0.0000    Air
```

- 写后立即 poll：

```text
C +014.73 +043.13 +0.0006 +0.0006 +0.0000    Air
```

- 结果：ID=C，setpoint=`0`，mass flow=`0.6 sccm`，通过。

## 清零后整体复查

```text
A +014.73 +044.06 +0.0000 +0.0000 +0.0000    Air
B +014.73 +044.25 +0.0000 +0.0000 +0.0000    Air
C +014.73 +043.14 +0.0000 +0.0000 +0.0000    Air
```

A/B/C setpoint=`0/0/0 sccm`，mass flow=`0/0/0 sccm`，允许进入 LSS 修改。

## 逐台从 S 改为 U

### Alicat A

- 发送：`aLSS U\r`；语义：仅将 A Setpoint Source 设置为 unsaved Serial/Front Panel。
- 写命令返回：`A U`
- 无参数 LSS 读回：`A U`
- 随后 poll：

```text
A +014.73 +044.08 +0.0000 +0.0000 +0.0000    Air
```

- 结果：`S → U` 成功，setpoint=`0`、mass flow=`0 sccm`；继续 B。

### Alicat B

- 发送：`bLSS U\r`；语义：仅将 B Setpoint Source 设置为 unsaved Serial/Front Panel。
- 写命令返回：`B U`
- 无参数 LSS 读回：`B U`
- 随后 poll：

```text
B +014.73 +044.28 +0.0000 +0.0000 +0.0000    Air
```

- 结果：`S → U` 成功，setpoint=`0`、mass flow=`0 sccm`；继续 C。

### Alicat C

- 发送：`cLSS U\r`；语义：仅将 C Setpoint Source 设置为 unsaved Serial/Front Panel。
- 写命令返回：`C U`
- 无参数 LSS 读回：`C U`
- 随后 poll：

```text
C +014.73 +043.17 +0.0000 +0.0000 +0.0000    Air
```

- 结果：`S → U` 成功，setpoint=`0`、mass flow=`0 sccm`。

## 最终完整确认

### LSS 原始返回

```text
A U
B U
C U
```

### Poll 原始返回

```text
A +014.73 +044.13 +0.0000 +0.0000 +0.0000    Air
B +014.73 +044.32 +0.0000 +0.0000 +0.0000    Air
C +014.73 +043.20 +0.0000 +0.0000 +0.0000    Air
```

最终 A/B/C mode=`U/U/U`，setpoint=`0/0/0 sccm`，mass flow=`0/0/0 sccm`，Gas 均为 Air，无状态码。

## 异常、观察与资源释放

- 没有 timeout、空响应、ASCII 乱码、ID mismatch、setpoint mismatch、状态码或异常实际流量；每条写命令只发送一次。
- 本轮初始温度字段约 `43–44 °C`，高于上一轮约 `29 °C`；压力仍为 `14.73`、Gas 仍为 Air，温度在本轮内缓慢且稳定变化，没有伴随流量、setpoint 或状态码异常，记录为设备通电升温观察。
- 未执行 power cycle，因此尚未实测 U 模式重新上电后 setpoint 是否归零。
- 执行器输出 `NORMALIZATION_SUCCESS` 和 `PORT_CLOSED`，退出码 `0`。退出后未发现 OlfactoryPilot、Python probe 或常见 serial terminal 进程继续运行；没有重新打开 COM6 做额外验证。

## 尚未解除的 commissioning 门禁

- 气味阀 1–20 的真实初始关闭状态尚未确认，因此 checklist D 仍不能整体完成。
- 真实 App Connect 仍未授权；其创建/启动 NI DO task 与自检后 A/B/C 写零副作用仍须单独讨论和批准。
- U 模式 power-cycle 行为尚未实机验证。
