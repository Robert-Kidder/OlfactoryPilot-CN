---
baseline_commit: 49fd99ecd0b4247555d071e6fd7e0fff8b3205b9
---

# Story 4.1: 自动清洗流程

Status: in-progress

<!-- 本 Story 受 2026-07-31 Epic 4 冻结技术边界约束；改变冻结项必须重新运行 Correct Course。 -->

## Story

作为实验室技术人员，
我需要自动清洗序列，
以便实验后冲洗残留气味。

## Acceptance Criteria

1. 使用显式 `CLEANING` action category 和独占 maintenance lease；清洗 open 不得标记为 `SAFETY`，也不得与协议、manual/pretest 或补偿流程并发。
2. maintenance bundle 在首次硬件副作用前完成绑定并达到 recorder readiness；bundle 使用 `maintenance-v1` schema，包含结构化 `.log` 与 `manifest.json`，不混入实验 session。
3. 清洗由 owner 状态机和 monotonic deadline 驱动；每个步骤携带 `operation_id + generation + step_id + command_id + target + action_kind`，匹配 receipt 后才推进。
4. 全程遵守气流安全联锁；stop、`LOW_FLOW`、disconnect、recorder/owner failure 可抢占普通步骤。
5. stale/late receipt 不推进新步骤；late successful open、close timeout 或不确定 ack 保留 `possibly_open` 并触发安全收敛。
6. 中止、失败和 shutdown 必须关闭全部 21 个配置目标并请求 A/B/C 清零；只有目标集合 close receipt 完整、flow 清零成功且 owner 完成交接后才可报告安全终态，清零无法确认则进入 `RECOVERY_REQUIRED`。
7. 清洗步骤、command/receipt、失败、安全动作和恢复要求全部进入 maintenance bundle；失败 bundle 不得发布为 complete 或原地续写。
8. 清洗页以直观复选框列出已配置气味通道及机外气路标签，并允许在满足 Story 4.2 断开态门禁时调整、原子保存清洗通道、清洗流量、每路持续时间与循环次数；保存值写入现有 `local_config.json` 的 `cleaning` 配置并在以后每次启动时自动恢复，不使用只存在于 View、`QSettings` 或第二份 JSON 的临时偏好。当前本机默认依次清洗软件通道 `2,3,4,5,6,7,8,9`，对应机外标签 `2,4,6,8,12,14,16,18`。开始后使用已发布的不可变配方快照并锁定控件，任何时刻只打开一个气味通道。
9. 当前批准的起始配方为 Air、A=`1500 ml/min`、B=`0`、C=`0`、每路 `10 s`、`3` 轮；流量、时长和轮数可在经配置批准的上下限内调整。正常完成、用户中止或故障均先完成阀门安全关闭，再把 A/B/C 清零；清洗不得保留或恢复启动前的非零 setpoint。

## Tasks / Subtasks

- [x] Task 1：建立清洗领域模型、配置快照和操作身份（AC: #1, #3, #5）
  - [x] 新增不可变 `CleaningPlan`、`CleaningStep`、`CleaningOperationIdentity`、`CleaningSnapshot`、`CleaningResult`，状态固定为 `IDLE → PREPARING → RUNNING → STOPPING → COMPLETED`，并支持任意非终态进入 `FAILED → RECOVERY_REQUIRED`。
  - [x] 每个 step/command/receipt 显式携带 `operation_id`、`generation`、`step_id`、`command_id`、规范化 `target` 和 `action_kind`；不得用 protocol 的 `trial_id`/`execution_epoch` 冒充 maintenance identity。
  - [x] 在 `ActuationCategory` 中增加 `CLEANING`，保持现有 protocol/manual/pretest/master/safety 命令与持久化 schema 向后兼容；CLEANING receipt 不进入协议正常 jitter 质量窗口。
  - [x] 只从启动/保存后已发布的 immutable config snapshot 构造清洗计划，校验通道存在、目标唯一、至少选择一路、流量/次数/时长位于批准范围、步骤 ID 唯一；点击开始后锁定控件，计划不可被 UI 或配置热更新改变。
  - [x] 配方唯一真源仍是 `default_config.json + local_config.json` 递归合并结果；清洗页编辑的是 Story 4.2 的 candidate，保存后原子更新 `local_config.json` 的 `cleaning` 子树并发布新 snapshot。不得用 View 字段、`QSettings`、注册表或另一份偏好文件保存上次选择。
  - [x] 当前本机默认 route 映射为 `software_channel → external_label`：`2→2, 3→4, 4→6, 5→8, 6→12, 7→14, 8→16, 9→18`；这是软件逻辑通道与机外标号的显示映射，不得把机外标签直接当成 NI DO 线路。
  - [x] 当前默认 recipe 为 `gas=air`、`flow_setpoints_sccm={A:1500,B:0,C:0}`、`open_duration_s=10`、`cycles=3`、`selected_channels=[2,3,4,5,6,7,8,9]`、`parallel_open_limit=1`；A 的当前批准上限为 `1500 sccm`，不得因 UI 可调而允许无界输入。

- [x] Task 2：把 maintenance lease 建成单一、可审计的互斥门禁（AC: #1, #4）
  - [x] 把当前仅覆盖 `idle/protocol` 的散落 lease 字符串收敛为显式 lease kind/token/generation；原子 acquire/release，拒绝旧 token、重复释放和部分获取。
  - [x] 泛化 `FlowWorker` 当前的 protocol-only lease，同时保留现有 `acquire_protocol_lease()`/`release_protocol_lease()` 兼容 wrapper；maintenance operation 即使不改变 MFC setpoint，也必须阻断其他 serial/flow 业务流程获得冲突 lease。
  - [x] maintenance lease 与 protocol、manual/pretest、compensation/config-change 双向互斥；Controller/View 只能提交获取意图并渲染结果，不能成为 lease 真源。
  - [x] acquire 失败、recorder bind 失败、初始全关失败或 owner 未就绪时不得提交任何 CLEANING open。
  - [x] lease 只有在 operation 已终结、配置目标 close receipt 集合完整且相关 owner handoff 完成后才可释放；释放失败保持 fail-closed。

