# Story 4.5 HIL 现场准备单

## 现在的状态

设备已通电连接，只读检查曾确认 Dev1/Dev2、COM6，A/B/C setpoint 和质量流量均已清零。新的 live 入口仍在离线开发/复审阶段。**当前保持上游 Air 关闭，不运行任何 HIL live 命令；特别不得运行旧 benchmark。**

离线演练入口只有 mock 模式，没有 `--live` 参数：

```powershell
python scripts/hil_story45_safe_stop.py --scenario all --output-root <仓库外的空证据目录>
```

这条命令只运行 fake owner 和生产 `ShutdownService`/`SafeStopPlan` 的软件逻辑。全部场景满足 oracle 才返回 `0`；任一安全断言不满足就返回非零。输出中的 mock receipt、selector 模拟观察均不是机械阀、DAQmx 或真实气路证据。

## 候选现场参数（只核对，不执行）

| 项目 | 候选值 |
|---|---|
| 串口 / 波特率 | `COM6` / `19200` |
| Alicat 地址 | A=`a`、B=`b`、C=`c` |
| 流量 | `sccm`，字段 `mass_flow`；setpoint/readback scale=`0.001`/`1000.0` |
| Alicat timeout / 容差 | `0.2 s` / `0.05`；readback 延迟 `0.1 s`，最多 3 次 |
| 正常场景 | Air；A=`2500 sccm`、B=C=`0`；仅本次诊断例外，不修改生产默认 `1500 sccm` 上限 |
| NI | `Dev1`、`Dev2`；selector=`Dev2/P1.0` |
| selector | LOW=`compensation`（安全路线），HIGH=`odor` |
| 气味阀 | 1–20，关闭电平 LOW；不得把 selector 当第 21 阀 |
| timeout | DO `100 ms`；selector/气味阀紧急动作 `500 ms`；A/全流量/owner shutdown `2000 ms` |

这些只是候选值。开机前必须与候选配置、设备铭牌和现场连接重新核对；不一致就停止，不能临场猜值。

## 以后收到“可以开机”通知前

- [ ] 候选 commit 已固定，工作树状态已记录。
- [ ] 离线场景全部通过，证据目录和哈希完整。
- [ ] 真实硬件专用入口已经另行审查；不得用本离线脚本代替。
- [ ] 操作者能直接关闭上游气源，并能观察 A/B/C 与 selector 两个出口。
- [ ] 现场只有干净 Air，无气味材料、容器或受试者。
- [ ] 每一个 NI/Alicat 写入都有独立编号，且自动收尾写入已逐项预授权。

以上任一项未满足：机器保持关闭并停止。

## 未来现场逐步授权顺序

只有获得新的明确授权后才进入本节。执行者先生成固定候选、场景、全部逐项写入和自动收尾的 manifest；操作者一次确认其 SHA-256/token 后，broker 按顺序逐条核销。目标、值、顺序、次数任一不符即在到达 HAL 前拒绝。

1. 只读 preflight：确认设备身份、串口地址、压力范围和现场 Air 条件；不写硬件。
2. 现场没有独立阀位/压力指示，因此电子 ack 不能被写成 selector 机械证据。本次已批准的受限诊断只在洁净 Air、无材料/受试者、A=`2500 sccm`、连续范围 `2250..2750 sccm` 和自动清零条件下进行；20 秒后必须另行记录阀 2 的实际气流观察，记录前 normal 结果保持 pending。
3. 场景开始前逐项预授权自动收尾：A=0、B=0、C=0、selector 写入 compensation、气味阀 1–20 LOW。selector 写入仍受“A=0 匹配 receipt 先行”门禁；未授权完整收尾就不启动场景。
4. 正常停止场景逐项授权：代表气味阀、selector 气味路线、A 非零流量；随后触发全局停止。
5. 现场核对严格顺序：fence → A=0 匹配 receipt → selector 补偿路线 → 气味阀 1–20 关闭 → A/B/C=0 → owner handoff。
6. fault 场景从物理 A/B/C=0 开始。receipt 失败、stale、late 或 selector 不确定时，不得宣称安全完成，也不得自动反复切 selector。

执行者必须在一次授权前完整展示不可变 manifest：每项写入编号、阶段、设备/目标、请求值、是否补偿写入、是否可选，以及完整自动收尾。操作者只确认该 manifest 的 SHA-256/token；broker 逐项核销，但不再要求操作者在 20 秒安全窗口内逐条回复。任何未列出、重复、乱序或参数不同的写入都在 HAL 前拒绝。

