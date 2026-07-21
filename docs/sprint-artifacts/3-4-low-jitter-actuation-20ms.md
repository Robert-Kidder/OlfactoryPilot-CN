---
baseline_commit: 8b8c126553ec915c034c6d3150d937b18c632986
---

# Story 3.4: 低抖动阀门动作（<20ms）

Status: ready-for-dev
Epic: 3 - 协议执行与数据记录
Story Key: 3-4-low-jitter-actuation-20ms
Story ID: 3.4
Depends On: Story 3.3 review patch（已由提交 `3983c58`、`8b8c126` 完成）

## Story

作为实验室技术人员，
我需要阀门动作软件抖动小于 20ms，
以保证实验时序可靠，并在系统负载或硬件写入延迟异常时得到明确告警和安全降级。

## Acceptance Criteria

1. **统一且可复现的动作时序定义**
   - Given 一个合法 trial 已通过 manual/TTL 触发并进入 Story 3.2 的呼吸门控；
   - When 校准后的 AI0 样本实际使 executor 首次锁存当前 trial 的 pending open（包括新 `EXHALE` transition，以及 Story 3.2 已支持的 gating 已处于 `EXHALE` fallback）；
   - Then 该次判定所使用的具体样本携带的 `monotonic_ns` 是目标气味阀开启动作的 `expected_open_ns`，不得在 Controller/UI 收到 signal 时重新打时间戳。
   - And `started_*_ns` 与 `actual_*_ns` 必须由 HAL DO adapter 在目标通道 on-demand `Task.write()` 调用前、成功返回后立即采集并装入结构化结果；ValveService、Worker、Controller 不得重新打 actual 时间。`expected_close_ns = actual_open_ns + duration_ns`；`actual_close_ns` 是关闭写入成功返回后的 HAL ack 时间。
   - And 每次正常协议动作记录带符号的 `offset_ms = (actual_ns - expected_ns) / 1e6` 与用于质量门的 `jitter_ms = abs(offset_ms)`；所有阈值比较使用未取整值，UI 仅在比较后格式化显示。
   - And `actual_duration_ms = (actual_close_ns - actual_open_ns) / 1e6`；现有墙钟 `timestamp` 继续用于人类日志和 Story 3.5 关联，但系统校时不得改变 deadline、duration 或 jitter。
   - And `actual_*` 的测量点必须明确标记为 `daqmx_write_ack`，不能宣称为机械阀物理动作完成；物理延迟验证需要独立 loopback/传感器，不得用软件回执替代。
   - And 时间序列必须满足 `expected_ns <= started_ns <= actual_ns`、`expected_close_ns >= actual_open_ns`、`actual_duration_ms >= 0`；违反时产生 measurement fault、进入 `BLOCKED` 并排除质量样本，不能简单取绝对值掩盖时钟错误。
   - And `duration_ms` 延续现有“有限且 >0”校验，并以 `duration_ns = int(round(float(duration_ms) * 1_000_000))` 转换；结果必须 >0，覆盖小数/亚毫秒及过大值边界。
   - And 为保持 Story 3.2 已建立的语义，本故事不静默重解释当前尚未参与执行的 `ProtocolTrial.timing_ms`；本故事的开阀 deadline 是呼气放行样本时刻，`timing_ms` 的未来 onset 语义需另行产品决策。

2. **关键执行路径脱离 UI 线程**
   - Given 协议正在等待呼气或执行刺激；
   - When UI 绘图、窗口事件或日志显示发生延迟；
   - Then 从 `EXHALE` 样本到 DO 写入、从开阀确认到定时关阀的关键路径不得依赖 `MainController` 的 50ms `QTimer`、UI event loop 或 UI signal handler 才能继续。
   - And 新增专用的单写者动作 Worker（`ActuationWorker` 或等价组件），使用单调 deadline、线程安全队列/condition 和稳定 sequence 排序；不得以 UI `QTimer` 或忙等循环作为最终调度方案。
   - And ActuationWorker 是单一 `ProtocolExecutor`、现有 `GatingService`、唯一 `ActuationMetrics` 实例和协议动作状态的唯一 owner；只有该线程可修改 `ProtocolExecutionState`、门控状态、质量窗口或 severe latch，不得复制第二套状态机/阈值算法，也不得由 Controller 并发调用 executor。
   - And 固定消息流为：`HardwareWorker -> ActuationWorker` 直送 AI batch、TTL pulse/input error 与原始 telemetry；`MainController -> ActuationWorker` 提交 load/start/manual/mode/pause/stop/rearm 及 flow intent；ActuationWorker 先做设备租约/状态授权，再把获准的 flow command 交给 serial owner/FlowService，结果按同一 command identity 返回；ActuationWorker 向 Controller 发 frozen result/snapshot/receipt。Controller 不读取可变 executor state，FlowService 不得先写串口再事后上报授权。
   - And ActuationWorker 根据 executor 状态直接向 HardwareWorker 发有序 `arm_ttl(arm_epoch)`/`disarm_ttl()`，HardwareWorker 在 detector 锁内执行并返回 ack；只有匹配 epoch 的 arm ack 后 snapshot 才可显示 `ttl_armed=True`。stop/pause/mode switch/disconnect 先递增 execution/arm epoch 并提交 disarm，任何在途旧 pulse 仍按捕获 epoch 拒绝。
   - And 开阀成功后由动作 Worker 自行按 `actual_open_ns + duration_ns` 安排关闭，不能等待 Controller 收到开阀回执后才安排。
   - And Controller 只提交用户意图、消费不可变回执、更新日志与 View；View 只显示状态和发出 intent。
   - And 呼吸等待 timeout 与刺激 deadline 都由 ActuationWorker 的同一单调 deadline 队列处理，并以 `(deadline_ns, priority, sequence)` 确定稳定顺序；50ms UI timer 只能请求/显示快照，不得修改 executor、触发 timeout 或关闭阀门。

