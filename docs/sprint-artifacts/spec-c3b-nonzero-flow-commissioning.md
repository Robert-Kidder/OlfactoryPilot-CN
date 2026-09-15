---
title: C.3b-4 非零气流与补偿气路 commissioning
type: commissioning-spec
created: 2026-09-15
status: in-progress
route: offline-implementation
context: []
baseline_commit: 5ef6500e942ef6e310d5f2ddc392cf2d270b7806
---

# C.3b-4 非零气流与补偿气路 commissioning

## 意图与边界

本阶段逐级确认洁净 Air 的非零 Alicat 设定、独立回读、实际流量稳定，以及开放的补偿出口。一次只增加一个物理变量；每个实机子阶段都以全部清零、selector 回 COMPENSATION、20 个气味阀关闭、Global Stop 安全收口和资源释放为退出门。第 0 阶段离线代码整改、Fake/Mock、review 和验证已经完成；后续单独实机授权前，**不启动 Real App 实机、不写非零 setpoint、不切真实 selector、不操作真实气味阀**。非零流量不与 odor selector HIGH 或气味阀操作组合。

**分级审批门禁**：本轮只能审批规格方向，**尚不能批准任何非零实机 HIL**。首个 **A-only、B=C=0** 子阶段仍受 A 当前身份/500 sccm 可控范围、A 入口压力及调压状态、洁净 Air 与 LOW 补偿出口实物通路、测试上限/最长时长及现场明确授权阻断。2026-09-15 操作者补充现场人工事实：A/B/C 为相同型号，满量程均为 5000 ml/min（5000 sccm）；历史 A 型号记录为 `MC-5NLPM-D`，但本次信息未提供三台当前逐台型号铭牌/序列号。这只解除 device capacity 未知项，**不改变 commissioning approved maxima=500/0/0 sccm**。后续 **完整 baseline** 仍受 B/C 非零 commissioning limit、B 主气流通路、C 吸气路径及 C>0/A=0 瞬态门禁阻断。详细待确认项见末节。

不属于本轮：气味口开启、Manual release、8 路验证、Auto/Protocol、清洗维护、timing benchmark。普通 UI 继续只显示产品流量和连接状态；技术回执与时序只进入日志/evidence。

## 领域规则与现有代码

用户独立输入 A=sample flow、B=main flow、C=vacuum flow；total delivery=A+B。baseline/restore 的 controller targets 为 **A-controller=A+C，B-controller=B，C-controller=C**；stimulus 为 **A-controller=A，B-controller=B，C-controller=0**。B 全程保持用户设定，不恢复旧的 editable total / B=T−A 模型。[长期规则](../project-context.md)、[气路历史证据](evidence/gas-path-requirements-2026-08-01.md)。当前 `ActuationWorker` 的 baseline/stimulus/restore 按此生成目标；`FlowService` 保持 B→C→A 写入顺序。

真实 UI 的“开始供气”曾被 `MainController.handle_manual_supply_requested()` 的 `not simulation_mode` blanket 门禁拒绝；第 0 阶段已离线整改为默认关闭的 `real_supply_policy` 门禁，仍只能由该权威入口按 Controller→Worker→HAL 请求。不能用旧的 `handle_apply_request`、脚本或直接 HAL 调用绕过；容量事实更新也不授权任何真实命令。

## 设备量程与上限事实

| 控制器 | 真实设备证据 | 当前软件限制 | 实机前缺口 |
| --- | --- | --- | --- |
| A | 历史只读记录型号 `MC-5NLPM-D`、SN `486285`；2026-09-15 现场人工确认当前满量程 `5.0000 NLPM = 5000 sccm`，但本次未重报序列号 | device capacity=5000；commissioning approved max=500 | 当前身份/允许入口压力与现场实际压力、500 sccm 控制/验收及单独实机授权；baseline 的 A+C 还须 ≤5000 |
| B | 2026-09-15 现场人工确认与 A/C 同型号、满量程 `5000 ml/min = 5000 sccm`；未提供当前逐台型号/序列号记录；通信/ID/零流量已真实确认 | device capacity=5000；commissioning approved max=0 | 容量已知不等于非零授权；完整 baseline 仍须另批 B test point、主气流通路与压力边界 |
| C | 2026-09-15 现场人工确认与 A/B 同型号、满量程 `5000 ml/min = 5000 sccm`；未提供当前逐台型号/序列号记录；通信/ID/零流量已真实确认 | device capacity=5000；commissioning approved max=0 | 容量已知不等于非零授权；完整 baseline 仍须另批 C test point、真实吸气路径与过渡安全 |