## 自动收尾与停止条件

未来真实运行的自动收尾必须已经预授权，并在 `finally` 中继续执行可安全完成的动作：fence 新动作、A 清零门禁、关闭气味阀、A/B/C 清零、maintenance/DO/lease/AI/serial handoff、保存证据。

出现以下任一情况立即进入 `RECOVERY_REQUIRED`：A 清零 receipt 无效或超时、selector receipt 无效或不确定、最终 A/B/C/气味阀证据不完整、任一 owner 未交还。selector 不确定时不自动重试；DO owner 卡住时不跨线程 fallback。若进程异常退出，先人工关闭上游气源，独立确认 A/B/C=0 后再等待新的恢复授权。

## 每场证据

每个场景使用独立目录，至少保存：候选 commit/tree 与工作树状态、有效参数、timeline、commands、receipts、shutdown event、A/B/C/气味阀/selector 终态、owner handoff、summary 和 SHA-256。所有证据必须明确区分“软件 receipt”“模拟观察”和“真实机械/气路观察”。

## 新的真实 HIL 入口（开发完成并复审前禁止运行）

入口为 `scripts/hil_story45_live.py`，不得用旧 `hil_actuation_benchmark.py` 替代。三个命令互相分离：

1. `plan` 只读取 Git/JSON 并把 manifest 写到仓库外，不导入 `nidaqmx` 或 `serial`。
2. `preflight` 只枚举 NI 并发送 Alicat 查询帧，不含 setpoint 或 DO 写入。A 型号/序列号使用官方只读 `??M*`，满量程使用 `FPF 5`（mass-flow statistic 5）核对；参见 [Alicat Serial Communications Primer](https://documents.alicat.com/Alicat-Serial-Primer.pdf)。
3. `run` 必须同时匹配干净候选 commit/tree、有效硬件配置快照及其 SHA-256、场景、manifest SHA-256 和完整 token；每进程只允许一个场景，不支持 live `--all`。任何 `RECOVERY_REQUIRED`（即使是预期 fault 注入）都返回非零。

候选提交完成后才可生成 manifest：

```powershell
python scripts/hil_story45_live.py plan --scenario normal --output "$env:TEMP\story45-normal-manifest.json"
```

只读检查命令如下；它不授权后续写入：

```powershell
python scripts/hil_story45_live.py preflight --confirm I-CONFIRM-READ-ONLY --output "$env:TEMP\story45-preflight.json"
```

### normal 场景时操作者只做什么

1. 运行前保持上游 Air 关闭；无气味材料、容器和受试者，确认阀 2 出口畅通。
2. 执行者展示整张 manifest、候选 commit、哈希、token 和自动收尾；操作者确认后才打开上游洁净 Air。
3. 程序先在 A/B/C=0 时关闭阀 1–20、确认 selector LOW，再置 selector HIGH、只打开阀 2，最后设置 A=`2500 sccm`。
4. A 连续达到 `2250 sccm`（2500 的 90%）后开始 20 秒观察。操作者仅把手放在阀 2 出口前方约 2–5 cm，不能接触或堵住管口；记住“持续气流 / 短促气流 / 无气流”之一。
5. 20 秒结束后程序自动调用生产全局停止，不等待人工输入：A=0 receipt → selector LOW → 阀 1–20 LOW → B/C/A=0 → maintenance/DO/lease/AI/serial handoff。
6. 程序完成最终只读 A/B/C 核对后，操作者关闭上游 Air，再报告观察结果。若最终只读核对失败，程序立即显示“关闭上游 Air”，不静默等待重试；若程序异常退出，第一动作同样是人工关闭上游 Air。
7. 安全停止和关气完成后，执行者用纯离线命令记录观察；只有“持续气流”使 normal HIL 通过，其他结果均保留非零状态且进入调查：

```powershell
python scripts/hil_story45_live.py record-observation --evidence-dir <本次证据目录> --observation 持续气流
```

本清单确认不代表无限授权：只对 manifest 中列出的单次写入有效。fault 场景均从 A/B/C 已读为 0 开始，且每次重新生成和确认独立 manifest；A receipt 故障场景明确不含 selector 写入。