3. **所有数字输出使用单写者通道**
   - Given 手动阀矩阵、预检序列、协议动作、FlowService 主阀、stop/reset/disconnect/shutdown 都可能请求 DO 写入；
   - When 任一路径发出阀门动作；
   - Then 所有正常 DO 写入必须经同一 ActuationWorker/队列，按 `ValveService` 的映射/安全计划进入 ActuationWorker 所有的 HAL DO session；`HardwareWorker` 不再参与 DO 调用链，也不得从 UI 线程或任意后台 `threading.Thread` 直接同步调用 `RealHAL.write_digital()`。
   - And `HardwareWorker` 继续独占 AI0/AI6 连续采集，ActuationWorker 独占 DO task；同一 DAQ task 不得跨线程使用，AI 与 DO 子资源的所有权必须明确。
   - And 协议运行期间，手动/预检的新开阀请求应由设备租约拒绝并给出中文原因；安全关闭、stop 和 shutdown 不受该租约阻断。
   - And 设备租约还必须拒绝会改变协议 readiness 的预检/手动 MFC setpoint、FlowService sequence 与主阀 normal 操作；已获准的协议流量配置保持，任何失败/断连仍可直达 interlock 并阻断 open。
   - And 队列满或 Worker 繁忙时普通 open 必须 fail closed 并使协议进入 `BLOCKED`；紧急关闭队列必须预留容量、可按 channel 合并且永不静默丢弃。
   - And shutdown 优先请求 Worker 执行 `emergency_close_all` 并等待有界确认；只有 ActuationWorker 明确停止并完成 DO ownership handoff/释放后，shutdown 才能在新 owner 中重建 DO session 做兜底关闭。若 Worker 卡在 DAQ 调用、无法交还 ownership，则记录关闭失败、将硬件标记不可用并要求人工确认，禁止跨线程复用旧 task 或无限等待 mutex。
   - And 该迁移是 Story 3.4 为消除并发 DAQ task 竞争、建立可证明低尾延迟所必需的工程使能与回归范围；不得借此提前实现 Story 4.4 的静息/刺激主阀自动化业务规则。

4. **动作命令、回执与状态机原子关联**
   - Given 任何正常或安全动作已提交；
   - When Worker 执行、取消或返回结果；
   - Then 使用 frozen/不可变 command 与 receipt，至少携带 `command_id`、`execution_epoch`、`arm_epoch`、稳定 sequence、trial id/index、valve、`open|close`、normal/safety 分类、expected/started/ack 单调时间、墙钟时间、offset、jitter、result 和 measurement point。
   - And `ProtocolExecutionState` 增量记录 pending command/可能已打开的阀；只有匹配当前 epoch 与 command identity 的成功回执才能推进正常状态机。
   - And 首次合法样本提交 open 时，executor 必须在同一状态提交中立刻写入唯一 `pending_open_command_id` 并离开可重复提交分支；在对应 receipt/取消收敛前，后续 EXHALE batch 不得产生第二条 open。close pending 同理不得被 tick/deadline 重复提交。
   - And 新增独立单调 `execution_epoch`（每次成功新运行以及 stop/pause/mode switch/protocol replace/safety invalidation 时递增）用于动作身份；现有 `arm_epoch` 继续只承担 trigger/TTL 布防代次。每条 protocol command/receipt 必须同时携带 `execution_epoch`、当时的 `arm_epoch` 和唯一 `command_id`，不得把两种 epoch 二选一或混用。
   - And open 仅在 DAQ 写入成功回执后成为已确认活动阀；close 仅在成功回执后清除活动阀并推进 trial。写入失败、取消或超时不得伪装为成功。
   - And open 一旦进入 HAL/driver 后发生异常、超时或结果不确定，都按 `possibly_open` 处理：立即进入 `BLOCKED`、提交紧急 close，并且只有 close ack 才能移除该保守事实。
   - And stop、readiness loss、模式切换、协议替换与 queued open/close 竞争时，必须先失效 epoch、取消旧 normal open，并通过高优先级安全关闭收敛硬件状态；安全清理成功前不得提交新的 document/mode/trial 状态。
   - And stale close 回执可以更新保守的物理关闭事实，但不能推进新 trial；stale open 若已成功写硬件，不能只忽略，必须立即提交紧急关闭。
   - And normal close 与 safety close 重复到达必须按 command/channel 幂等，不重复推进 trial、不重复制造告警风暴。

5. **p95 统计口径与 20/30ms 边界**
   - Given 当前协议运行产生成功的正常气味阀 open/close 回执；
   - When 更新动作质量统计；
   - Then open、close 与 combined 三个序列分别维护滑动窗口并使用确定性的 nearest-rank `sorted_values[ceil(0.95*n)-1]` 计算 p95：open/close 各自保留最近 100 个同类成功正常动作；combined 按 HAL ack 的时间顺序合并 open/close，保留最近 100 个 action jitter（不是每 trial max，也不是 duration jitter）。
   - And 每个序列至少有 20 个样本后才评估其 p95；单次阈值从第一个合格样本起生效。窗口大小、最小样本数和阈值来自默认配置，缺字段时使用本故事规定的默认值。
   - And 任一已具备最小样本数的 open/close/combined p95 **严格大于** `20.0ms` 时产生非阻断警告；警告在该序列 p95 回到 `<=20.0ms` 时记录一次恢复。恰好 `20.0ms` 不触发“超过”警告，但 UI 显示为“临界（未达到 <20ms 目标）”，不得显示为达标。
   - And 任一单次正常 open 或 close jitter **严格大于** `30.0ms` 时产生单次严重超限；恰好 `30.0ms` 不触发该条件。
   - And 失败、取消、stale 回执、主阀预备动作与安全提前关闭不进入正常质量样本，但必须作为结构化事件记录，不能被静默丢弃。
   - And 质量统计在同一次协议运行的 rearm 后继续保留，避免通过重试隐藏差表现；加载新协议本身不重置，只有该候选协议成功 start/restart 并提交新运行时才重置。Story 3.5 建立会话后应由会话边界接管重置语义。
   - And 指标仅在 start/restart 的候选 document、状态与 readiness 全部通过并成功提交新 `execution_epoch` 后原子重置；非法 start、readiness 拒绝或协议替换安全清理失败不得清空统计。
   - And “100/20 样本、nearest-rank、open/close/combined、rearm 保留/start 重置”是本 Story 为消除上游歧义而新增并固定的验收口径，不得被实现者替换为库默认 percentile 或其他窗口。

