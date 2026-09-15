---
title: C.3b-4 非零气流与补偿气路 commissioning
type: commissioning-spec
created: 2026-09-15
status: ready-for-dev
route: investigate-and-spec-only
context: C.3b-3 严格真实验收已通过；本规格尚未授权任何新实机动作
---

# C.3b-4 非零气流与补偿气路 commissioning

## 意图与边界

本阶段逐级确认洁净 Air 的非零 Alicat 设定、独立回读、实际流量稳定，以及开放的补偿出口。一次只增加一个物理变量；每个实机子阶段都以全部清零、selector 回 COMPENSATION、20 个气味阀关闭、Global Stop 安全收口和资源释放为退出门。当前仅调查和规划；本规格批准前及后续单独实机授权前，**不启动 Real App、不写非零 setpoint、不切 selector、不操作气味阀**。非零流量不与 odor selector HIGH 或气味阀操作组合。

**分级审批门禁**：本轮只能审批规格方向，**尚不能批准任何非零实机 HIL**。首个 **A-only、B=C=0** 子阶段受真实“开始供气”代码拒绝、A 当前身份/500 sccm 可控范围、A 入口压力及调压状态、洁净 Air 与 LOW 补偿出口实物通路、测试上限/最长时长及 settling 验收值阻断。B/C 量程未知，因此不授权其非零设定；若 B/C 的设定值和实际流量均独立回读为零，这一缺口不单独阻断 A-only 阶段。后续 **完整 baseline** 额外受 B/C 量程、B 主气流通路、C 吸气路径及 C>0/A=0 瞬态门禁阻断。详细待确认项见末节。

不属于本轮：气味口开启、Manual release、8 路验证、Auto/Protocol、清洗维护、timing benchmark。普通 UI 继续只显示产品流量和连接状态；技术回执与时序只进入日志/evidence。

## 领域规则与现有代码

用户独立输入 A=sample flow、B=main flow、C=vacuum flow；total delivery=A+B。baseline/restore 的 controller targets 为 **A-controller=A+C，B-controller=B，C-controller=C**；stimulus 为 **A-controller=A，B-controller=B，C-controller=0**。B 全程保持用户设定，不恢复旧的 editable total / B=T−A 模型。[长期规则](../project-context.md)、[气路历史证据](evidence/gas-path-requirements-2026-08-01.md)。当前 `ActuationWorker` 的 baseline/stimulus/restore 按此生成目标；`FlowService` 保持 B→C→A 写入顺序。

真实 UI 的“开始供气”目前被 `MainController.handle_manual_supply_requested()` 的 `not simulation_mode` 门禁明确拒绝。不能用旧的 `handle_apply_request`、脚本或直接 HAL 调用绕过。后续需要先做独立的离线产品授权整改、量程校验与 fake/Mock regression，再申请具体实机动作；**本轮不改产品代码**。

## 设备量程与上限事实

| 控制器 | 真实设备证据 | 当前软件限制 | 实机前缺口 |
| --- | --- | --- | --- |
| A | 历史只读设备回报 `MC-5NLPM-D`、SN `486285`、满量程 `5.0000 NLPM = 5000 sccm` | sample A 输入 ≤5000 sccm | 当前项目批准的非零 HIL 上限、当前本机允许入口压力与现场实际压力、500 sccm 控制/验收依据；baseline 的 A+C 还须 ≤A 真实允许范围 |
| B | 通信/ID/零流量已真实确认；型号、满量程未见可信证据 | `max_total_sccm=5000` 仅限制 A+B，**不是 B 单机上限** | 人工或可信设备资料确认型号、满量程、允许 setpoint、项目批准上限 |
| C | 通信/ID/零流量已真实确认；型号、满量程未见可信证据 | vacuum C 输入 ≤5000 sccm，仅为软件上限 | 人工或可信设备资料确认型号、满量程、允许 setpoint、项目批准上限与真实吸气路径 |

