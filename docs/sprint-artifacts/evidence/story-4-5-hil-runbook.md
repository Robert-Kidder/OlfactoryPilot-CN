# Story 4.5 HIL 现场准备单

## 现在的状态

当前只批准了离线演练。**不要开机器，不要连接或枚举 NI/Alicat，不要打开气源，也不要运行任何旧 HIL live 脚本。**

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
| 正常场景 | Air；A=`1500 sccm`、B=C=`0` |
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

只有获得新的明确授权后才进入本节；每一项都要单独确认，不能一次批准整场运行。

1. 只读 preflight：确认设备身份、串口地址、压力范围和现场 Air 条件；不写硬件。
2. 在 A/B/C 已独立确认为 0 时，只允许用已批准的阀位指示、压力测点或其他独立反馈确认 selector 路线。零流量时不能靠出口流量或手感判断；没有独立反馈就停止。若必须使用诊断流量，须另写限值、压力条件、单独写入授权和自动清零步骤，本单不授权。
3. 场景开始前逐项预授权自动收尾：A=0、B=0、C=0、selector 写入 compensation、气味阀 1–20 LOW。selector 写入仍受“A=0 匹配 receipt 先行”门禁；未授权完整收尾就不启动场景。
4. 正常停止场景逐项授权：代表气味阀、selector 气味路线、A 非零流量；随后触发全局停止。
5. 现场核对严格顺序：fence → A=0 匹配 receipt → selector 补偿路线 → 气味阀 1–20 关闭 → A/B/C=0 → owner handoff。
6. fault 场景从物理 A/B/C=0 开始。receipt 失败、stale、late 或 selector 不确定时，不得宣称安全完成，也不得自动反复切 selector。

每次准备写入前，执行者必须给出：写入编号、设备、目标、原状态、新状态、原因和该步失败时的收尾。操作者明确回复该编号后，才允许执行该一项。

逐项授权账本模板：

| 写入 ID | 设备/目标 | 已确认原状态 | 请求新状态 | 原因 | 失败收尾 | 操作者确认/时间 |
|---|---|---|---|---|---|---|
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 未授权 |

## 自动收尾与停止条件

未来真实运行的自动收尾必须已经预授权，并在 `finally` 中继续执行可安全完成的动作：fence 新动作、A 清零门禁、关闭气味阀、A/B/C 清零、maintenance/DO/lease/AI/serial handoff、保存证据。

出现以下任一情况立即进入 `RECOVERY_REQUIRED`：A 清零 receipt 无效或超时、selector receipt 无效或不确定、最终 A/B/C/气味阀证据不完整、任一 owner 未交还。selector 不确定时不自动重试；DO owner 卡住时不跨线程 fallback。若进程异常退出，先人工关闭上游气源，独立确认 A/B/C=0 后再等待新的恢复授权。

## 每场证据

每个场景使用独立目录，至少保存：候选 commit/tree 与工作树状态、有效参数、timeline、commands、receipts、shutdown event、A/B/C/气味阀/selector 终态、owner handoff、summary 和 SHA-256。所有证据必须明确区分“软件 receipt”“模拟观察”和“真实机械/气路观察”。