6. **严重超限的安全降级与显式恢复**
   - Given 单次正常 open 或 close jitter 严格大于 `30.0ms`；
   - When Worker 记录该回执；
   - Then 本故事将其定义为“严重超限”：立即锁存质量阻断、取消所有未执行 normal open、拒绝新的 normal open，并使当前协议进入 `BLOCKED`；不能只显示 UI 警告后继续自动动作。
   - And 若严重超限发生在 open，必须立即走高优先级安全关闭，不再等待原 `duration_ms`；该 trial 未完成，显式恢复后重试当前 trial。若发生在 close，先以回执确认阀已关闭，将该 trial 标为 `executed_quality_failed` 并推进指针，但在布防下一 trial 前进入 `BLOCKED`；恢复后从下一 trial 继续，不能默认重复暴露。若它是最后一个 trial，显式确认后进入 `COMPLETED` 而非重跑。
   - And p95 >20ms 但没有单次 >30ms 时显示非模态警告并继续当前运行；不得弹出会阻塞动作线程的模态对话框。
   - And 质量阻断不影响安全 close、stop、reset、disconnect 或 shutdown；任何安全关闭失败必须保留 `BLOCKED + active_valve/possibly_open`，允许现有 stop/安全路径重试。
   - And 系统不得自动清除质量阻断；仅在全部阀门确认关闭、readiness 全部恢复后，由用户显式 `rearm_current()`（或明确的质量确认动作）创建新 epoch 并继续当前 trial。
   - And “单次 >30ms 同时触发上游要求的告警，并按严重超限安全降级”是本 Story 新增的安全优先策略；它不冒充 epics 原文，但作为本 Story 的明确验收决策实施，避免再发明未获依据的第三个阈值。
   - And 用户请求暂停时，必须失效当前 epoch、取消 pending normal open；若阀已打开或可能打开，立即请求高优先级安全关闭。关闭确认后保持当前 trial 于 `PAUSED`，恢复时重新检查 readiness、创建新 epoch 并从该 trial 的 `WAITING_TRIGGER` 重新开始；关闭失败进入 `BLOCKED` 并保留恢复依据。

7. **执行瞬间仍受 readiness 与安全联锁保护**
   - Given 一个 open 在入队时满足 readiness；
   - When 它到达 deadline 准备写硬件；
   - Then Worker 必须在同一原子提交边界重新验证其持有的最新 frozen safety/readiness snapshot 与 generation：有效协议、连接、自检、MFC setpoint、`SAFE`，TTL trial 还需 AI6 ready；任一变化都取消 open 并进入现有安全阻断路径。
   - And 新增唯一的线程安全 `ActuationInterlockIngress`（或严格等价 store），它复用同一个 `SafetyManager`，允许 HardwareWorker/serial owner 在 producer 线程、入队前原子发布 raw telemetry、AI error/disconnect 与 MFC readiness；store 在锁内更新 immutable snapshot、严格递增 ingress generation，并在任一 unsafe/loss 时锁存 `unsafe_latched=True`。这不是第二套协议状态机或安全算法。
   - And HardwareWorker 必须先更新 ingress store，再另行发 UI signal/ActuationWorker command。ActuationWorker 在每个 open write 前读取 `(generation, snapshot, unsafe_latched)`，write 返回后再次读取；即使它在同步 DAQ call 中无法消费队列，producer 仍可推进 generation/锁存 unsafe。若前后 generation 不同或 latch 为 unsafe，则按 `possibly_open` 立即紧急关闭。
   - And safe telemetry 不得由 producer 自动清除 unsafe latch；只有 ActuationWorker 消费对应更新、确认全部 readiness 与硬件关闭后才能显式 clear。generation 在 connected、hardware_ready、flow_setpoints_ready、safety_state、ttl_input_ready 或 protocol/device lease 任一字段变化时递增；deadline 路径不跨线程读取 `AppState`。`ValveService` 复用映射和政策校验，execute-time interlock 以该 snapshot 为准。
   - And safety loss 与 due-open 同时发生时安全 generation 优先；若 DAQ write 已开始且返回后发现 generation 已变化，必须把该阀视为可能已打开并立即紧急关闭。
   - And 所有 open 继续复用 `ValveService` 的映射、MFC readiness、`SafetyManager` 与主阀契约；不得在 Worker/HAL 中复制或绕过这些守卫。
   - And `safety_close=True` 仍只能关闭；误用于打开必须拒绝且不写硬件。非 `SAFE` 下安全关闭仍允许。
   - And 本故事不改变当前“协议定时 close 只关闭气味通道”的主阀行为；全局 stop、reset、disconnect、急停、退出与 shutdown 仍必须按 FR1.3 关闭所有应关闭的阀门。Story 4.4 的静息/刺激主阀自动化留在其原范围。主阀若需首次准备，必须在协议布防前通过正常安全路径完成并单独记录；不得把临时创建/打开主阀留在第一个 odor deadline 内制造长尾。