- [x] Task 3：扩展现有单写者为独立 maintenance bundle（AC: #2, #7）
  - [x] 为 `SessionWriterWorker` 增加显式 bundle kind/mode 或 maintenance descriptor/ingress；文件 owner 仍只有同一个 `SessionWriterWorker`，不得复制第二个 writer 或从 Controller 同步写文件。
  - [x] maintenance staging 目录在首次 open 前独占预留并写 ownership marker；只创建结构化 `.log` 与 `manifest.json`，不得创建实验 `.raw`，也不得使用实验 `session_id` 冒充 `operation_id`。
  - [x] `manifest.json` 至少包含 `schema=maintenance-v1`、operation identity、不可变配置/计划快照、step count、receipt count、producer fence、log byte/count/SHA-256、最终 operation 状态/outcome 和失败原因。
  - [x] maintenance expected producers 覆盖 `actuation`、`controller` 与 `flow`；maintenance 不记录 raw，因此不得伪造 `hardware` raw producer/fence。fence 之后同 generation 的任何旧事件必须被拒绝。
  - [x] 沿用现有 staging → fsync/close → manifest atomic replace → 单目录 publish、producer fence、hash/count/sequence 校验、ownership/recovery quarantine；成功用户中止可记录 `COMPLETED + outcome=aborted`，真正失败或恢复必需状态不得发布 `complete`。
  - [x] reserve/header/write/queue/flush/fsync/close/manifest/replace/rename 任一步失败时，先锁存 `recording_ready=False` 再通知动作 owner；禁止后续 open，`SAFETY` close 仍可执行，失败目录隔离且不可原地续写。
  - [x] 所有安全动作仍必须提交结构化 maintenance event/receipt；若永久 I/O 故障使其无法落盘，保留 immutable owner terminal snapshot、未持久化计数与失败原因供 recovery/诊断展示，绝不能另建旁路 writer 或把不完整 bundle 宣称为 complete。
  - [x] 复用“文件”页选择的本地实验输出根目录；未选择有效本地目录时中文拒绝启动。maintenance 根目录固定为 `<实验输出根目录>/maintenance/`，bundle stem 固定为 `{yyyyMMdd-HHmmss-fff}_cleaning_{operation_id}`，并复用现有路径预算、碰撞预留、ownership 与 recovery 规则；不得依赖活动实验 session，也不得写死开发机绝对路径。

- [x] Task 4：在 `ActuationWorker` owner 内实现 receipt 驱动的清洗状态机（AC: #3, #4, #5）
  - [x] 固定启动顺序：原子取得 maintenance lease → reserve/bind maintenance bundle → writer ready → 对全部配置目标提交 `SAFETY` close 并收到完整匹配 receipt → FlowWorker 写入并回读确认 A=`用户确认值（默认 1500）`、B=`0`、C=`0` → 主阀 open receipt → 进入 `RUNNING` 并提交 step 1；任一步失败均不得打开气味通道。
  - [x] 清洗 mutation、deadline queue、命令提交和 receipt 消费均在 `ActuationWorker` owner 路径；可把纯计划校验/纯状态转换放入 model/service，但禁止 UI `QTimer`、`sleep` 循环、临时线程或直接 HAL 调用。
  - [x] 复用现有 producer-safe queue/condition、注入式 monotonic ns clock、`ActuationDOAdapter`/HAL、`ValveService` 目标解析和紧急关闭能力；每一步以前一步匹配成功 receipt 为唯一推进条件。
  - [x] normal CLEANING open/close 均受连接、自检、flow readiness、气流联锁、recorder readiness、lease 与 generation 门禁；只有抢占式全关使用 `SAFETY` close。
  - [x] 正常序列严格为：按已确认顺序对单一通道 `open → 等待配置时长 → close receipt`，然后才进入下一通道；完成一轮后从第一通道重复，直至达到 cycles。不得并行打开两路，不设置额外通道间延时。
  - [x] 正常结束严格为：最后一个气味通道 close receipt → 主阀 close receipt → FlowWorker A/B/C=`0/0/0` 成功 receipt → owner handoff/finalize；stop/故障则先抢占式关闭全部 21 目标，再尽力提交并确认 A/B/C 清零，清零失败进入 `RECOVERY_REQUIRED` 且不得妨碍 DO 全关。
  - [x] duplicate receipt 幂等不重复推进；conflicting receipt、generation/step/command/target 任一不匹配均记录并 fail closed；stale successful close 可确认目标关闭但不得推进当前步骤。
  - [x] late successful open 必须加入 `possibly_open` 并立即发起匹配目标的 `SAFETY` close；close failure/timeout/uncertain 继续保留 `possibly_open`，进入 `RECOVERY_REQUIRED`。
  - [x] 所有 step start/end、command/receipt、elapsed、拒绝、stale/conflict、状态转换和安全动作都经 maintenance ingress 结构化记录。

- [x] Task 5：实现 stop、故障与 shutdown 的 21-target 安全收敛（AC: #4, #5, #6, #7）
  - [x] stop、`LOW_FLOW`、disconnect、recorder failure、owner failure 和 shutdown 首先 invalidate 当前 generation、停止新步骤，再抢占式提交全目标 `SAFETY` close。
  - [x] 目标集合从全部已配置 DO target 的去重并集生成：当前生产配置必须是 20 个气味阀目标 + 1 个主阀目标，共 21 个；不能只使用当前 10/20-channel View 的 active map，也不能硬编码线路字符串。
  - [x] 扩展或新增全配置目标 API，保留 `ValveService.emergency_close_steps()` 既有调用者行为；目标按 `(device, line)` 去重并保留可审计 logical target。
  - [x] 用户中止只有在 21-target close receipt 全部成功且 owner handoff 完成后才能显示“已安全停止”并终结为 `COMPLETED/aborted`。
  - [x] 普通中止的 handoff 是 maintenance operation/lease 从 Actuation/Flow owner 安全交回 idle，不要求停止长期 worker；只有 shutdown 才按固定顺序停止 worker 并释放 DO/AI/serial 资源。
  - [x] `LOW_FLOW`/disconnect/owner failure 终结为 `FAILED`；close timeout/uncertain、recorder/finalize failure 或 handoff 不完整进入 `RECOVERY_REQUIRED`。safe telemetry 不自动清除 unsafe latch，恢复必须由用户显式触发。
  - [x] shutdown 继续遵守现有顺序：禁止新提交/失效 generation → emergency close receipt 集合 → ActuationWorker/DO handoff → HardwareWorker/AI handoff → FlowWorker/serial handoff；不得跨线程复用已交还 task 做兜底写入。

