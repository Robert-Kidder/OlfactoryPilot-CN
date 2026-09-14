# C.3b-3 第一次真实 App 自动连接与零流量安全停止证据（阻断）

> **结论：首次自动连接通过；第一次真实 Global Stop 失败，C.3b-3 整体阻断。** 应用进入 `RECOVERY_REQUIRED`，无法确认 selector 和气味阀关闭。操作者随后按门禁关闭设备电源，且未观察到气流、阀门/selector 动作或异常声响。不得据此继续重新连接、供气或气口验证。

## 范围与现场条件

- 日期：2026-09-14（Asia/Shanghai）
- 分支：`hil/c3b-commissioning`
- 启动命令：`python -m app.main --local-config config/local_config.json`
- 无受试者、无气味样品、出口畅通；操作者可立即停止/断电。
- 本轮没有开始供气、修改 Manual 参数、选择或验证气口、运行额外 HIL script、重新连接或 power-cycle。

## 启动前只读 preflight

命令：`python scripts/probe_alicat.py --port COM6 --baud 19200 --ids a,b,c`（未使用 `--set`）。

```text
A +014.69 +025.46 +0.0000 +0.0000 +0.0000    Air
B +014.69 +025.37 +0.0000 +0.0000 +0.0000    Air
C +014.69 +025.37 +0.0000 +0.0000 +0.0000    Air
```

按当前项目字段约定解析：A/B/C setpoint=`0/0/0 sccm`，mass flow=`0/0/0 sccm`。

只发送无参数查询 `aLSS\r`、`bLSS\r`、`cLSS\r`：

```text
A raw=b'A U\r'
B raw=b'B U\r'
C raw=b'C U\r'
COM6_CLOSED=True
```

- LSS=`U/U/U`。
- local config：`hal_mode=real`、`COM6 @ 19200`、Unit ID=`a/b/c`、NI=`Dev1/Dev2`。
- 当前生效 HardwareProfile 与此前 C.3b 审计相同：启用气口 2/4/6/8/12/14/16/18；selector=`Dev2/P1.0`、安全位置=`COMPENSATION`。
- 当前配置推导的 21 个受管输出安全电平均为 LOW。此项是代码/配置判断，不是本轮真实写入回执。

## 第一次真实 startup auto-connect

| 时间 | 证据 |
|---|---|
| 17:48:27.581 | Controller 开始 safe DO acquisition。窗口显示后只观察到一次 startup connection transaction。 |
| 17:48:28.053 | Dev1、Dev2 和 COM6 self-check 全部 PASS。 |
| 17:48:28.056–17:48:29.097 | 按 B→C→A 顺序写入 0；三台均在 attempt 1 回读 `0.0000`。 |
| 17:48:29.630 后 | 用户确认 UI 显示“设备已连接”；超过 10 秒的 zero-flow idle 内没有新增错误或自动第二次连接。 |

连接阶段结果：

- startup auto-connect exactly once：通过。
- self-check：通过。
- B/C/A zero 与回读：通过。
- UI connected：用户现场确认通过。
- zero-flow idle：通过；用户未观察到气流、异常阀门/selector 动作或异常声响。
- DAQ acquisition 阶段未出现错误并进入 connected；但终端没有输出可独立核验的首次 packed NI image 明细。因此“配置期望为完整 LOW safe image”有静态依据，“真实首次 packed image 的明确运行回执”仍是证据缺口。

## 第一次真实 Global Stop

用户在 zero-flow idle 稳定后人工点击一次“全局停止”。没有点击“重新连接”。

流量收口：

- 17:50:21.099–17:50:21.440：A=0，attempt 1 回读 `0.0000`。
- 17:50:21.674–17:50:22.674：B→C→A=0，三台均在 attempt 1 回读 `0.0000`。

NI 安全收口失败：

```text
result=recovery_required
valves_closed=False
heaters_off=True
selector_safe_confirmed=False
a_zero_confirmed=True
Status Code: -200846
Write cannot be performed when the auto start input to DAQmx Write is false,
task is not running, and timing for the task is not configured or Timing Type is set to On Demand.
Task Name: _unnamedTask<3>
```

- selector safe receipt：`uncertain`。
- 气味阀 1–20 close receipts：均为 `uncertain`。
- 应用正确地没有报告普通成功，而是进入 `RECOVERY_REQUIRED`。
- 由于阀门和 selector 安全状态无法由回执确认，本次 Global Stop 判定 **FAIL**。

## 现场异常处置与结束状态

- 收到失败日志后立即停止测试；没有 retry、第二次 Connect、额外 Global Stop 或其他控制命令。
- 操作者关闭设备电源，并确认断电前后均未观察到气流、阀门/selector 动作或异常声响。
- 硬件断电后，OlfactoryPilot 进程被终止；终端会话结束，不再运行 App。
- 进程终止释放了其 COM/NI 进程句柄；由于不是应用正常 shutdown receipt，不能将其记为 graceful resource-release PASS。
- 设备已断电，因此没有重新打开 COM6 执行最终 poll/LSS；最终串口状态不能以本轮读回确认。

## 结论与 blocker

- **首次真实自动连接：PASS（但首次 packed NI image 缺少显式运行回执）。**
- **第一次真实 Global Stop：FAIL。**
- **C.3b-3：BLOCKED。**
- 重新进行任何真实连接前，必须离线查明并修复 Global Stop 对已停止 on-demand NI task 写 safe image 时触发 `-200846` 的生命周期问题，并补充回归测试。
- 修复后需重新获得人工授权；不得自动重试，不得继续供气、气口验证或 timing benchmark。