8. **HAL 的低尾延迟与真实回执**
   - Given RealHAL 使用 NI USB-6001 的软件定时 digital output；
   - When 执行连续阀门动作；
   - Then deadline 路径不得每次重新创建、配置和销毁 `nidaqmx.Task()`；应在自检/准备阶段预建并复用适合真实设备资源模型的 DO task，在 reset/shutdown 时确定性释放。
   - And 不得盲目缓存 20 个可能争抢同一 port 的 line task；先以真实 Dev1/Dev2 spike 确认每设备/port/line 的可行资源分组，再将选定策略固化为 HAL 测试和工程记录。
   - And on-demand `Task.write()` 的成功返回是软件确认测量点；失败、异常或超时返回结构化失败回执，不得只吞异常或伪造 actual timestamp。
   - And MockHAL 与 RealHAL 暴露相同动作结果契约；测试可注入 writer、单调时钟与确定性延迟，不新增运行时依赖或升级已锁定的 Python 3.11、PySide6 6.7.2、nidaqmx 0.9.0。
   - And Story 3.3 的唯一 AI0/AI6 RSE continuous task、批量排空、1000Hz 采集、AI0 100Hz 下采样与故障恢复必须保持不变。
   - And RealHAL 生命周期拆分为线程归属明确的 AI、DO 与 serial 子资源：AI prepare/reset 只由 HardwareWorker 调用，DO prepare/write/release 只由 ActuationWorker 调用；`flush_logs()` 不得再隐式释放其他线程拥有的硬件 task。

9. **结构化事件、日志与中文 UX**
   - Given 动作完成、阈值越限、取消、失败或进入质量阻断；
   - When Controller 消费 Worker receipt；
   - Then `ProtocolGateEvent`/等价事件在保持现有字段兼容的同时增加 action identity、expected/actual/offset/jitter、p95、sample count、warning/severe 和 measurement point，供当前 logger 与 Story 3.5 会话持久化复用。
   - And 同类 p95 警告按“进入超限/恢复正常”状态转换记录，不得每个 50ms tick 重复输出；单次严重超限每个 command 只记录一次。
   - And 协议页显示当前 trial、下一个气味（优先取 trial metadata 的 odor/label，缺失时显示 `-`）、目标阀、触发模式、运行/暂停/阻断状态、最近一次 jitter、open/close/combined p95、样本数与剩余刺激时间；remaining time 基于单调 close deadline，不基于 UI tick 累加。
   - And “开始、暂停、停止”都必须经过服务/Worker 的真实状态机与安全联锁；View 禁用只能作为反馈，不能作为唯一守卫。暂停行为遵守 AC6，不留下无人管理的 queued open 或活动阀。
   - And p95 警告使用黄色/橙色且不阻断 UI；严重超限/安全关闭失败使用红色，并在持久状态栏保留最近严重事件。
   - And 新增时序信息不得替换或隐藏状态栏既有的连接状态、气流数值、安全状态与最近错误。
   - And 所有用户可见文案使用简体中文，同时说明“发生了什么”和“下一步怎么做”，例如：`阀门时序严重超限，已暂停新的阀门动作并请求安全关闭。请检查系统负载和设备状态，确认所有阀门关闭后重新布防。`

10. **既有状态机与跨 Story 边界不回归**
    - Given Story 3.1–3.3 已建立 parser、呼吸门控、manual/TTL、epoch 和安全关闭契约；
    - When 本故事异步化动作执行；
    - Then `IDLE/READY/WAITING_TRIGGER/WAITING_EXHALE/TRIGGERED/SKIPPED/COMPLETED/BLOCKED/STOPPED` 既有语义、manual/TTL 两道门、timeout skip/retry、mode override、显式 rearm/restart、frozen protocol model 和陈旧 pulse 拒绝全部保持；仅按 AC6 增量加入语义明确的 `PAUSED`。
    - And close 写失败时不得清空活动阀或推进 trial；协议替换清理失败时保留旧 document/mode/trial/active valve；`BLOCKED` 无活动阀时不得产生周期性重复安全日志。
    - And 本故事只产生内存态指标与结构化日志事件，不创建 Story 3.5 的 `.raw/.log` 会话文件，也不实现 Story 4 的清洗流程。

11. **自动化与并发验证**
    - Given 不连接真实硬件的开发/CI 环境；
    - When 运行新增与既有测试；
    - Then 使用可注入 fake monotonic clock、wait strategy、writer delay 和 barrier 覆盖 deadline 顺序、不得提前执行、open ack 后安排 close、duration、nearest-rank p95、100 样本 rollover、20 样本门槛及严格 20/30ms 边界；单元测试不得依赖脆弱的真实 sleep。
    - And 精确覆盖 safety-vs-open、stop-vs-ack、mode/protocol switch-vs-queued command、stale successful open、重复 close、队列满、epoch 变化、wall-clock 跳变与 UI receipt 延迟；每条竞态验证硬件最终关闭、状态不错误推进且日志不洪泛。
    - And Mock/HAL 测试验证 DO task 复用、write ack 取时点、资源释放和失败回执，同时保留 Story 3.3 AI task 的完整回归。
    - And 全量现有 pytest、ruff 和 `git diff --check` 通过；任何因异步化调整的旧测试必须迁移为等价 characterization，不得通过删除断言掩盖回归。
    - And 覆盖 flow/MFC readiness generation、协议设备租约及同步 API 防死锁：ActuationWorker 内不得等待自己的 receipt；UI/普通操作使用全异步回执，只有 shutdown/资源交接桥可从 Worker 外部执行有界等待。