- [x] Task 6：新增中文清洗页并由 Controller 只做编排（AC: #1-#7）
  - [x] 新增“清洗”标签页，View 只发 start/stop/recover/output intent，并渲染 immutable snapshot；禁止 View 直接读取 HAL、lease、writer 或可变 worker 状态。
  - [x] 页面显示当前状态、步骤/通道、剩余时间、maintenance lease、记录就绪、全关收敛进度、bundle 位置和恢复要求。
  - [x] 空闲页用 20 通道复选框矩阵（仅启用已配置通道）显示“软件通道 + 机外气路标签”，提供“全选已配置/清空”；首次无 local override 时预选当前默认八路，以后启动时显示上次成功保存的选择。至少选择一路才允许保存和开始。后续接线变化时，操作员可在不改代码的情况下选择所有实际在用气路。
  - [x] 提供“清洗气流（ml/min）”“每路时间（秒）”“循环轮数”三个带单位输入和预计总时长预览；默认 `1500 / 10 / 3`，预计八路约 `4 分钟`。数值只能落在配置批准范围内，运行中全部锁定。
  - [x] 开始前确认摘要明确显示：Air、A/B/C 目标、气路顺序（优先显示机外标签）、单路时间、轮数和输出位置；不得要求用户直接编辑 JSON 才能完成日常清洗。
  - [x] 提供“保存清洗配置”入口，调用 Story 4.2 的断开态校验/原子保存链，同时持久化已选气路、清洗流量、每路时间和轮数；成功后明确显示“已保存，关闭并重新启动软件后仍会保留”。保存时必须保留 `local_config.json` 中所有无关字段，失败则磁盘旧值和 active snapshot 均不改变。
  - [x] 页面维护明确的“已保存/有未保存修改”状态；存在未保存修改时禁止开始，并提示“请先保存或撤销修改”。连接中、活动 session/maintenance/lease、目标未全关或 owner 未 handoff 时禁止保存并说明需要先安全断开；不得把未保存字段作为只作用一次的隐式配方。
  - [x] 应用每次启动及 Story 4.2 成功发布新 snapshot 后，View 都从 effective config 重新渲染清洗设置；不得因关闭标签页、断开连接或清洗完成而恢复仓库默认值。
  - [x] 中止或失败后先显示“正在安全关闭”；只有目标 receipt 集合和 handoff 均完成后才显示“已安全停止”，不得依据按钮 disabled 或命令已提交提前承诺。
  - [x] 错误文本必须说明：发生了什么、系统采取了什么安全动作、用户下一步做什么；颜色只作辅助。
  - [x] Controller 负责顺序化 lease/bundle/owner intent 和 snapshot 转发，不直接执行 deadline、DO、同步文件 I/O 或清洗状态 mutation。
  - [x] 活动/准备中的实验 session 与 maintenance operation 必须双向拒绝启动；清洗不得绑定现有 session recorder，实验开始也不得复用活动 maintenance writer。

- [x] Task 7：补齐确定性自动化测试（AC: #1-#7）
  - [x] 覆盖 CC-01：cleaning step vs stop，断言 stop 抢占、旧 receipt 不推进、全目标关闭。
  - [x] 覆盖 CC-02：cleaning open ack vs `LOW_FLOW`，断言 late successful open → `possibly_open` → matching safety close。
  - [x] 覆盖 CC-03：maintenance recorder queue full vs open，断言 `recording_ready=False` 先于后续 open 且 safety close 可用。
  - [x] 覆盖 CC-04：maintenance finalize vs shutdown，断言 producer fence 前事件不丢、旧 generation/fence 后提交被拒绝、失败不发布 complete。
  - [x] 覆盖 duplicate/conflicting/stale/late receipt、close timeout/uncertain、lease 双向互斥、bind/readiness/initial-close 顺序、终态幂等和显式 recovery。
  - [x] 覆盖 maintenance 文件全 fault matrix、manifest schema/hash/count/sequence/fence、失败隔离与只读 recovery；不得仅用 `chmod` 证明 Windows 不可写。
  - [x] 覆盖生产配置目标并集恰为 21、active 10-channel 时仍关闭完整配置目标集、映射重复时去重且无未配置写入。
  - [x] 覆盖默认八路映射与顺序、单路互斥、10 s × 3 轮展开、A/B/C=`1500/0/0` 的 flow receipt 前无 odor open、终态 `0/0/0`、运行中配方不可变及未来增减选择通道无需改代码。
  - [x] 使用 fake monotonic clock、`Event`/`Barrier`、cancellation token、fake filesystem 和 fault injection 固定交错；毫秒 `sleep` 或重复运行不得作为唯一竞态证据。
  - [x] UI 测试验证 intent/snapshot/中文三段式错误与状态文字，并覆盖保存后重建 App/View 仍恢复上次气路/流量/时间/轮数、未保存时禁止开始、原子保存失败保留旧值和无关 local override；当前仓库未安装 `pytest-qt`，不得把本地 no-op `qtbot` fixture 宣称为真实 pytest-qt 覆盖。可使用现有 harness 与 `PySide6.QtTest`，依赖工具链统一留给 Story 4.3。