`config/local_config.json` 当前为 Real、COM6@19200、a/b/c、Dev1/Dev2，启用气口 2/4/6/8/12/14/16/18；selector 为 Dev2/P1.0，LOW=COMPENSATION、HIGH=ODOR。C.3b-3 的 21 路安全 LOW 证据只证明零流量连接/停止，不证明新非零气流安全。任何配置、设备身份、出口或气路变化都要求 HALT 和重审，不能套用旧证据。

首轮前须确认当前现场 A **仍是**上述型号、序列号和量程，优先直接核对可查看的设备铭牌/面板，或使用已有可信、可追溯的现场设备记录；历史 Story 4.5 回报不能独自证明当前未换机。Alicat 官方只读 manufacturer-info 语法为 `[unit ID]??M*<CR>`，历史本机回报呈多行，而当前 `AlicatSerialSession` 严格按单个 CR frame 完成一问一答。**不在本轮把多行命令塞入现有单帧 transaction，也不要求为 A-only 新增该功能**；人工/现有可信记录足以确认 A 时即可满足身份门禁。B/C 因面板难查看，后续可另立只读 multi-frame manufacturer-info 规格，不能与本轮 A-only 混做。[Alicat 官方串口命令说明](https://www.alicat.com/support/serial-communication-tutorial/)。

## 首轮候选值与待批准条件

**仅提出候选：用户 A=500、B=0、C=0 sccm；baseline 实际 targets=500/0/0 sccm。不是本规格自动授权的实机命令。** 500 为已知 A 满量程 5000 sccm 的 10%，是历史开放补偿出口 A=2500 sccm 稳定实测点的 1/5；B/C 保持 0，避免用未知 B/C 量程和未追踪 C 真空路径试探。Alicat 公开的 **MC 10 SCCM–20 SLPM 系列**标准稳态控制范围为 0.01%–100% FS；因此 10% FS 是设备家族控制范围内的合理候选，不是贴近家族下限的试探值。这只是 **family-level supporting evidence**，不能替代当前 SN `486285` 的实际校准/配置、现场压力和本次稳定验收标准。[官方 MC 系列技术资料](https://documents.alicat.com/specifications/DOC-SPECS-MC-MID.pdf)。人工仍须先确认本机在 500 sccm 可控、洁净 Air 与补偿出口畅通、批准该值和最大持续时间；若证据不足，不定测试点、不写硬件。不能在不稳定时自行升至 1000/1500/2500 或改 B/C。

完整 baseline 的 B/C 非零组合**暂不设数值**。B/C 真实范围、C 吸气路径和 A+C 容量门禁确认后，另行明确低风险 A/B/C 组合、批准上限与单独动作授权；不可仅因软件 `max_total_sccm` 或历史示例选择值，也不可孤立给 C 非零。

现有 B→C→A 安全顺序及 supply-only 前置 `manual_post_close_a_zero` 在未来 C 非零时，都可能形成 **C>0、A=0** 的瞬态；“成套 baseline 目标”并不能消除这个过渡。必须先用真实气路证据证明该瞬态与吸气路径安全，或在另行授权的离线设计中提出兼容现有安全偏序的方案；还须确认 B 主气流的实际出口、开放通路与压力边界。未证明前 B/C 保持 0，不进入完整 baseline 实机阶段。C 不得孤立非零。

**selector 安全边界**：历史实机中，HIGH + A 非零 + 20 阀全关表现为不通畅、疑似封闭路径。HIGH 电气回执只能在全部 setpoint=0 的另行授权子阶段验证；HIGH 的有流量物理路径必须等单口首轮提供合法开放出口。

## 后续实机 runbook（当前均未执行）

0. **离线门禁**：按上列审批门禁修正真实“开始供气”拒绝路径，但保留唯一 Controller→Worker→HAL、租约、exact receipt、SafeStop；补上 B 单机及 A+C 真实容量检查与测试。为连接状态下 B/C 实际流量采样建立 FlowWorker 唯一串口 owner 内的路径与 Fake 回归，严禁第二个 COM6 诊断 session 抢占。对 serial desync 后 `FlowService._rollback_flows()` 和 Global Stop 的串口零命令做零新 TX regression；无法确认零流量时必须进入需现场停止/断电的状态。确认 setpoint/readback/实际 flow 各自的容差、settling 与上层 deadlines 后复审，人工批准具体测试点、持续时间和停止方式。
1. **只读 preflight**：Git/配置/设备身份及现场无受试者、无样品；按上述优先方式确认当前 A 仍为 `MC-5NLPM-D`、SN `486285`、5 NLPM。人工实物确认 A 入口确实连接预期洁净 Air 气源，上游调压器/气源处于正常工作状态，当前实际入口压力落在**这台具体 A 控制器**允许工作范围内，不存在明显异常高压、错误气源或异常压差；允许范围优先由本机铭牌、校准证书或可信历史设备资料确定。除非已确认传感器位置和气路关系，不能把 Poll 帧的压力读数直接当作上游入口压力。Alicat 通用 MC 规格的标准压力参数仅作辅助参考，**不能替代本机允许值或现场实际压力**；任一无法确认即 A-only BLOCKED，不为测试临时调整调压器（后续明确人工授权除外）。另确认 LOW 实物通路为公共入口 2→补偿出口 3、出口 3/下游无遮挡畅通、操作者可立即停机/断电。只读 Poll/LSS 要求 A/B/C gas 字段为 Air、setpoint 与 mass flow 近零、U/U/U，COM6 释放。身份、压力、气体或出口不符以及任一串口异常均 HALT。[官方 MC 系列标准规格，仅辅助参考](https://documents.alicat.com/specifications/DOC-SPECS-MC-MID.pdf)。
2. **零流量连接**：正式 Real App startup auto-connect 一次；safe DO image、self-check、B→C→A=0、fresh readiness、零流量 idle 均成功。不得人工重连补救。
3. **首个非零变量 A**：仅在已批准 500/0/0 与 LOW compensation 开放出口条件下，从权威产品“开始供气”路径执行。必须核验本次 supply-only transaction 的 **post-close A=0 exact receipt → selector COMPENSATION exact receipt → restore_supply A=500** 顺序，不能拿 startup 的 selector LOW 代替本次回执；任一前置回执失败，不得发送非零 setpoint 命令。全过程按“Readback、settling 与 PASS 门槛”记录命令响应、独立 Poll、实际流量和单调时戳。操作者仅在出口前 2–5 cm 观察，不接触/堵塞出口，记录是否有持续气流、异常声响、阀/selector 意外动作；保持 B/C=0，不切 HIGH、不打开气味阀。
4. **稳定与退出**：使用连续、新鲜、独立 readback 判定 A accepted setpoint 和 measured mass flow 的稳定窗口；不能把固定 sleep、UI 数字或单次 setpoint receipt 当物理稳定。最长持续时间从**首个 A=500 非零 TX 的单调时戳**保守起算，由现场操作者及只读观察者共同计时；产品目前没有可据此宣称的持流自动 watchdog。若该命令/回读失败，不等到时限而立即停机。到期前由用户**只人工执行一次 Global Stop**，以现有安全偏序归零并释放；不要先点“停止供气”再点一次 Global Stop（当前“停止供气”也进入 `stop_hardware()`）。GUI 无响应或物理危险时现场停止/断电，不等待计时结束。稳定性失败不自动升流量或 retry，立即走同一停止门禁。
5. **安全收口/最终回查**：Global Stop 要求 selector COMPENSATION receipt、20/20 valve CLOSED receipts、A/B/C zero 及实际流量回落的独立证据、DO/serial release 全部确认；正常关窗、进程 exit code=0；App 退出后只读 Poll/LSS 确认 0/0/0 和 U/U/U，COM6 再释放。每个子阶段失败即 HALT；安全无法确认时操作者现场停止/断电，不以第二次连接或脚本补救。

selector 的 LOW compensation 物理流路在第 3 步观察；零流量 HIGH 电气回执不能冒充 odor 流路物理验证。完整 baseline 的 C 路必须先解决前述吸气路径与 C>0、A=0 过渡门禁。

## Readback、settling 与 PASS 门槛

每条未来真实命令区分 requested target、设备 command response/accepted setpoint、独立 Poll setpoint readback、measured mass flow、settling elapsed、phase、单调时戳、异常状态。新 Alicat transaction 必须 CR 结尾、一问一答、命令响应归属完成后才发 Poll；timeout/partial/mismatch/desync 零后续 TX，不能把 best-effort rollback 当零流量证据。**家族级控制范围不能确定本次 PASS tolerance。** 现有 `alicat_setpoint_tolerance=0.05` 为设备单位约 **50 sccm，即 500 候选的 10%**，不是已批准的 setpoint 精度，更不是三台真实流量稳定容差；A low-flow/SAFE 阈值约 0.2 sccm 只用于安全状态，不是 500 sccm 稳定证明。A telemetry 的新鲜度、FlowWorker 周期、setpoint verification 和 Global Stop deadline 必须与未来 frame timeout/settling window 一并审计。**requested setpoint tolerance、accepted/readback tolerance、measured mass-flow stability tolerance、连续有效样本数、settling observation window、maximum settling deadline、maximum non-zero hold duration，均须在首轮实机前离线明确，以本机设备资料、代码语义与保守 commissioning 目的支撑并做边界 Fake regression；不得测试开始后临时修改。** B/C 实际流量的可信独立采集路径也须确认。

PASS 只可基于：目标及回读一致、连续新鲜实际流量在批准容差内且无异常、现场补偿出口畅通且无意外动作、时限内安全清零、Global Stop 精确回执和最终资源释放。若尚未非零即遇异常，立即 HALT；若已非零而普通测量/settling 失败且 Controller 可用并能核验回执，**立即由操作者执行一次 Global Stop 后 HALT**；若明显异常气流、出口受阻、设备/GUI 不响应、串口 desync 或安全状态不可确认，立即现场停止/断电并 HALT，不能盲目依赖软件零命令。不能重发、升流量、临时改 tolerance 或继续下一物理阶段。

## Evidence 与审批问题

新 evidence 必须留存每个 command target/response、独立 Poll raw frame 与解析值、mass-flow 样本和 settling 时戳、Controller phase、现场观察、每次 zero/Global Stop 的 exact receipts、DO/serial release、最终 Poll/LSS、异常与操作者停机记录；历史 C.3b-3 PASS 与早期 FAIL 不改写。Mock/Fake 和 simulation 只作离线保障，不作非零实机证据。

审批 A-only 前需确认：① 通过可查看的铭牌/面板或可信现场记录确认当前 A 型号/序列号/量程及 500 sccm 可控性；不强制新增多帧 manufacturer-info；② 本机允许入口压力、当前现场压力/调压器状态、预期洁净 Air、实物 LOW 入口 2→出口 3 及出口畅通，**压力任何一项无法确认即 BLOCKED**；③ 首轮 requested、accepted/readback、measured mass-flow 三类容差、连续样本数、settling 窗口/期限与最长非零持续时间，全部实机前离线定稿；④ 真实“开始供气”门禁、唯一 owner 采样、desync fail-closed 与容量门禁离线整改通过复审。B/C 零设定和零实际流量要独立回读。审批完整 baseline 额外需要 B/C 型号/满量程/允许值、B 主气流出口/压力边界、C 吸气路径及 C>0/A=0 瞬态安全证明。**相应门禁未解决时，该子阶段非零实机 HIL BLOCKED。** 本轮到 spec approval 即停止，等待 `Approve / Edit`；批准规格本身仍不等于批准任一真实硬件命令。

## 依据

- [气路拓扑与历史 clean-Air 补偿/封闭路径证据](evidence/gas-path-requirements-2026-08-01.md)
- [C.3b-3 最终严格真实零流量验收](evidence/c-3b-final-real-connect-global-stop-2026-09-15.md)
- [Story 4.5 A 设备身份历史只读回报](evidence/story-4-5-hil-normal-20260818/preflight.json)
- [待办：Alicat readback 容差/zero receipt](deferred-work.md)