12. **真实 Windows/NI 工程验收**
    - Given Dev1/Dev2、阀门映射、主阀、流量和安全状态已准备，且 NI MAX/其他会争抢资源的客户端已关闭；
    - When 在实际 Windows 用户环境（非 Codex 沙箱账户）同时运行 AI0/AI6 采集、TTL 检测、呼吸绘图、协议日志和阀门动作基准；
    - Then 分别采集至少 200 个正常 open 与 200 个正常 close 原始 receipt，验证运行期间每一个达到最小样本数的 rolling p95 均小于 `20ms`，并另报全部 200 样本 aggregate p95、最大值和最终 last-100；且无单次 jitter 大于 `30ms`。原始结果和测试环境写入本 story 的 Dev Agent Record。
    - And 基准覆盖 Dev1 port0、Dev1 port1、Dev2 port0 的代表性气味通道；主阀/task 冷启动准备作为单独 warm-up 记录并排除正常样本，预热完成后的首个正式动作仍必须计入。
    - And 正常性能 run 的意外 write failure、timeout、漏 receipt、重复 command、measurement fault 数必须全部为 0；安全故障注入另起 run、另行统计，不能用继续收集 200 个成功样本掩盖失败。
    - And 验证 normal stop、LOW_FLOW/readiness loss、严重超限注入和应用 shutdown 最终关闭所有按该入口应关闭的阀门，包括主阀和全部气味阀；关闭失败场景必须保留可恢复状态并记录。
   - And HIL 工程结果是性能 AC 的证据；fake-clock 测试只能证明算法。若未获得真实硬件授权或环境不可用，不得把本 story 标记为 done。
   - And “每类至少 200 个样本且 HIL 未完成不得 done”是本 Story 新增的工程交付门槛，用于让 `<20ms` 从声明变为可复核证据。

## Tasks / Subtasks

- [x] 固定可开发基线（所有 AC 的前置条件）
  - [x] Story 3.3 review patch 已按协议状态安全与 TTL/共享 AI 可靠性拆分提交。
  - [x] 已在 frontmatter 写入包含完整 3.3 patch 的 `baseline_commit=8b8c126553ec915c034c6d3150d937b18c632986`；3.4 后续实现必须以该提交为基线区分新改动。

- [ ] 固化动作指标、配置与不可变模型（AC: 1, 4, 5, 6, 9）
  - [ ] 新增 `app/models/actuation.py`（或等价聚合文件），定义 frozen `ActuationCommand`、`ActuationReceipt`、动作分类/结果和质量快照；更新 `app/models/__init__.py`。
  - [ ] 扩展 `app/models/protocol_execution.py`，增量加入 pending identity、possibly-open/active 状态、单调 deadline、动作指标和 UI snapshot 字段；保持 `ProtocolGateEvent.as_dict()` 现有键兼容。
  - [ ] 新增纯逻辑 `app/services/actuation_metrics.py`，实现 open/close/combined 滑窗、nearest-rank p95、最小样本、严格阈值、warning transition 与 severe latch；更新 `app/services/__init__.py`。
  - [ ] 在 `config/default_config.json` 加入默认值：`actuation_jitter_target_ms=20.0`、`actuation_jitter_single_limit_ms=30.0`、`actuation_jitter_window_size=100`、`actuation_jitter_min_samples=20`、`actuation_normal_queue_capacity=256`、`actuation_write_timeout_ms=100`、`actuation_emergency_close_timeout_ms=500`、`actuation_shutdown_timeout_ms=2000`；`config/local_config.example.json` 仅在需要说明真实机覆盖时同步键名。

- [ ] 为 AI、TTL 与门控事件贯通单调时间（AC: 1, 2, 7, 10）
  - [ ] 扩展 `AnalogInputFrame`、`TtlPulse` 与 `GatingTransition`，保留现有 wall `timestamp` 并增加采样点 `monotonic_ns`；Mock 和 Real HAL 共享契约。
  - [ ] 将 `breath_samples = Signal(list, float)` 升级为 frozen 结构化 batch（或数值与等长 monotonic 数组的严格契约），从 HAL/HardwareWorker 一直保留每个样本 identity 到 GatingService/Executor；测试 EXHALE 位于多样本 batch 中间位置，禁止只传 batch 尾 wall time 后二次重建。
  - [ ] RealHAL 显式启动 AI task，以 `perf_counter_ns()` 包围 `task.start()` 并记录 midpoint origin、uncertainty、AI epoch 与单调 sample sequence；每帧时间由 origin + sequence/采样率计算，batch backlog 不得向前重锚以隐藏延迟。reset 后创建新 AI epoch。
  - [ ] `RealHAL.read_ai_frames()` 同时传播 wall/monotonic 样本时间；跨 batch 在同一 AI epoch 严格递增。缺失、非有限、倒退或超出已记录启动不确定度/采样周期约束时 fail closed 并走现有 AI 错误恢复，不得退回 UI arrival time。
  - [ ] 明确 `expected_open_ns`：优先取本批首次合法 `EXHALE` transition 的 sample；若沿用 3.2 的“进入等待时 gating 已处于 EXHALE”分支，则取触发该次判定的最后一个有效样本 `monotonic_ns`，并以 distinct reason 记录。不得修改 TTL wall timestamp/epoch/sequence 语义。