- [ ] Task 8：执行软件 Gate 与范围触发式真实 HIL（AC: #1-#7）
  - [x] 运行 `python -m pytest`、`python -m ruff check .` 和 `git diff --check`。
  - [ ] 在真实 Windows/NI 环境以 Air、无气味材料/容器、无受试者执行清洗 HIL；入口压力表约 `5 bar`，A 默认 `1500 ml/min`。逐路核对软件通道与机外标签时不得超过 `1500 ml/min` 批准上限；当前细管在该流量下无法由纸条或手感可靠判定，需使用不增加污染、背压或超限风险的外部检测手段，感受不明确不得猜测为通过。再以候选配方 `10 s × 3` 轮验证实际 step/receipt 顺序、stop、`LOW_FLOW`、disconnect、stale receipt、A/B/C 清零和最终 21-target 全关。
  - [ ] 因 maintenance bundle/writer/fence/finalize 被修改，在真实记录负载下验证 log/manifest hash、count、sequence、producer fence 和失败隔离 Gate。
  - [ ] 若修改 ActuationWorker queue/interlock/DO task/deadline 或 shutdown 顺序，重跑 200 open + 200 close、aggregate/rolling/final-last-100 p95 与 stop/LOW_FLOW/severe/shutdown 四类安全场景。
  - [ ] HIL 证据记录 candidate commit、clean/dirty worktree、Windows/Python/NI/COM 环境、硬件身份、配方快照、完整失败 run 与声明边界；`daqmx_write_ack` 不得宣称机械阀物理完成。

## Dev Notes

### 实施上下文与范围

- 本 Story 实现 FR6.1 的安全、可审计自动清洗，不实现 Story 4.2 的配置编辑/回滚 UI、Story 4.3 的全局本地化审计或 Story 4.4 的 flow → master → odor 补偿状态机。
- 推荐实施顺序是 4.2 → 4.1 → 4.4 → 4.3，但 4.1 可先消费现有启动配置合并结果；不得为绕开 4.2 另建数据库、第二份 JSON 或 UI 私有配置。
- `ProtocolTrial.timing_ms` 继续只解析/展示，不参与清洗或协议执行。清洗 duration/deadline 属于独立 `CleaningPlan`。
- maintenance bundle 不生成 `.raw`，不混入实验 session；实验 session、protocol epoch、jitter window 和已发布 bundle 的语义必须保持不变。
- 现场已确认清洗气体为 Air、入口压力表约 `5 bar`，当前无串接装有气味材料的瓶子、管子或容器。旧版 `ProgOlfacto` 手册的隐藏“Rinçage”页要求各电磁阀交替打开，并允许设置总时长与单阀时长；因此本 Story 明确禁止八路同时打开。
- 照片可确认可见 Alicat 的 `SETPT=1.50000 NLPM`、`Mass Flow≈1.4998 NLPM Air`，与既有真实 HIL 中 `alicat_flow_unit=A`、A=`1500 sccm`、B/C=`0` 的成功基线一致。清洗使用这一已验证基线，但不得根据安装前后位置猜测另外两台仪表的量程或状态。
- `5 bar` 是独立入口压力表读数；照片中的 `14.71 PSIA` 接近环境绝对压力，两者不是同一个测点。软件不得把 Alicat 屏幕 PSIA 当作入口供气压力，也不得声称仅凭该照片验证了所有支路压降。
- 配方决定已冻结为默认 `A/B/C=1500/0/0 ml/min`、单路 `10 s`、`3` 轮、仅单路依次打开；正常运行约 `8×10×3=240 s`，另加命令/回执开销。操作员可在批准范围内于清洗页修改并持久化本机配方；以后启动自动恢复上次成功保存值，开始清洗后以 immutable snapshot 执行。

### 架构不变量

- `HardwareWorker` 是唯一 AI0/AI6 continuous task owner。
- `ActuationWorker` 是唯一 `ProtocolExecutor`、`GatingService`、动作质量窗口和全部 DO owner；清洗硬件状态机必须进入该 owner 路径。
- `FlowWorker` 是唯一 Alicat serial owner。清洗页只提交期望值；Controller 不直接写串口，Actuation owner 以 maintenance identity 编排 FlowWorker 的 setpoint/receipt。清洗独立 flow 阶段不得复用 protocol phase identity，也不得改变 Story 4.4 的协议 flow → master → odor 顺序。
- `SessionWriterWorker` 是实验 session 与 maintenance bundle 的唯一文件 owner。
- Controller 只编排 immutable intent/result；View 只发布意图并渲染 snapshot。
- `SAFETY` 仅用于 close。普通清洗 open/close 均为 `CLEANING`，不能借类别绕过 interlock、lease 或 recorder。

### 现有实现检查与回归保护

| 文件/符号 | 当前状态 | 本 Story 变化 | 必须保留 |
|---|---|---|---|
| `app/models/actuation.py` | `ActuationCategory` 尚无 `CLEANING`；command/receipt 以 protocol identity 为主 | 新增 CLEANING 与 maintenance identity/schema | 现有 protocol receipt、measurement point、quality 统计兼容 |
| `app/workers/actuation_worker.py` | 已有 producer-safe interlock、deadline/emergency queue、stale/duplicate处理、`possibly_open`、DO handoff；lease 主要是 `idle/protocol` | owner 内新增 cleaning state/lease/receipt 消费与 recorder ingress | 协议 deadline、normal jitter window、安全优先队列、全部 DO 单写 |
| `app/workers/flow_worker.py` | 唯一 serial owner；lease 仅支持 `idle/protocol` 与 protocol epoch | 泛化带 kind/operation/generation 的独占 lease并保留 protocol wrapper | Alicat 单写、已有 flow intent/result 与 shutdown release |
| `app/services/valve_service.py` | `emergency_close_steps()` 只基于当前 active variant + master | 提供全部配置目标并集/审计 target API，供初始与终态全关 | View 的 active map、现有 manual/pretest plan、target 去重 |
| `app/models/session.py` / recorder latch | descriptor/readiness 字段以 `session_id` 命名 | maintenance identity 使用独立 descriptor/latch 或明确 bundle identity 抽象 | 不用伪 session ID 冒充 operation ID；实验 session v1 不变 |
| `app/workers/session_writer.py` | 初始化强制创建 `.raw/.log/manifest`，schema/producer 面向实验 session | 显式 maintenance log-only 模式、maintenance ingress/fence/finalize | 单写、有界队列、先锁存 failure、hash/count/sequence、事务 publish |
| `app/services/session_file_service.py` | 已有 Windows NFC/路径预算、碰撞预留、ownership marker、严格 validator/recovery | 抽取可复用 bundle primitive 或新增 maintenance 专用 reserve/validator | 不误处理用户目录；失败不自动续写、补全或删除 |
| `app/controllers/main_controller.py` | 编排 session recorder、protocol lease、interlock、safe stop/shutdown | 编排 cleaning intents、bundle bind、snapshot 和终态 | 不直接写 DO/serial/file；现有 session/protocol 启停不回归 |
| `app/views/main_window.py` | 仅有概览/文件/校准/预检/协议 | 注册独立 CleaningView 标签页及 signal/snapshot | 全局停止/状态栏、安全状态持续可见 |
| `config/default_config.json` | 无清洗配置 | 加入已确认的 cleaning schema、默认值和批准上限；本机标签/默认选择可由 local override 覆盖 | `default + local` 递归覆盖唯一真源；不写开发机绝对路径 |