`config/local_config.json` 当前为 Real、COM6@19200、a/b/c、Dev1/Dev2，启用气口 2/4/6/8/12/14/16/18；selector 为 Dev2/P1.0，LOW=COMPENSATION、HIGH=ODOR。C.3b-3 的 21 路安全 LOW 证据只证明零流量连接/停止，不证明新非零气流安全。任何配置、设备身份、出口或气路变化都要求 HALT 和重审，不能套用旧证据。

首轮前须确认当前现场 A **仍是**上述型号、序列号和量程，优先直接核对可查看的设备铭牌/面板，或使用已有可信、可追溯的现场设备记录；历史 Story 4.5 回报不能独自证明当前未换机。Alicat 官方只读 manufacturer-info 语法为 `[unit ID]??M*<CR>`，历史本机回报呈多行，而当前 `AlicatSerialSession` 严格按单个 CR frame 完成一问一答。**不在本轮把多行命令塞入现有单帧 transaction，也不要求为 A-only 新增该功能**；本次人工确认已经成为 A/B/C 同型号、三路容量均为 5000 sccm 的当前 authority。若以后需要补充逐台序列号追溯，可另立只读 multi-frame manufacturer-info 规格，不能与本轮 A-only 混做。[Alicat 官方串口命令说明](https://www.alicat.com/support/serial-communication-tutorial/)。

## 首轮候选值与待批准条件

**仅提出候选：用户 A=500、B=0、C=0 sccm；baseline 实际 targets=500/0/0 sccm。不是本规格自动授权的实机命令。** 500 为 A 满量程 5000 sccm 的 10%，是历史开放补偿出口 A=2500 sccm 稳定实测点的 1/5；B/C 虽也确认具有 5000 sccm device capacity，仍因 commissioning approved max=0 必须保持 0，并继续避开未追踪的 B 主气流和 C 真空路径。Alicat 公开的 **MC 10 SCCM–20 SLPM 系列**标准稳态控制范围为 0.01%–100% FS；因此 10% FS 是设备家族控制范围内的合理候选，不是贴近家族下限的试探值。这只是 **family-level supporting evidence**，不能替代当前 SN `486285` 的实际校准/配置、现场压力和本次稳定验收标准。[官方 MC 系列技术资料](https://documents.alicat.com/specifications/DOC-SPECS-MC-MID.pdf)。人工仍须先确认本机在 500 sccm 可控、洁净 Air 与补偿出口畅通、批准该值和最大持续时间；若证据不足，不定测试点、不写硬件。不能在不稳定时自行升至 1000/1500/2500 或改 B/C。

完整 baseline 的 B/C 非零组合**暂不设数值**。三台 5000 sccm device capacity 已确认，但 B/C commissioning approved max 仍为 0；只有 C 吸气路径、B 主气流通路、A+C 容量门禁和新的人工批准均完成后，才能另行明确低风险 A/B/C 组合与非零批准上限。不可仅因设备容量、软件 `max_total_sccm` 或历史示例选择值，也不可孤立给 C 非零。

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

审批 A-only 前需确认：① 通过可查看的铭牌/面板或可信现场记录确认当前 A 型号/序列号/量程及 500 sccm 可控性；不强制新增多帧 manufacturer-info；② 本机允许入口压力、当前现场压力/调压器状态、预期洁净 Air、实物 LOW 入口 2→出口 3 及出口畅通，**压力任何一项无法确认即 BLOCKED**；③ 首轮 requested、accepted/readback、measured mass-flow 三类容差、连续样本数、settling 窗口/期限与最长非零持续时间，全部实机前离线定稿；④ 真实“开始供气”门禁、唯一 owner 采样、desync fail-closed 与容量门禁离线整改通过复审。B/C 零设定和零实际流量要独立回读。审批完整 baseline 不再缺 B/C 满量程事实，但仍额外需要 B/C 非零 commissioning test point/approved limit、B 主气流出口/压力边界、C 吸气路径及 C>0/A=0 瞬态安全证明。**相应门禁未解决时，该子阶段非零实机 HIL BLOCKED。** 当前离线实现完成后立即停止；规格批准与代码就绪均不等于批准任一真实硬件命令。

## Tasks & Acceptance

第 0 阶段离线默认值冻结为：requested setpoint tolerance=`1e-9 sccm`（软件 intent 数值一致性）；accepted/readback tolerance=`1.0 sccm`（不沿用约 50 sccm 的旧默认）；active measured mass-flow stability tolerance=`±25 sccm`，zero-channel tolerance=`≤5 sccm`；连续有效样本数=`6` 且首末样本覆盖至少 `1.0 s`；maximum settling deadline=`5.0 s`；maximum non-zero hold duration=`15.0 s`，从首个非零 TX 单调时戳起算。500 sccm 下 family-level 标准精度量级约为 ±5 sccm（±0.6% reading 与 ±0.1% FS 取较大），±25 sccm 是只用于首轮 commissioning 的保守稳定带，不冒充本机校准精度。所有值必须进入结构化、默认禁用的 Real supply policy；现场资料或人工批准改变数值时，须在实机前离线改配置并重跑边界测试，运行中不可修改。当前 policy 必须保持 `enabled=false`，所以本轮代码不会授权 500 sccm。

- [x] **真实供气权威入口与容量门禁**（`app/controllers/main_controller.py`、配置/模型及对应测试）：移除 Real 模式 blanket rejection，但只允许唯一 Controller→Worker→HAL transaction。A/B/C device capacity 均明确为 5000；A target 必须校验 baseline 的 A+C；B/C 当前 approved limit=0，容量已知不得绕过 commissioning limit；A=500/B=C=0 不硬编码为自动动作。
  - Given Real 已连接且 device capacity=5000/5000/5000、commissioning approved maxima=500/0/0，When 请求 500/0/0，Then 仅提交一次现有 supply-only plan；When B或C>0，Then 因 approved limit=0 而零硬件提交；When A+C>5000，Then 因 A capacity 而零硬件提交。
- [x] **三路可信实际流量采样**（`app/workers/flow_worker.py`、`app/services/real_hal.py`/协议接口及测试）：连接状态下仅 FlowWorker 串行采集 A/B/C 的完整 Poll readback，发布带 unit、setpoint、mass flow、freshness/monotonic timestamp 的快照；不得创建第二个 serial session。
  - Given 一个已连接 serial owner，When 采集三路，Then A→B→C transaction 严格串行且只使用同一 owner；任一 timeout/partial/mismatch/desync 时不发布伪造的 0 或 fresh-ready。
- [x] **三类 tolerance 与 settling/hold 门禁**（配置、worker/controller 状态机及测试）：分别定义 requested setpoint、accepted/readback、measured mass-flow stability tolerance；定义连续有效样本数、settling observation window、maximum settling deadline、maximum non-zero hold duration。值必须可审计且在首个非零 TX 前冻结；最大持流从首个非零 TX 单调时戳起算并触发既有 Global Stop/SafeStop，不恢复旧动作。
  - Given 边界内/外样本、过时样本、deadline 与 hold timeout，When 状态机评估，Then 仅连续新鲜三路证据满足全部门槛时确认稳定；超限只触发一次 fail-closed，不能升流量、retry 或延长时限。
- [x] **serial desync fail-closed**（`app/services/flow_service.py`、FlowWorker/Controller 及测试）：transport desync latch 后，rollback、zero、Global Stop 不得在同一 session 新 TX 或宣称归零成功；上报零流量无法确认并进入需要现场停止/断电的安全状态。
  - Given 任一通道 transaction desync，When rollback 或安全停止开始，Then serial TX count 不增加、zero receipt 为 uncertain/failed、`recovery_required=True`，并保留既有 digital safe-stop 偏序。
- [x] **范围保护与离线验证**：不改 B→C→A、Global Stop 偏序、NI lifecycle、HardwareProfile mapping/polarity、普通 UI 文案；补齐 Fake/Mock regression，运行 Ruff、定向测试、完整 pytest、PyInstaller 和 simulation smoke，且不访问真实硬件。
  - Given Fake/Mock 与 simulation，When 验证完成，Then 覆盖上述成功/边界/失败路径且 0 failure；repository 搜索不存在 Real supply 旁路或第二 serial owner。

## 依据

- [气路拓扑与历史 clean-Air 补偿/封闭路径证据](evidence/gas-path-requirements-2026-08-01.md)
- [C.3b-3 最终严格真实零流量验收](evidence/c-3b-final-real-connect-global-stop-2026-09-15.md)
- [Story 4.5 A 设备身份历史只读回报](evidence/story-4-5-hil-normal-20260818/preflight.json)
- [待办：Alicat readback 容差/zero receipt](deferred-work.md)

## Review Triage Log

| Finding | Verdict | Route | Evidence |
| --- | --- | --- | --- |
| VG-1 B/C 零目标越界实际流量缺少测试 | medium | patch | `FlowSettlingMonitor` 有门禁但原测试只覆盖边界值；已补 B、C 各自越界至 settling deadline 的回归。 |
| VG-2 accepted/readback mismatch 缺少拒绝路径测试 | high | patch | `_apply_real_supply_receipt_gate()` 是非零命令后的关键门禁；已补超出 1.0 sccm 时不创建 monitor 且 `recovery_required=True` 的回归。 |
| VG-3 ActuationWorker 缺少未确认 A=0 的负向测试 | high | patch | 背景 SafeStop 必须拒绝缺失/非零实际读回；已补 selector 不得前进并进入 recovery 的回归。 |
| EC-1 容量边缘使用 requested tolerance 放宽 | medium | patch | “未知容量只允许 0、不得越过真实容量/批准上限”是严格门禁；边界比较不应借 intent identity tolerance 放宽。 |
| EC-2 stale/out-of-order snapshot 不清空连续样本 | medium | patch | 当前 early return 保留旧有效样本，违反“连续新鲜样本”要求。 |
| EC-3 相同 restore 可在授权消费前重复入队 | high | patch | submit 与执行前均检查但首个执行后 authorization 变为 `None`，第二条会落入普通 manual 放行路径。 |
| EC-4 authorization cancel 与 receipt gate 存在竞态 | high | patch | receipt gate 未持 `_condition`；cancel 可在读取 authorization 后清除，随后 monitor 被重新建立。 |
| EC-5 无 airflow sink 时 monitor 不轮询 | high | patch | run loop 和 `_poll_airflow()` 都以 sink 为前提，可能使 maximum hold 无法执行。 |
| BH-1 enabled 配置省略容量/批准上限时使用默认值 | false | reject | 正式应用先合并版本化 default config；当前 A/B/C device capacity=5000/5000/5000 与 commissioning approved maxima=500/0/0 是冻结配置，`enabled=false` 才是实机授权门。仍补 parser 负向校验，避免原始畸形输入被吞掉。 |
| BH-2 falsey 非 Mapping 被 `or {}` 吞掉 | medium | patch | `[]`、空字符串、0 会被当成缺省对象，削弱配置验证。 |
| BH-3 缺少身份/压力/Air/通路逐项布尔 attestation | false | reject | 这些是下一轮明确的现场人工门禁；唯一 `enabled` 是本地、默认关闭且需单独改动的总体授权门，不是普通 UI 设置。本轮不把现场清单扩成产品配置框架。 |
| BH-4 `manual_post_close_a_zero` 未采用 commissioning 严格读回 | high | patch | 当前仅 restore 使用 1.0 sccm 门禁，pre-close 可能依据较宽 legacy setpoint tolerance 前进 selector。 |
| BH-5 未来 B/C 非零会在 monitor 前运行 | false | reject | 当前批准上限固定 B=0、C=0，任何 B/C 非零在 Controller 提交前被拒；未来改变上限必须另行规格与验证。 |
| BH-6 polling 可因 sink/队列而错过 deadline | high | patch | monitor 目前依赖 sink且普通队列优先于 poll；active monitor 应独立驱动并优先检查 deadline。 |
| BH-7 evaluator 用 snapshot 时刻且先 emit snapshot | medium | patch | DirectConnection subscriber 可延迟评估；生产路径应先按当前单调时钟评估，再发布快照。 |
| BH-8 commissioning failure signal 被拒时 FlowWorker 不直接停机 | false | reject | 已连接生命周期保证 ActuationWorker 为接受状态且 DirectConnection 只做 owner mailbox ingress；若唯一动作 owner 已停止，FlowWorker 不得越权直接执行 SafeStop。 |
| BH-9 ActuationWorker 无第二套 hold timer | false | reject | hold 的唯一权威时基属于首个 serial TX，FlowWorker 在 bounded poll transaction 上执行；复制一套 ActuationWorker timer 会制造两个互相漂移的安全时钟。 |
| BH-10 stale/duplicate/out-of-order 不打断连续样本 | medium | patch | 与 EC-2 同位置但独立审查结论；必须清空累计样本。 |
| BH-11 settling 未检查 gas=Air | high | patch | 本阶段只批准洁净 Air；非空但错误 gas 不能通过稳定门禁。 |
| BH-12 final A/B/C zero 使用请求值而非三路读回 | high | patch | `zero_all_for_safe_stop()` 目前检查 `result.a/b/c`，`zero_confirmed` 也由请求值计算，不能证明设备设定已归零。 |
| BH-13 AuthorizedHAL 未转发 snapshot/desync/TX timestamp | medium | patch | 该受权代理包装 HAL 后会退回单通道或丢失 desync/TX 时基，违反新增协议接口。 |
| BH-14 commissioning evidence 没有持久可捕获的三路样本细节 | medium | patch | 当前 signal 无 production consumer，状态日志也不含 raw frame、setpoint、mass flow 与 sample timestamp；下一轮 HIL 缺直接证据。 |
| Root-1 setpoint command 失败时 rollback 漏掉当前通道 | high | patch | A 非零命令若合法响应但读回不匹配，旧代码只清零先前成功通道，未尝试清零当前可能已受影响通道；健康同步 session 下必须把当前通道纳入有回执的 rollback。 |

## 离线实现与验证结果

- 实现保持 `Controller → ActuationWorker/FlowWorker → HAL`、MANUAL lease、exact receipt 与既有 SafeStop；Real “开始供气”只有默认关闭的 `real_supply_policy.enabled` 明确启用且冻结目标通过容量/批准上限校验后，才会把一次性授权绑定到同一 operation/generation。
- `FlowWorker` 是连接会话内唯一 Alicat owner；三路 A→B→C Poll 使用同一 `RealHAL` / `AlicatSerialSession`，快照包含 unit、setpoint、mass flow、gas、freshness 与单调时戳。commissioning monitor 不依赖 UI sink，且优先于普通命令队列检查 settling/hold deadline。
- serial timeout、partial、mismatch 或 desync 不发布伪造零流量；desync latch 后 rollback/zero 零新增 TX，并将零流量标记为无法确认、要求现有 fail-closed / 现场停止或断电路径。
- 独立 review 共登记 23 项 finding：17 项修补，6 项因超范围、破坏唯一硬件 owner 或重复安全时基而有证据拒绝；详见上表。补强包括严格容量边界、连续样本失效重置、授权消费/取消竞态、三路实际读回安全收口、Air 校验及低频结构化样本审计。
- 2026-09-15 离线验证：定向回归 `246 passed`；完整 `pytest` 为 `1427 passed, 1 skipped`；`python -m ruff check .`、`git diff --check`、PyInstaller 构建和 offscreen Mock simulation 均通过。既有 Python GC `ResourceWarning` 已记录在 deferred work；Mock/Fake PASS 不构成真实非零硬件证据。
- 全程未打开 COM6、未创建真实 NI task、未启动 Real App 实机、未向 Alicat 写入 setpoint，也未切换真实 selector 或气味阀。`A=500, B=C=0` 仍只是下一轮候选，必须通过本文现场门禁并获得单独人工授权。

2026-09-16 authority 修订：2026-09-15 操作者补充 A/B/C 同型号、各 5000 sccm 满量程事实，版本化 device capacity 已统一为 5000/5000/5000；commissioning approved maxima 保持 500/0/0，默认 `enabled=false`。B/C 非零继续由批准上限 0 在硬件提交前拒绝，而非由“容量未知”拒绝。第 0 阶段离线实现已完成，但 C.3b-4 真实非零 HIL 尚未获授权或执行，因此本总规格保留活动状态 `in-progress`。