- [ ] 建立专用单写者 ActuationWorker（AC: 2, 3, 4, 6, 7）
  - [ ] 新增 `app/workers/actuation_worker.py` 与导出，使用 thread-safe command API、紧急/普通队列、deadline heap/condition、稳定 sequence 和可注入 clock/wait strategy。
  - [ ] 将单一 `ProtocolExecutor/GatingService/ActuationMetrics` 实例完全归属 ActuationWorker；在同一有序 command stream 中处理 AI/TTL/readiness/user intent/timeout/receipt，并在 open ack 后直接安排 close。
  - [ ] 实现 safety generation、设备租约、epoch 取消、stale receipt 补偿关闭、幂等 close、queue backpressure 和 severe quality latch。
  - [ ] 实现线程安全 `ActuationInterlockIngress`：producer 在入队前推进 generation/unsafe latch，Worker 在阻塞 write 前后比较；safe 恢复只能由 owner 显式 clear。使用 barrier 验证 write 期间 safety loss 必被发现并补偿关闭。
  - [ ] 实现 ActuationWorker -> HardwareWorker 的有序 TTL arm/disarm/ack；只有匹配 epoch 的 arm ack 才显示 armed，覆盖 stop/mode/disconnect 与在途 pulse 竞态。
  - [ ] HAL DO adapter 在 write 前/返回后采集 started/actual，ActuationWorker 只组装业务 receipt/metrics 并通过 Qt signal/线程安全结果通道交给 Controller；UI handler 到达时间不得参与 jitter。

- [ ] 将全部 DO 写入迁移到单写者并优化 HAL（AC: 3, 7, 8）
  - [ ] 重构 `app/services/valve_service.py`：映射/安全校验/主阀计划与写入确认原子协作，缓存只在成功 receipt 后更新；为共享状态加锁或由单写者独占。
  - [ ] 修改 `app/workers/hardware_worker.py`、`app/services/hal.py`、`mock_hal.py`、`real_hal.py`，让 HardwareWorker 仅拥有 AI；禁止 normal 路径跨线程直接写 DO，并提供分离的 AI/DO/serial 生命周期与结构化写入确认契约。
  - [ ] 以真实 NI spike 决定 per-device/port/line task 复用策略；deadline 内不创建/销毁 task，且不破坏唯一 continuous AI task。
  - [ ] 迁移 MainController 手动阀、预检后台序列、FlowService master writer、ShutdownService close-all 等入口；flow intent 必须先由 ActuationWorker 租约授权再触达 serial/FlowService，安全 close 始终具有最高优先级。
  - [ ] 固定 shutdown/reconnect 顺序：停止新提交与 flow 变更 -> 失效 normal epoch -> emergency close ack -> 停止 ActuationWorker -> 由其释放 DO -> HardwareWorker 释放 AI -> serial owner 最后释放；任一步失败记录 unsafe/人工恢复，不得由别的线程抢先关闭共享 HAL。

- [ ] 异步接回 ProtocolExecutor、Controller 与应用生命周期（AC: 2, 4, 6, 7, 10）
  - [ ] 修改 `app/services/protocol_executor.py`：从同步 writer 返回改为产生 action request/消费 receipt（或严格等价结构），open ack 后确认 active，close ack 后推进 trial。
  - [ ] 50ms protocol timer 仅保留非关键 UI refresh/snapshot request；呼吸 timeout、刺激 close 与所有状态变更统一归 ActuationWorker deadline queue。
  - [ ] 修改 `app/controllers/main_controller.py` 与 `app/main.py`，创建、启动、连接和确定性停止 ActuationWorker；所有用户 intent、readiness/safety update、动作 receipt 通过单一编排路径。
  - [ ] stop/reset/disconnect/shutdown/mode switch/protocol replacement 等待或异步确认真实安全清理结果，不能先清逻辑状态再假定硬件已关闭。
  - [ ] production submit API 默认为异步；禁止 UI、ActuationWorker 自身或 receipt handler 用 `Event.wait()`/future wait 保持旧同步接口。仅 shutdown ownership handoff 可在 Worker 外部按配置执行有界等待，超时必须进入失败记录而非死锁。

- [ ] 增加指标日志与协议页反馈（AC: 5, 6, 9）
  - [ ] 扩展 `app/views/protocol_view.py` 显示下一个气味、最近 jitter、open/close/combined p95、样本数、单调 remaining time 与质量阻断原因；增加受服务层守卫的暂停/恢复 intent，并保持现有 manual/TTL capability。
  - [ ] 扩展 `app/views/main_window.py` 的持久状态栏严重事件显示，按 UX 使用黄/橙/红语义和可执行中文文案。
  - [ ] `MainController._publish_protocol_result()` 记录完整结构化 receipt/metrics，按状态转换去重，供 Story 3.5 直接持久化。

- [ ] 建立 RED-GREEN-REFACTOR 自动化保护（AC: 1-11）
  - [ ] 新增 `tests/test_actuation_metrics.py`、`tests/test_actuation_worker.py`，使用 fake clock/wait 与 barrier 覆盖统计、deadline、优先级和竞态。
  - [ ] 扩展 `tests/test_protocol_executor.py`、`test_protocol_trigger_integration.py`、`test_integration_gating.py`，覆盖 pending/ack/close/advance、严重超限阻断与 stale 补偿关闭。
  - [ ] 扩展 `tests/test_valve_service.py`、`test_flow_service.py`、`test_ttl_input.py`、shutdown/app/simulation 测试，覆盖单写者、MFC generation/租约、DO task 复用、AI 回归和应用生命周期。
  - [ ] 扩展 `tests/test_protocol_view.py` 验证中文指标、颜色/警告、按钮能力与 persistent error。
  - [ ] 运行定向测试、全量 `python -m pytest`、`python -m ruff check app tests` 与 `git diff --check`。