### 建议文件结构

预计新增：

- `app/models/cleaning.py`
- `app/services/cleaning_executor.py`（可选，仅纯计划校验/纯转换，无 HAL/线程）
- `app/views/cleaning_view.py`
- `tests/test_cleaning_state_machine.py`
- `tests/test_cleaning_view.py`
- `tests/test_maintenance_writer.py`

预计更新：

- `app/models/actuation.py`
- `app/models/__init__.py`
- `app/services/valve_service.py`
- `app/services/session_file_service.py`
- `app/workers/actuation_worker.py`
- `app/workers/flow_worker.py`
- `app/workers/session_writer.py`
- `app/controllers/main_controller.py`
- `app/views/main_window.py`
- `app/views/__init__.py`
- `config/default_config.json` 与 `config/local_config.example.json`（仅在配方字段确定后）
- 对应的现有 actuation/session/shutdown/integration tests
- `scripts/hil_cleaning_gate.py` 与对应测试（若不扩展现有 HIL 脚本）

路径不是授权大规模重写。优先扩展已有 owner、queue、ingress、bundle 与 recovery primitive；如果抽公共 primitive，现有 session tests 必须保持全绿。

### Cleaning 配置契约

配置键名可在实现时按项目命名约定微调，但语义必须保持单一且可校验：

```json
{
  "cleaning": {
    "enabled": true,
    "gas_label": "Air",
    "flow_channel": "A",
    "default_flow_sccm": 1500,
    "max_approved_flow_sccm": 1500,
    "fixed_flow_setpoints_sccm": {"B": 0, "C": 0},
    "default_open_duration_s": 10,
    "default_cycles": 3,
    "parallel_open_limit": 1,
    "default_channels": [2, 3, 4, 5, 6, 7, 8, 9],
    "external_labels": {
      "2": "2",
      "3": "4",
      "4": "6",
      "5": "8",
      "6": "12",
      "7": "14",
      "8": "16",
      "9": "18"
    }
  }
}
```

- 可选通道集合来自 active valve mapping，不在映射中的通道不得启用；`external_labels` 只负责人类可读显示，fallback 为软件通道号，不参与 DO target 解析。
- UI 的单一“清洗气流”字段只改变 A；B/C 固定为 0，避免要求操作员理解未确认的另外两台 Alicat。输入必须为有限正数且 `<= max_approved_flow_sccm`；当前批准上限仅为 1500，未来提高前必须基于仪表量程与真实支路验证更新本机批准值。
- `parallel_open_limit` 当前必须等于 1；配置为其他值时视为无效并阻断启动，不能把它当成开放并行清洗的隐藏开关。
- 每路时间和轮数必须为有限正值；实现需设置合理的 UI/配置上限并在超出时阻断，不能用极大值造成无法合理停止的操作。
- `default_config.json` 提供首次使用默认值；每次成功保存只在 `local_config.json` 的 `cleaning` 子树写入覆盖值。下次启动按既有递归合并规则自动恢复，不要求操作员再次设置。
- 清洗配置保存复用 Story 4.2 的同目录临时文件 → flush/fsync → atomic replace → 发布 snapshot 事务；不得只修改内存，也不得整文件重写而丢失 COM、NI、校准等本机字段。
- 选择变化或清洗配置保存不改变 valve mapping；实际重新接线仍须由 Story 4.2 更新/校验映射与机外标签，并重新执行 `2 s × 1` 轮手感气流映射验证。该验证只证明“有无出气”和软件通道/机外标签对应，不证明流量、压力或机械动作完成。

### Library / Framework Requirements

- 保持项目锁定版本：Python 3.11、PySide6 6.7.2、pyqtgraph 0.13.7、NumPy `>=2.0,<2.1`、nidaqmx 0.9.0、pyserial 3.5、pytest 7.4.4、ruff 0.6.5、PyInstaller 6.3.0。
- 本 Story 不需要新第三方库，也不应顺带升级 PySide6/nidaqmx。官方仓库已有更高版本，但升级与清洗功能无关并会扩大 HIL/打包验证范围。
- deadline 使用注入式 monotonic ns clock；绝对参考点不得作为墙钟时间持久化，只记录差值/elapsed，并另用 wall clock 记录审计时间。
- Qt `QThread` 对象本身属于创建它的线程；直接调用 `QThread` 对象方法不会自动切换到 worker thread。继续使用现有 producer-safe queue/condition/immutable ingress，不能通过“给 QThread 加 slot”绕开 owner。
- NI 写入继续封装在 HAL/`ActuationDOAdapter`。on-demand `Task.write(auto_start=False)` 的既有持久 task 路径与测量点不得因 Story 4.1 改回每步建 task。

### Testing Requirements

最低测试矩阵：

