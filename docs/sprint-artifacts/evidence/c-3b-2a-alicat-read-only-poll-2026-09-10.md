# C.3b-2A Alicat 只读串口查询证据（2026-09-10）

> **范围：已完成只读查询，未执行任何硬件状态修改。** 本轮没有启动 OlfactoryPilot real app、没有创建 NI task、没有写 NI 数字输出、没有切换 selector、没有开关气口，也没有发送 Alicat setpoint 或其他配置命令。

## 人工现场事实

| 项目 | 现场结果 | 当前结论 |
|---|---|---|
| 现场安全准备 | 无受试者；无气味样品；待测试出口畅通；操作者知道如何立即停止/断电 | 通过 |
| NI Dev1 | NI USB-6001；NI MAX 显示序列号 `0214581E` | `0x0214581E = 34887710`，与历史一致，现场确认 |
| NI Dev2 | NI USB-6001；NI MAX 显示序列号 `02145875` | `0x02145875 = 34887797`，与历史一致，现场确认 |
| COM / Baud | `COM6` / `19200` | 与 `config/local_config.json` 一致，现场确认 |
| Alicat A | 面板 Unit ID=`A`、Baud=`19200`、Setpoint Source=`Serial/Front Panel` | 软件地址 `a` 按协议大小写不敏感规则视为同一地址；三项现场确认 |
| Alicat B | 面板不可直接查看，不拆卸 | 本轮 poll 确认地址 `b` 可在 19200 通信；Setpoint Source 未确认 |
| Alicat C | 面板不可直接查看，不拆卸 | 本轮 poll 确认地址 `c` 可在 19200 通信；Setpoint Source 未确认 |
| 真实 App Connect | 未授权 | 不得执行 |

## 查询前安全门

- `config/local_config.json`：`serial_port=COM6`、`baud_rate=19200`、A/B/C 地址为 `a/b/c`。
- 进程检查未发现 OlfactoryPilot 主程序实例。
- `scripts/probe_alicat.py` 静态审计：不传 `--set` 时只对各地址发送 `<id>\r` live-data poll；只有显式 `--set` 才构造 `<id>s<value>\r`。本轮没有传 `--set`。

## 唯一真实串口命令

```text
python scripts/probe_alicat.py --port COM6 --baud 19200 --ids a,b,c
```

退出码：`0`。

## 原始设备返回

```text
a: A +014.73 +029.51 +0.0006 +0.0006 +1.5000    Air
b: B +014.73 +029.29 +0.0002 +0.0002 +1.5000    Air
c: C +014.73 +029.06 +0.0002 +0.0002 +0.5000    Air
```

以上冒号后的内容为设备返回的完整 ASCII 单行；终端只对脚本的中文包装文字出现代码页乱码，设备返回本身没有乱码或替换字符。

## 解析与判断

项目当前解析约定为：第 4 个数值字段是 mass flow，第 5 个数值字段是 setpoint，设备单位乘 `1000` 转为 sccm。

| 查询对象 | 返回首 ID | 通信 | mass flow | setpoint | Gas |
|---|---:|---|---:|---:|---|
| a | `A` | 成功 | `0.6 sccm` | `1500 sccm` | Air |
| b | `B` | 成功 | `0.2 sccm` | `1500 sccm` | Air |
| c | `C` | 成功 | `0.2 sccm` | `500 sccm` | Air |

- A/B/C 均有响应，首 ID 与查询对象按大小写不敏感比较一致。
- 没有 timeout；设备 ASCII 返回没有乱码。
- 未见两个查询地址返回同一个 Unit ID 的迹象。
- 三台当前 mass flow 均接近零，但三台 setpoint 均为非零。这与真实动作前期望的安全初值不一致，是 blocker；本轮依照禁令没有发送 `0` 或其他恢复命令。
- B/C 的正常响应只能证明串口设备存在、地址可寻址且 19200 通信正常，不能证明其 Setpoint Source 为 `Serial/Front Panel`。

## 资源释放

探针以 `with serial.Serial(...)` 管理端口；命令正常退出后已执行关闭，且退出后未发现遗留 Python 进程。没有再次打开 COM6 验证，以免超出本轮唯一获准的串口命令。

## 下一步 blocker

1. 必须由人工决定如何在新的明确授权下处理 A/B/C 非零 setpoint；本证据不授权写 0。
2. 本文件形成时 B/C Setpoint Source 尚未确认；后续已通过官方协议支持的无参数 `LSS` 只读查询确认 A/B/C 均为 `S`，见 [VE/LSS 只读查询证据](c-3b-alicat-ve-lss-read-only-2026-09-10.md)。
3. 真实 App Connect 仍未授权；它会启动 NI DO task，并在自检成功后写入 A/B/C=0。

## 后续状态

2026-09-10 的 C.3b-2C 已在新的明确写操作授权下将 A/B/C setpoint 逐台清零，并将 LSS 从 `S/S/S` 改为 `U/U/U`；详见 [安全初始状态规范化证据](c-3b-alicat-safe-state-normalization-2026-09-10.md)。本文件保留 2A 查询当时的原始状态，不回写覆盖历史值。