- [ ] 完成真实 Windows/NI 性能与安全验收（AC: 8, 12）
  - [ ] 在真实用户环境关闭 NI MAX 后验证 DO task 复用策略与 Dev1/Dev2 资源无冲突，保留设备/驱动/配置信息。
  - [ ] 在 AI/TTL/UI/logging 并发负载下采集至少 200 open + 200 close receipt，保存原始样本并记录 open/close/combined p95、最大值和失败数。
  - [ ] 验证 stop、LOW_FLOW/readiness loss、severe 注入和 shutdown 对主阀及全部气味阀的最终关闭事实；未经授权不得执行破坏性拔线/短接测试。
  - [ ] 固定可复现参数：代表通道至少 valve 1/9/13、`duration_ms=100`、inter-trial 至少 250ms、主阀预备方式、惰性气路/无气味负载与操作者授权；原始 JSONL/CSV 写入 `logs/benchmarks/`，摘要写入本 story。
  - [ ] 仅在全部自动化、ruff、diff check 与 HIL AC 均有证据后把 story 置为 review。

## Dev Notes

### 本故事固定的产品/测量决策

- 上游只规定“p95 >20ms 或单次 >30ms 告警；严重超限暂停新动作或安全降级”，没有定义样本窗口、最小样本或严重阈值。本故事固定为：最近 100 个成功正常动作、20 个样本后评估 p95；单次严格 >30ms 即严重并锁存 `BLOCKED`；p95 严格 >20ms 仅非阻断警告。这是安全优先且无需再发明第三个阈值的实现口径。
- `jitter_ms` 使用绝对偏差，另保留 signed `offset_ms` 诊断早/晚。20/30ms 比较使用未取整值和严格大于，忠实对应 epics 的“超过”。
- open 以 EXHALE 样本单调时刻为 expected，包含 AI batch/线程调度/ValveService/DAQ 写入到 ack 的软件路径；不采用“Controller 入队时间”以免隐藏 UI/队列延迟。
- close 以实际 open ack + `duration_ms` 为 expected，使开阀晚到不会自动缩短刺激时长；严重 open 超限例外，立即安全关闭。
- `timing_ms` 当前只解析/展示，3.2/3.3 从未执行它。本故事不静默赋予新语义，避免改变已有协议行为；如需 onset delay，应另行确认后建 story。
- 正常统计只覆盖协议气味阀 open/close；主阀准备、手动/预检、失败/取消/stale 和安全 close 单独记录。首个 odor deadline 前必须完成主阀准备，避免首动作长尾。
- 上述窗口、严重策略、expected-open/close 与 200+200 HIL 样本均是本 Story 为获得可实现、可测试结果而新增的验收决策：优先保留固定刺激时长，迟到的 open 会顺延 normal close；它们不是对 epics/PRD 原文的逐字转述。

### 当前代码基线与必须修复的事实

- Story 3.3 review patch 已拆分为 `3983c58`（协议执行状态与 readiness）和 `8b8c126`（TTL/共享 AI 连续采集）；Story 3.4 的实现基线固定为完整包含两者的 `8b8c126553ec915c034c6d3150d937b18c632986`。
- `MainController` 创建 `ProtocolExecutor(clock=time.time)`，并以 UI 线程 50ms `QTimer` 调用 `tick()`；开阀在呼吸回调中同步执行，关阀轮询量化误差已超过目标。[Source: app/controllers/main_controller.py]
- `HardwareWorker.write_digital()` 只是普通同步方法，从外部调用不会自动切换到其 `QThread.run()`；当前 UI、预检后台线程和 shutdown 可并发写 DO。[Source: app/workers/hardware_worker.py]
- `RealHAL.write_digital()` 每次使用新的 `nidaqmx.Task()`；必须把 task 准备移出 deadline 路径，但 NI 可能按 port 保留资源，需先实机确认分组。[Source: app/services/real_hal.py]
- 现有 `triggered_at` 是呼气样本 wall time，`stimulus_end` timestamp 在 close write 前取得，`actual_duration_ms` 不是两个写入 ack 的间隔，不能直接作为 3.4 证据。[Source: app/services/protocol_executor.py]
- `ValveService` 已实现映射、MFC readiness、安全守卫、主阀与 close-failure 恢复；必须扩展复用而不是绕过。异步后 `_states` 只能由单写者或锁保护，并且只在成功回执后提交。[Source: app/services/valve_service.py]
- 当前 AI batch 的 wall timestamp 是按读取时刻向前重建，尚无硬件原生逐样本 timestamp；3.4 必须用显式 AI start epoch + sample sequence 建立单调时间，并把 origin uncertainty 写入工程记录，不能用 UI 到达时刻伪造精度。[Source: app/services/real_hal.py]

### 必须保留的 Story 3.1–3.3 契约

- `ProtocolDocument`/`ProtocolTrial` 保持 frozen；运行态写执行模型，不写回 parser 输出。
- manual/TTL 先进入 `WAITING_EXHALE`，只有校准后的合法 EXHALE 才请求 open；manual 不因仅 AI6 不可用而阻断。
- TTL `timestamp + arm_epoch + pulse_sequence` 保真；持续高电平、重复、模式不匹配和旧 epoch 不推进。
- readiness 拒绝必须原子且不改 trial/mode/epoch/timer/valve；运行中 readiness 丢失进入 `BLOCKED`。
- stop、模式切换、协议替换、安全中断先清理 pending/active 输出；close 失败保留 `active_valve`/possibly-open 并可重试。
- `BLOCKED` 只允许显式 rearm，`STOPPED` 显式 restart 回 trial 0，`COMPLETED` 需 reset/reload；不得自动恢复或消费旧 pulse。
- 唯一 Dev1 AI0/AI6 RSE continuous task、批量排空、故障锁存/退避和有效帧恢复保持不变。

### 架构与库约束