| 类别 | 必须断言 |
|---|---|
| 启动门禁 | lease → bundle bind/ready → 21-target initial close receipts → first open；任一步失败零 open |
| 身份 | generation/step/command/target/action 全匹配才推进；duplicate 幂等，conflict/stale fail closed |
| 抢占 | stop/LOW_FLOW/disconnect/recorder/owner failure invalidate 后无新 CLEANING step |
| 不确定状态 | late successful open、write exception、close timeout 保留 `possibly_open` 并安全收敛 |
| 记录 | log-only maintenance-v1；完整 event/receipt/fence/hash/count；失败不发布 complete/不续写 |
| lease | protocol/manual/pretest/compensation/config-change 与 cleaning 双向拒绝 |
| 目标集 | 从全部配置映射并集 + master 去重，当前生产配置恰为 21；active 10-channel 不得漏关其他已配置目标 |
| 配方 | 默认逻辑通道 2–9 对应机外标签 2/4/6/8/12/14/16/18；一次仅一路；10 s × 3 轮；A/B/C=1500/0/0 后才开主阀/气味阀 |
| 配方编辑 | 断开安全态可选实际使用气路和批准范围内的流量/时长/轮数并原子保存；重启自动恢复；开始后不可变；未保存、零选择、未映射、越界或非有限值零硬件副作用 |
| flow 终态 | 正常/中止/故障均先关阀，再将 A/B/C 清零；flow receipt 失败不得显示安全完成 |
| 终态 | safe aborted 与 failed/recovery 分开；close receipts + handoff 未完成不得显示安全终态 |
| UI | 意图与 snapshot 单向流；中文状态与三段式错误；颜色不作为唯一状态 |

### Epic 3 / Git Intelligence

- Epic 3 已建立四条可直接复用的链：immutable identity、唯一 owner、receipt 驱动全关、producer fence/事务 publish。Story 4.1 不能在其旁边再建临时线程、文件 owner 或 UI 状态机。
- 最近提交表明真正风险在 owner dispatch、首动作 backlog、全目标收敛、duplicate/conflicting receipt 与 recorder failure 顺序，而不是“多写几个测试”。实现时保留已有安全优先队列和失败先锁存 readiness 的模式。
- Story 创建时 `HEAD=49fd99e`；Epic 4 冻结文档及相关 architecture/epics/ux/sprint-status 更新位于用户当前未提交工作树。不得覆盖或回退这些改动，也不得把它们宣称为已提交 candidate。
- Epic 4 没有上一条已实现 Story；本 Story 的直接实现基线是 Story 3.5 的 owner/session/HIL 闭环与 Epic 3 retrospective。

终态提交顺序必须避免循环依赖：停止新步骤/失效 generation → 完整全关 receipt 集合 → maintenance lease/owner handoff → producer fences 与 bundle finalize/publish → 对外发布终态。任一步失败进入 `FAILED` 或 `RECOVERY_REQUIRED`，不得提前显示成功；recorder 已失败时跳过 complete publish，但仍必须完成安全全关。

### Latest Technical Information

- Python 3.11 的 monotonic ns clock 不会倒退，适合本地 deadline；只比较差值，不把 reference point 当作时间戳保存。[Python 3.11 `time.monotonic_ns`](https://docs.python.org/3.11/library/time.html#time.monotonic_ns)
- Qt 官方建议永久 worker 通过 queued signal/slot 或明确的 worker queue 接收跨线程命令；`QThread` 实例的方法仍在调用线程执行。[Qt for Python: Multithreading Technologies](https://doc.qt.io/qtforpython-6/overviews/qtdoc-threads-technologies.html) [PySide6 QThread](https://doc.qt.io/qtforpython-6.8/PySide6/QtCore/QThread.html)
- NI 文档说明反复 start/stop task 会降低循环写入性能；继续复用当前持久 DO task 和 `auto_start=False` 路径。[NI nidaqmx Task API](https://nidaqmx-python.readthedocs.io/en/stable/task.html)
- 版本核验日为 2026-07-31：PyPI 的 PySide6 当前版本高于项目锁定的 6.7.2；本 Story 明确不升级。[PySide6 on PyPI](https://pypi.org/project/PySide6/)

### Project Context Reference

- 项目长期规则：[Source: docs/project-context.md#架构原则]
- 产品需求：[Source: docs/prd.md#FR6清洗]
- Story 与 Epic 4 上下文：[Source: docs/epics.md#Story-41-自动清洗流程]
- 架构与冻结摘要：[Source: docs/architecture.md#Epic-4-冻结技术边界]
- 清洗 UX：[Source: docs/ux-design.md#清洗页]
- 旧版软件清洗行为：[Source: docs/ManuelUtilisation_ProgOlfacto.pdf，第 34 页，Onglet Rinçage]
- 权威冻结边界：[Source: docs/sprint-artifacts/epic-4-technical-boundary-2026-07-31.md#3-Story-41--CLEANING-状态机冻结]
- 并发测试与 HIL：[Source: docs/sprint-artifacts/epic-4-technical-boundary-2026-07-31.md#7-确定性并发测试清单] [Source: docs/sprint-artifacts/epic-4-technical-boundary-2026-07-31.md#8-真实-WindowsNI-HIL-触发矩阵]
- Epic 3 经验：[Source: docs/sprint-artifacts/epic-3-retro-2026-07-30.md#第二部分Epic-4-Preparation]
- 变更批准：[Source: docs/sprint-artifacts/sprint-change-proposal-2026-07-31.md#Detailed-Change-Proposals]

### 开发前人工决定（已确认）

| 项目 | 决定 |
|---|---|
| 清洗介质/现场条件 | Air；入口压力表约 5 bar；当前气路不串接气味材料瓶、管或容器 |
| 打开方式 | 任何时刻只开一个气味通道，按所选顺序循环；禁止八路同时打开 |
| 当前默认气路 | 软件通道 `2,3,4,5,6,7,8,9`；对应机外标签 `2,4,6,8,12,14,16,18` |
| 默认时间 | 每路 10 s，3 轮；八路理论吹扫时间 240 s |
| 默认 flow | A=1500 ml/min，B=0，C=0；照片与既有真实 HIL 均支持 A≈1500 ml/min 基线 |
| 运行前后 | 无人工操作气瓶/泵/手动阀；软件先全关并确认，再设 flow、开主阀、逐路清洗；结束先关阀，再 A/B/C 清零 |
| 操作员调整 | 清洗页复选实际使用气路并调整批准范围内的流量、每路时间、轮数；通过 Story 4.2 断开态事务保存到现有 `local_config.json`，以后每次启动自动恢复；开始后锁定为不可变快照 |
| 现场映射验证 | Air、无气味、无受试者；不得超过 `1500 ml/min` 批准上限。当前细管在该流量下手能感到气流但不够明显，纸条也难以使用，因此手感只作辅助观察；任何不明确结果均不得猜测为通过，正式映射须使用不引入污染、背压或超限风险的外部检测手段 |
| bundle 根目录 | 当前“文件”页选择的本地实验输出根目录下 `maintenance/`；未选择则拒绝启动 |
| bundle 命名 | `{yyyyMMdd-HHmmss-fff}_cleaning_{operation_id}` |
| bundle 发布 | 正常完成发布 complete；确认全关的用户中止发布 `COMPLETED/outcome=aborted`；失败进入 recovery/quarantine，绝不伪装 complete 或原地续写 |

以上决定只填充已定义的 immutable config/plan，不改变 `maintenance-v1`、owner topology、lease 互斥、21-target 全关、timing/HIL 等冻结边界；今后若接线增加，只调整已配置通道的显示标签/默认选择并重新做手感气流映射验证，无需修改状态机。

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Debug Log References

- 2026-07-31 Task 1 RED：`tests/test_cleaning_models.py` 因缺少清洗模型导入失败；Task 1 GREEN：清洗模型、配置契约及原子保存测试通过。
- 2026-07-31 Task 1 回归：`python -m pytest -q` → 650 passed。
- 2026-07-31 Task 2 RED：缺少显式 lease kind/token 与 maintenance gate；首次完整回归暴露 protocol epoch 原子换代兼容问题（12 failures），修复为仅当前持有者可 renew。
- 2026-07-31 Task 2 回归：`python -m pytest -q` → 655 passed。
- 2026-07-31 Task 3 RED：缺少 maintenance descriptor/readiness/ingress 与 log-only 文件预留；GREEN 后补齐 `maintenance-v1` manifest、事务发布、校验与 recovery quarantine。
- 2026-07-31 Task 3 回归：`python -m pytest -q` → 658 passed。
- 2026-07-31 Task 4 RED：缺少 `ActuationWorker` 清洗 intent/snapshot/result API；GREEN 后以 fake monotonic clock 验证 initial close → flow → master → odor deadline → close → zero 顺序。
- 2026-07-31 Task 4 回归：`python -m pytest -q` → 661 passed。
- 2026-07-31 Task 5 RED：active 10-channel 缺少完整配置目标 API，且 shutdown 未显式请求 A/B/C 清零；GREEN 后验证 21-target 去重并集、LOW_FLOW/stop 抢占、late open、uncertain close 与显式 recovery。
- 2026-07-31 Task 5 回归：`python -m pytest -q` → 667 passed。
- 2026-07-31 Task 6 RED：MainWindow 尚无清洗页与 Controller handlers；首个端到端测试进一步暴露 maintenance lease 被周期 interlock 刷新覆盖，以及 maintenance receipt 触发 protocol 递归 drain 后提前释放 DO ownership。
- 2026-07-31 Task 6 GREEN：中文 intent/snapshot View、断开态配置保存/重载、Controller maintenance 编排和完整 bundle 端到端测试通过；`tests/test_cleaning_view.py + test_session_view.py + test_app.py` → 95 passed。
- 2026-07-31 Task 7：以 fake monotonic clock、`threading.Event` 与 writer/service fault injection 固定 CC-01～CC-04；补齐 maintenance reserve/finalize 15-stage fault matrix、生产默认配方和 UI 重建持久化断言。
- 2026-07-31 Task 7 定向 Gate：清洗/lease/writer/UI/flow/shutdown 相关 69 项测试通过。
- 2026-07-31 Task 8 软件 Gate：新增专用 `scripts/hil_cleaning_gate.py` 及测试；最终 `python -m pytest -q` → 699 passed，`python -m ruff check .` 与 `git diff --check` 通过。
- 2026-07-31 Task 8 HIL preflight：Windows 10 / Python 3.11.15；检测到 `Dev1 = USB-6001/SN 34887710`、`Dev2 = USB-6001/SN 34887797` 与 `COM6 = ATEN USB to Serial Bridge`。用户已确认机器气路连接完成、介质为 Air、入口约 `5 bar`、无气味材料或容器、无受试者，并授权 live DO/serial 控制；已按 `config/local_config.example.json` 建立被 gitignore 排除的本机 real 配置。用户确认 `1500 ml/min` 时手能感觉到气流，但可能因不明显而判断不准，因此手感仅作辅助观察、不作为明确映射通过证据，也不得为获得更明显手感而超过 `1500 ml/min` 批准上限。
- 2026-07-31 Task 8 live close-only：`logs/benchmarks/story-3-4-20260731-193421-live/summary.json` 记录 21/21 配置目标 `daqmx_write_ack` 成功、无缺失或失败目标；该证据只证明 NI 写入确认，不宣称机械阀物理完成。
- 2026-07-31 Task 8 HIL runner smoke：生产 Controller/Actuation owner/Flow owner/maintenance writer 的 simulation stop、`LOW_FLOW`、disconnect 场景通过。Smoke 首次暴露“startup zero 后必为 LOW_FLOW、清洗却只接受 SAFE”的真实启动死锁；修复为只允许已确认 startup zero 的 LOW_FLOW 进入初始全关/flow 阶段，setpoint 成功后最长等待 5 s，只有实际气流恢复 SAFE 并清除锁存才允许 master/odor open，超时则全关清零。stop 完整 bundle 校验通过；`LOW_FLOW`/disconnect 均 21/21 全关、A/B/C=`0/0/0`、无 possibly-open，并按失败隔离规则保留 staging、不发布 complete。
- 2026-07-31 Task 8 live cleaning：首次真实 run 在开阀前因 setpoint 串口事务期间 flow sample 暂态 `DATA_STALE` 而阻断，并暴露 STOPPING 被重复 unsafe update 重入、不断重建全关集合的问题；修复为 master 关闭的准备态可在 5 s 上限内等待 fresh SAFE，且 STOPPING 幂等。独立 close-only 与 Alicat 只读检查确认异常后 21/21 全关、A/B/C setpoint=`0/0/0`、A mass flow=`0`。
- 2026-07-31 Task 8 live cleaning：修复后两次约 `0.5 s` 与一次约 `5 s` 的通道 2 短停 run 均通过电子/记录 Gate：实际 open→close 分别为 `513.0346 ms`、`501.1341 ms`、`5009.3851 ms`；终态均为 `completed/aborted`，21/21 全关、A/B/C=`0/0/0`、无 possibly-open，完整 `maintenance-v1` bundle 的 hash/count/fence validator 通过。但用户三次均未感觉到机外标签 2 出气，因此软件通道 2 / 机外标签 2 的物理映射验证明确失败；不得用电子回执扩称机械阀动作或指定出口出气。完整证据见 `docs/sprint-artifacts/evidence/story-4-1-hil-exploratory-20260731.md`。

### Implementation Plan

- 按 Story Task 顺序采用 red-green-refactor；模型与纯校验先行，随后依次扩展 lease、单写者 bundle、Actuation owner 状态机、安全收敛、Controller/View，最后补齐竞态 Gate 与 HIL 证据。
- 清洗操作使用独立 `operation_id + generation` 命名空间；协议 `execution_epoch/trial_id` 仅保留兼容字段，不作为 maintenance 身份。
- 本机清洗配置仅写入现有 `local_config.json` 的 `cleaning` 子树，先验证 candidate，再同目录 fsync/atomic replace，成功后才发布新 immutable snapshot。

### Completion Notes List

- Ultimate context engine analysis completed - comprehensive developer guide created.
- Task 1：新增不可变清洗配置、计划、步骤、运行快照和结果模型；加入 `CLEANING` 动作类别与 command/receipt maintenance identity。
- Task 1：加入默认八路 Air 配方、批准上限校验及保留无关字段的 `local_config.json` 原子保存/发布链；完整回归 650 项通过。
- Task 2：引入单一 `ExclusiveDeviceLease`，支持 protocol/maintenance/manual/pretest/compensation/config-change 双向互斥、精确 token 释放和持有者原子换代；maintenance 释放需终态、全关、handoff 三项证据。
- Task 3：同一个 `SessionWriterWorker` 新增 maintenance descriptor/ingress 分支，生成无 `.raw` 的 `maintenance-v1` bundle；producer fence、hash/count/sequence、失败隔离、immutable 终态诊断和 recovery quarantine 均已覆盖。
- Task 4：`ActuationWorker` 新增 receipt 驱动清洗 owner 状态机，复用既有安全优先队列、注入式 monotonic deadline 与 DO adapter；只有完整匹配 identity 的成功回执可推进，正常序列严格保持单通道。
- Task 5：新增全部配置 DO 目标去重 API（生产映射 21 个目标），stop/LOW_FLOW/disconnect/recorder/shutdown 抢占后先全关再清零；不确定关闭保留 `possibly_open`，仅显式 recovery 可重新收敛。
- Task 6：新增中文清洗标签页、20 通道/机外标签矩阵、配方确认与不可变运行快照；Controller 只编排 lease、maintenance writer 与 owner intents，并在终态 producer fence 后异步发布 bundle。
- Task 6：配置仅允许断开安全态原子保存并从 effective config 重载；修复周期 interlock 对 maintenance lease 的覆盖与 maintenance receipt 递归 drain 的 DO ownership 交错。
- Task 6：清洗配置写盘由后台事务线程调用唯一 `CleaningConfigStore`，Controller/View 只编排并渲染完成信号，避免 UI/Controller 同步文件 I/O。
- Task 7：CC-01～CC-04 均成为确定性命名测试；recorder 在 `step_started` 入队失败时，Actuation owner 会在气味阀 open 提交前转入安全停止。
- Task 7：maintenance writer/reserve 全故障矩阵均断言不发布 complete，并验证 fence 前事件、fence 后拒绝、hash/count/sequence 与 recovery 保留。
- Task 8：新增可区分正式 clean-candidate Gate 与 dirty-worktree exploratory run 的清洗 HIL runner，记录 source diff hash、硬件身份、配方、状态轨迹、bundle/failure staging 与声明边界。
- Task 8：修复真实启动才会暴露的 zero-flow `LOW_FLOW` 恢复路径；清洗仅可从已确认 startup zero 的 LOW_FLOW 进入，主阀必须等待实际 Air 流量恢复 SAFE，5 s 未恢复则 fail closed。

### File List

- .gitignore
- app/models/actuation.py
- app/models/cleaning.py
- app/models/lease.py
- app/models/session.py
- app/models/__init__.py
- app/services/cleaning_config_store.py
- app/services/session_file_service.py
- app/services/shutdown_service.py
- app/services/valve_service.py
- app/services/__init__.py
- app/controllers/main_controller.py
- app/views/cleaning_view.py
- app/views/main_window.py
- app/views/__init__.py
- app/workers/actuation_worker.py
- app/workers/flow_worker.py
- app/workers/session_writer.py
- config/default_config.json
- config/local_config.example.json
- docs/sprint-artifacts/4-1-cleaning-automation.md
- docs/sprint-artifacts/evidence/story-4-1-hil-exploratory-20260731.md
- docs/sprint-artifacts/sprint-status.yaml
- tests/test_cleaning_config_store.py
- tests/test_cleaning_models.py
- tests/test_cleaning_state_machine.py
- tests/test_cleaning_view.py
- tests/test_maintenance_lease.py
- tests/test_maintenance_writer.py
- tests/test_flow_worker.py
- scripts/hil_cleaning_gate.py
- tests/test_hil_cleaning_gate.py