- Python 3.11；PySide6 6.7.2；nidaqmx 0.9.0；pytest 7.4.4；ruff 0.6.5。不得为本故事随意升级或增加实时调度依赖。[Source: requirements.txt; requirements-dev.txt]
- 继续 MVC + Worker + HAL：View 被动、Controller 编排、Worker 承担硬件轮询/低抖动执行，所有硬件访问经过 HAL。[Source: docs/architecture.md; docs/project-structure.md]
- Python 官方将 `perf_counter_ns()` 定义为适合短时长测量的最高可用分辨率、单调性能计数器，并避免 float 精度损失；本故事用它做 deadline/interval，wall clock 只做日志。[External: Python `time` documentation](https://docs.python.org/3.11/library/time.html#time.perf_counter_ns)
- Qt 官方说明 `QTimer` 即使使用 PreciseTimer 也可能在系统繁忙时晚到；PySide6 6.7.2 又没有 Qt 6.8 新增的 `QChronoTimer`。因此 50ms UI timer 不能作为性能实现或证据。[External: Qt for Python `QTimer`](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QTimer.html)
- NI 官方明确 USB-6001 不支持 digital I/O hardware timing，只能软件定时；本故事必须测量并安全处理尾延迟，不能假称硬件定时保证。[External: NI USB-6001 hardware timing](https://knowledge.ni.com/KnowledgeArticleDetails?id=kA00Z000000kJFvSAM)
- NI-DAQmx Python 文档说明反复隐式启动/停止 task 会降低性能，而 on-demand `write()` 成功返回时设备已生成该样本；应预建/复用 task 并把 write 返回作为软件 ack。[External: NI-DAQmx Python Task API](https://nidaqmx-python.readthedocs.io/en/stable/task.html)
- 上述网页是当前官方 API 文档，而项目锁定 `nidaqmx==0.9.0`；persistent task 的 `start()/auto_start/write(timeout=...)` 行为必须在锁定版本单元桩与真实 NI 驱动上验证后才能作为实现依据，不得因新文档存在就假定 0.9.0 完全相同。
- `Condition.wait()` 可能被唤醒后发现条件已变化，调度循环必须在锁内反复检查 heap head、epoch 和 safety generation；不要假定一次 wait/notify 等于命令可执行。[External: Python `threading.Condition`](https://docs.python.org/3.11/library/threading.html#condition-objects)

### Project Structure Notes

- 新模型放 `app/models/actuation.py`，新纯指标逻辑放 `app/services/actuation_metrics.py`，新低抖动线程放 `app/workers/actuation_worker.py`；不要把业务逻辑放 View 或项目根目录。
- 预期 UPDATE：`app/main.py`、`app/controllers/main_controller.py`、`app/models/{__init__,protocol_execution}.py`、`app/services/{__init__,hal,mock_hal,real_hal,protocol_executor,valve_service,ttl_trigger_service,gating_service,shutdown_service,flow_service}.py`、`app/workers/{__init__,hardware_worker}.py`、`app/views/{main_window,protocol_view}.py`、配置和相关测试。
- 3.4 的文件写入仅限结构化 logger；`.raw/.log` 文件命名、会话目录和磁盘失败处理属于 Story 3.5。
- 实现完成后更新 `docs/architecture.md` 与 `docs/project-structure.md`，固化 ActuationWorker 的单一状态/指标/DO 所有权、HardwareWorker 的 AI 所有权、消息流和分资源生命周期，避免该边界只存在于 sprint artifact。

### Testing Requirements

- 先写失败测试再改生产代码；性能算法测试使用 fake clock/wait，竞态使用 barrier/event，不能用毫秒级真实 sleep 作为唯一断言。
- 必须迁移并保留现有 executor、trigger integration、gating、ValveService、TTL input/detector、shutdown、simulation、UI 测试的行为覆盖。
- HIL 基准必须在实际 Windows 用户环境运行；Story 3.3 已证明 Codex 沙箱会阻断 NI Configuration Manager IPC 并制造假超时。
- 软件 ack 只能证明 software/driver 时序。若未来声称机械阀时序，需要输出回采或外部传感器的独立验收。

### References

- [Source: docs/epics.md#Story-3.4-低抖动阀门动作]
- [Source: docs/prd.md#FR5-协议执行]
- [Source: docs/prd.md#非功能需求]
- [Source: docs/ux-design.md#协议页]
- [Source: docs/ux-design.md#文案规范]
- [Source: docs/architecture.md#分层结构]
- [Source: docs/architecture.md#HAL-硬件抽象]
- [Source: docs/project-structure.md#核心代码-app]
- [Source: docs/sprint-artifacts/3-2-breath-gated-stimulation.md]
- [Source: docs/sprint-artifacts/3-3-manual-vs-ttl-trigger-modes.md]
- [Source: app/models/protocol_execution.py]
- [Source: app/services/protocol_executor.py]
- [Source: app/services/valve_service.py]
- [Source: app/workers/hardware_worker.py]
- [Source: app/services/real_hal.py]
- [Source: app/controllers/main_controller.py]
- [Source: tests/test_protocol_executor.py]
- [Source: tests/test_protocol_trigger_integration.py]
- [Source: tests/test_integration_gating.py]
- [Source: tests/test_valve_service.py]
- [Source: tests/test_ttl_input.py]

## Dev Agent Record

### Agent Model Used

OpenAI Codex（GPT-5）

### Debug Log References

### Completion Notes List

- Ultimate context engine analysis completed - comprehensive developer guide created.
- Story 3.4 的 measurement point、滑动窗口、最小样本、严格阈值、严重超限与恢复语义已固定，开发者无需自行猜测上游歧义。
- Story 3.3 review patch 已提交，Story 3.4 baseline 已固定为 `8b8c126553ec915c034c6d3150d937b18c632986`。

### File List

## Change Log

- 2026-07-21：创建 Story 3.4 开发上下文，状态设为 ready-for-dev。
