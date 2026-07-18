---
baseline_commit: fe9d1259a6115e58d14887a3b0948cebb06b0ddb
---

# Story 3.3: 手动与 TTL 触发模式

Status: ready-for-dev
Epic: 3 - 协议执行与数据记录
Story Key: 3-3-manual-vs-ttl-trigger-modes
Story ID: 3.3

## Story

作为研究人员，
我需要在手动触发和外部 TTL 触发之间安全切换，
以便既能独立推进实验，也能接入 SuperLab，并继续复用既有呼吸门控与安全关阀链路。

## Acceptance Criteria

1. **触发事件先推进到呼吸门控，不直接开阀**
   - Given 已加载有效协议、硬件与流量条件满足且安全状态为 `SAFE`；
   - When 用户开始协议执行；
   - Then `ProtocolExecutor` 进入新增的“等待触发”状态，不打开阀门，也不立即进入 `waiting_exhale`。
   - And 当前 trial 的默认运行模式来自既有 `ProtocolTrial.trigger`；不得修改 frozen 的 `ProtocolDocument` 或 `ProtocolTrial`。
   - And 接受一次与当前模式匹配的触发后，当前 trial 才进入 Story 3.2 已有的 `waiting_exhale`，随后仍须达到呼气阈值才允许开阀。

2. **手动触发模式**
   - Given 当前 trial 处于“等待触发”、运行模式为 `manual` 且安全状态为 `SAFE`；
   - When 用户点击协议页“手动触发”；
   - Then `MainController` 将一次 `manual` 触发意图交给 `ProtocolExecutor`，当前 trial 只推进一次到 `waiting_exhale`。
   - And 按钮连击、重复 signal 或已离开“等待触发”后的再次点击不得重复推进、跳过 trial 或重复开阀。
   - And 非 `SAFE`、无有效协议、硬件未连接、基础硬件/AI0 自检未通过、MFC 流量设定未建立、未开始、错误模式、`blocked/stopped/completed` 等状态下，手动触发必须被服务层拒绝并给出中文原因，不能只靠按钮禁用；仅 AI6 不可用不得阻断合法 manual 流程。

3. **TTL 触发模式与 AI6 上升沿**
   - Given 当前 trial 处于“等待触发”、运行模式为 `ttl` 且安全状态为 `SAFE`；
   - When `Dev1/AI6`（配置项可覆盖）检测到一次有效 TTL 低到高跃迁；
   - Then HAL/Worker 产生一个带采集时间戳的 TTL pulse 事件，`MainController` 转交 `ProtocolExecutor`，当前 trial 只推进一次到 `waiting_exhale`。
   - And 持续高电平只能计为一个脉冲；必须先回落到低阈值以下并满足去抖条件后才能重新布防下一次上升沿。
   - And TTL 采集、阈值/迟滞和边沿去重不得依赖 UI 线程或 `ProtocolView`；真实与 Mock HAL 必须共享同一输入契约。
   - And `Dev1` 的 AI0 与 AI6 必须共用同一个 NI-DAQmx Analog Input task/采样资源，由 HAL/Worker 解复用呼吸与 TTL 样本；不得为 AI6 创建与现有 AI0 并发的第二个 Analog Input task。
   - And TTL 模式只有在硬件已连接、自检通过、MFC 流量设定已建立且共享 AI 采样链路中的 AI6 通道已就绪时才能布防；任一条件不满足时不得把状态推进到可接收 pulse 的等待态。
   - And 已布防或运行期间发生 TTL/共享 AI 读取异常时，必须产生中文错误/事件、失效当前 epoch、进入 `blocked`，并复用 Story 3.2 的安全关闭路径处理活动阀；不得把读取失败伪装成无脉冲。

4. **模式不匹配、陈旧与重复触发不推进状态**
   - Given 系统收到与当前模式不匹配的来源、模式切换前排队的旧 pulse、持续高电平的重复采样，或当前状态并非“等待触发”；
   - When 该事件到达 `ProtocolExecutor`；
   - Then trial index、等待呼气起点、重试计数和阀门状态均保持不变。
   - And 事件应以 `ignored`/`rejected` 结果记录来源和原因，但持续高电平不得造成高频重复日志。
   - And queued TTL 事件必须携带采样/发射时捕获的不可变 `arm_epoch`、单调 `pulse_sequence` 或等价旧事件身份；controller 不得在 signal 到达时用“当前 epoch”替旧事件补标。
   - And 不得通过动态反复 connect 同一个 Qt signal 来实现模式切换；硬件 signal 只连接一次，由执行器按模式和状态过滤。

5. **模式切换执行安全清理**
   - Given 当前模式为 `manual` 或 `ttl`；
   - When 用户选择另一模式；
   - Then 模式变更必须由 `ProtocolExecutor.set_trigger_mode()`（或等价服务 API）原子处理；模式值/override 仅写执行态、不写回协议模型，必要的安全关阀仍走既有 writer 链路。
   - And 对已经开始且处于合法运行态的切换，提交新模式前必须失效旧模式的待处理 pulse/触发锁存，清除旧的触发等待与呼吸等待起点、超时/重试计数，并让当前 trial 回到“等待触发”；`ready` 的特殊语义见下条。
   - And 若切换发生时存在活动阀，必须先通过既有 `ValveService` 安全关闭路径关阀；关闭成功后才能切换，关闭失败则保留 `active_valve`、保持旧模式并进入 `blocked`，允许既有停止/安全路径重试关闭。
   - And 允许切换的执行状态必须由 executor 明确定义：`ready` 下只更新当前 trial override 并保持 `ready`，不得绕过 `start()` 直接进入“等待触发”；运行中的合法等待态可清理后回到“等待触发”；`triggered` 仅在活动阀安全关闭成功后允许切换。
   - And `idle/blocked/stopped/completed`、任意非 `SAFE` 或通用 readiness 不满足时，服务层必须拒绝模式切换并保持原状态、原模式、epoch 和活动阀不变；切向 TTL 还必须检查 AI6 readiness，切向 manual 不得因仅 AI6 不可用而拒绝；恢复 `SAFE` 不得自动重试该切换或重新布防。
   - And 选择当前模式是幂等操作，不重复 reset、不重复连接 signal、不重复写日志。
   - And 当前 trial 初始模式取 `ProtocolTrial.trigger`；用户覆盖只影响当前 trial 的执行态并记录 `mode_override`。推进到下一 trial 后重新采用下一条 `ProtocolTrial.trigger`，以支持混合 manual/ttl 协议。

6. **协议替换、停止、重置和安全中断清理全部触发态**
   - Given 协议正在等待触发、等待呼气、刺激中或已经阻断；
   - When 加载另一份有效协议、点击协议停止、全局停止/重置、硬件断开，或安全状态变为 `LOW_FLOW`/`DATA_STALE`/其他非 `SAFE`；
   - Then 必须先取消/失效当前触发布防和排队 pulse，并复用 Story 3.2 的安全关闭路径处理活动阀。
   - And 新协议只有在旧执行态清理成功后才能成为当前执行 document；清理失败时不得覆盖旧 `loaded_protocol`，不得把新协议显示为可执行。
   - And 协议替换清理失败时，旧 `document/trial/mode/active_valve` 不被候选协议覆盖，但执行状态必须按 3.2 进入 `blocked`、保留关闭失败的 `active_valve` 并保持旧 epoch 失效；不得回滚为能消费旧 pulse 的等待态。
   - And 解析新协议失败仍保留上一份有效协议与既有执行状态原子性，不触发任何清理或硬件动作。
   - And 非 `SAFE` 下 manual/TTL pulse 均不得恢复或推进 trial；恢复 `SAFE` 后不能消费中断期间的旧 pulse，也不能自动恢复。
   - And 安全中断进入 `blocked` 且已确认无活动阀后，readiness 全部恢复时只允许用户显式调用 `rearm_current()`（或等价 API）：保留当前 trial 与运行模式、清空旧等待/重试并创建新 epoch 后进入 `waiting_trigger`；若仍有活动阀则必须先通过 stop/安全关闭重试。
   - And `stopped` 下只允许用户显式 `start/restart` 从 trial 0 重新开始，清空 retry/override 并采用首个 trial 的声明模式；`completed` 必须先 reset/reload 到 `ready`，`idle` 必须先加载有效协议。

7. **协议页明确显示模式、布防状态和可用动作**
   - Given controller 提供执行快照；
   - When 页面渲染 3.2 既有的 `idle`、`ready`、`waiting_exhale`、`triggered`、`skipped`、`blocked`、`stopped`、`completed` 以及新增的 `waiting_trigger`；
   - Then 页面以简体中文显示当前 trial、协议声明模式、当前运行模式、是否等待外部 TTL、呼吸门控状态、目标阀门、等待时间和最近事件。
   - And “手动模式”与“TTL 模式”使用互斥选择控件；“手动触发”是独立动作按钮，不提供会被误认为真实外部输入的生产环境“TTL 触发”按钮。
   - And capability 必须按动作所需 readiness 分别计算：`can_manual_trigger` 和手动模式选择只依赖通用 readiness；`can_select_ttl_mode`/`ttl_armed` 额外依赖 AI6 readiness。单一 `can_select_mode` 若无法表达目标模式差异，必须拆为目标感知字段或提供等价拒绝原因。
   - And “手动触发”仅在 `manual + waiting_trigger + 通用 readiness 满足` 时可用；AI6 未就绪不得错误禁用合法 manual 流程，但 TTL 模式只有实际布防后才显示“等待外部 TTL”。
   - And View 只发出 `trigger_mode_requested`、`manual_trigger_requested` 等意图并渲染 snapshot，不读取 AI6、不维护 pulse latch、不推进 trial、不调用 executor/HAL/ValveService。

8. **结构化事件日志可追踪**
   - Given 发生开始等待触发、手动触发、TTL pulse 接受/忽略、模式覆盖、模式切换、呼吸门控、安全阻断、停止或清理失败；
   - When controller 发布执行结果；
   - Then 沿用 `protocol_execution` logger 和 `ProtocolGateEvent.as_dict()`，至少记录 `timestamp`、`trial_id/index`、协议模式、当前模式、触发来源、结果、safety state 和中文 message。
   - And TTL 事件时间戳必须使用采集/边沿时间戳，不得在 UI handler 中用新的墙钟时间替代。
   - And 本 story 只保证事件结构与现有运行日志可观察；最终会话 `.log` 文件写入仍属于 Story 3.5。

9. **保持 3.1/3.2 契约和既有安全回归**
   - Given 3.1 已完成协议解析，3.2 已完成呼吸门控、超时、刺激开关阀和关阀失败恢复；
   - When 实现本 story；
   - Then 必须扩展现有 `ProtocolExecutor`、`ProtocolExecutionState/Snapshot` 和 `ProtocolView`，不得创建第二套协议执行状态机或阈值算法。
   - And 阀门动作继续只通过 controller 注入的 writer 调用 `ValveService.set_valve()`；TTL 与 View 均不能直接开关阀。
   - And 3.2 的门控超时 skip/retry、计划/实际时长、非 SAFE 阻断、`safety_close=True` 仅用于关阀、关阀失败保留活动阀、无活动阀时不重复安全日志等行为必须继续通过。
   - And 3.1 的 parser、加载失败原子性和 frozen 协议模型必须继续通过。

10. **统一运行就绪守卫覆盖所有执行入口**
   - Given `SAFE` 与硬件就绪是独立条件，且系统可能出现 `safety_state=SAFE` 但 `connected=False`、基础硬件/AI0 `hardware_ready=False`、`flow_setpoints_ready=False` 或仅 TTL/AI6 输入未就绪；
   - When 用户开始协议、选择模式、手动触发、系统收到 TTL pulse，或尝试从停止/阻断态重新开始/布防；
   - Then controller 必须把统一的执行 readiness 快照交给 `ProtocolExecutor`（或等价单一服务守卫），至少包含有效协议、连接、自检、流量、safety state，以及 TTL 模式所需的输入 readiness。
   - And 在动作提交前发现必要 readiness 不足时，该动作必须被纯拒绝：不得改变 trial index、运行模式、trigger epoch、等待起点、重试计数或阀门状态，不得进入 `waiting_trigger/waiting_exhale`，并返回说明原因与下一步操作的中文事件。
   - And 已进入 `waiting_trigger/waiting_exhale/triggered` 后发生连接、自检、流量、安全或共享 AI/TTL readiness 丢失时，不得按“纯拒绝”处理；必须失效 epoch、进入安全阻断，并复用既有关闭路径处理活动阀。
   - And readiness 校验必须存在于 controller/service 边界，不能只依赖 View capability；安全关闭动作仍可在非就绪/非 `SAFE` 下沿用 `safety_close=True` 关闭既有活动阀。
   - And `start/reset/rearm` 发现 `active_valve is not None` 时必须拒绝覆盖执行状态；只有既有安全关闭成功、旧执行态清理完成后，才允许 reset、替换 document 或重新布防。

## Tasks / Subtasks

- [ ] 扩展协议执行模型，明确“等待触发”与模式态（AC: 1, 4, 5, 7, 8, 9, 10）
  - [ ] 在 `app/models/protocol_execution.py` 为 `ProtocolExecutionStatus` 增加 `WAITING_TRIGGER`，保留 3.2 的 `IDLE/READY/WAITING_EXHALE/TRIGGERED/SKIPPED/COMPLETED/BLOCKED/STOPPED` 全部状态，不要用现有 `WAITING_EXHALE` 同时表示两种等待。
  - [ ] 在 `ProtocolExecutionState` 增加当前运行模式、协议声明模式、触发布防代次/epoch（或等价旧事件失效标识）、触发来源与最近 TTL 时间戳；运行态不得写回 `ProtocolTrial`。
  - [ ] 扩展 `ProtocolGateEvent` 和 `ProtocolExecutionSnapshot`：提供协议模式、当前模式、`can_select_mode`、`can_manual_trigger`、`ttl_armed`/等待外部 TTL、readiness 原因等 View 所需字段；capability 由 executor 的统一状态矩阵与 readiness 计算。
  - [ ] 保持现有字段与 `as_dict()` 向后兼容；如重命名 snapshot 字段，必须同步修复所有调用和回归测试。

- [ ] 扩展单一 `ProtocolExecutor` 的触发状态机（AC: 1, 2, 4, 5, 6, 8, 9, 10）
  - [ ] 修改 `start()`：验证有效协议、统一 readiness 且不存在未关闭的 `active_valve`；`READY` 下保留用户已设置的当前 trial override（没有 override 才采用 `ProtocolTrial.trigger`），`STOPPED` 下从 trial 0 清空 retry/override 后采用首个 trial 声明模式，随后进入 `WAITING_TRIGGER`；其他状态拒绝且不得覆盖旧执行状态。
  - [ ] 增加 `accept_trigger(source, readiness, timestamp, captured_epoch/sequence)` 或等价 API；只有来源匹配、事件身份有效、状态为 `WAITING_TRIGGER` 且全部 readiness 满足才进入既有 `_enter_waiting()` 呼吸门控路径。
  - [ ] 增加 `set_trigger_mode(mode, ...)`：校验受控 `TriggerMode`，按 AC5 的允许状态矩阵实现原子清理、幂等、覆盖日志和关阀失败语义；`ready` 切换保持 `ready`，非 SAFE/非就绪/终态不得重布防。
  - [ ] 修改 `_prepare_after_advance()`：下一 trial 先进入 `WAITING_TRIGGER`，并从该 trial 的 `trigger` 初始化运行模式；不要在刺激结束后直接进入 `WAITING_EXHALE`。
  - [ ] `process_breath_samples()` 仅在 `WAITING_EXHALE` 消费呼气门控；`WAITING_TRIGGER` 期间仍允许 `GatingService` 更新公共呼吸状态，但不能开阀。
  - [ ] `skip_current()` 保持“显式跳过当前 trial”的语义，与“手动触发”分开；非 `SAFE` 推进仍按 3.2 阻断。
  - [ ] `stop()`、`handle_safety_update()` 与协议替换清理必须使旧 epoch 失效；关闭失败语义复用 3.2，不得先清空 `active_valve`。
  - [ ] 收紧 `reset()`：它不是安全清理入口，只能在无活动阀且旧执行态已清理成功后替换 state/document；`blocked + active_valve` 下的 `start/reset/rearm` 必须拒绝并保留旧 document、trial、模式与活动阀，供既有停止路径重试关闭。
  - [ ] 增加 `rearm_current()`（或等价恢复 API）：只允许 `BLOCKED + active_valve is None + readiness 全部恢复`，保留当前 trial 与运行模式，清空旧等待/重试并创建新 epoch 后进入 `WAITING_TRIGGER`；`BLOCKED + active_valve` 必须先重试安全关闭。
  - [ ] 区分动作前拒绝与运行中 readiness 丢失：前者返回 rejected 且状态不变；后者以及 TTL/shared-AI read error 必须使 epoch 失效、进入 `BLOCKED` 并尝试安全关闭活动阀。

- [ ] 建立可测试的 TTL 输入边沿链路（AC: 3, 4, 6, 8, 10）
  - [ ] 扩展 `app/services/hal.py` 的统一契约，提供 AI0/AI6 共享采样帧或等价多通道读取；同步实现 `RealHAL` 与 `MockHAL`，不得在 controller 或 View 直接 import `nidaqmx`。
  - [ ] 重构 `RealHAL` 的现有 `_ai_task`：从配置读取 AI0 与 TTL/AI6 通道，在同一个 `Dev1` Analog Input task 中添加两个 channel 并统一创建、启动、读取、停止和释放；严禁创建两个并发 AI task，以避免 NI-DAQmx `resource reserved/-50103`。
  - [ ] AI6 配置/自检失败不得破坏 manual 所需的 AI0 呼吸路径：关闭任何部分创建的 task 后原子降级为 AI0-only 单 task，并报告 `ttl_input_ready=False`；后续只在重连/重新自检时安全重建 AI0+AI6 task，不得热插入 channel 或与旧 task 并发。
  - [ ] 共享 AI task 使用硬件定时缓冲采样或经真实 USB-6001 验证的等价单 task 方案，使 AI6 有效采样率达到 `ttl_poll_hz`；Worker 按 channel 顺序/名称解复用，AI0 继续以既有 100Hz 契约向上游发出，AI6 以采集时间戳进入 TTL detector。
  - [ ] 新增 `app/services/ttl_trigger_service.py`（或等价纯服务）实现高/低阈值迟滞、上升沿单次发射、回落重新布防和去抖；clock/输入应可注入，并在布防时接收 executor/controller 提供的 opaque epoch，单元测试不得依赖真实等待。
  - [ ] 定义不可变 TTL pulse payload，至少包含采集时间戳和采样/发射时捕获的 `arm_epoch` 或等价单调 sequence；Worker/TTL service 只原样携带身份，不解释协议模式。
  - [ ] 扩展 `HardwareWorker`，消费共享 AI sample frame/batch，分别发布既有呼吸样本和已去重的 pulse payload；读取异常通过单独错误 signal 或结构化结果上报，不能默认为低电平，也不能由 controller 在 queued signal 到达时补写当前 epoch。
  - [ ] 修改 worker 调度与缓冲读取，使 `ttl_poll_hz=1000` 不被现有 100Hz 呼吸/5Hz telemetry 循环降成 100Hz；不得用第二个 AI task 或 UI/QTimer 高频轮询规避共享采集设计，也不得阻塞既有两路信号。
  - [ ] Mock HAL 提供确定性的 TTL 电平/脉冲注入能力，保证 CI 可验证低→高→持续高→低→高序列。
  - [ ] 不把 Story 3.4 的阀门抖动 p95/20ms 质量门提前塞入本服务；本 story 只保留准确输入时间戳和边沿事件，供后续时序分析复用。

- [ ] 接入 `MainController`，保持业务与硬件边界（AC: 2, 3, 4, 5, 6, 8, 9, 10）
  - [ ] 在初始化时仅连接一次 worker TTL pulse/error signal；增加 `handle_protocol_trigger_mode_requested()`、`handle_protocol_manual_trigger_requested()`、`handle_ttl_pulse()` 等 slot。
  - [ ] 建立单一 execution readiness 快照/值对象，汇总有效协议、`telemetry.connected`、`hardware_ready`、`flow_setpoints_ready`、当前 safety state 与 TTL input readiness，并交给 executor；不得把 `SAFE` 当作硬件就绪的替代品。
  - [ ] 所有 handler 只构造来源、传入 readiness 与 pulse 原始不可变 payload、调用 executor、发布结果；不得自行改 trial index、给旧 pulse 补当前 epoch、直接开阀或复制模式校验。
  - [ ] 保持 `_write_protocol_valve()` 为 executor 唯一阀门 writer，并继续让关闭使用 `safety_close=True`、打开使用正常安全守卫。
  - [ ] 增加 TTL/shared-AI error 与 readiness-lost handler：已布防/运行时调用 executor 的统一安全阻断 API，失效 epoch、关闭活动阀并发布中文结果；动作提交前 readiness 不足仍走无副作用 rejected 路径。
  - [ ] 调整 `handle_protocol_file_selected()`：先完整解析候选协议；解析成功后安全清理旧执行态，确认无残留 `active_valve` 后才原子替换 `state.loaded_protocol` 并 reset 新 document。清理失败时旧 document/trial/mode 不被候选协议覆盖，但 executor 必须进入 `BLOCKED`、保留 `active_valve` 并保持旧 epoch 失效。
  - [ ] 全局停止、重置、断连与安全态变化继续调用 executor 的统一停止/阻断路径，同时使 TTL epoch 失效；不要在恢复 `SAFE` 时自动消费旧 pulse。
  - [ ] 恢复 readiness 后不自动执行：`BLOCKED` 且无活动阀时只响应显式 rearm-current，`STOPPED` 时只响应显式 restart；controller 不得自行修改 trial/mode/epoch。
  - [ ] `_publish_protocol_result()` 继续统一写 `protocol_execution` logger、中文状态栏和协议页 snapshot。

- [ ] 扩展 `ProtocolView` / `MainWindow`（AC: 7, 9, 10）
  - [ ] 将现有预留 `_manual_trigger_button` / `_ttl_trigger_button` 整理为互斥模式选择 + 独立手动触发动作；使用清晰中文，避免一个“TTL 触发”按钮伪造硬件脉冲。
  - [ ] 新增 View signals 并在 `MainWindow` 连接到 controller；View 不持有 executor 引用。
  - [ ] 由 snapshot 统一控制模式选择、手动触发、开始、停止、恢复和下一 trial 的 enablement；capability 按目标动作分别计算，至少区分 manual mode/manual trigger 与 TTL mode/armed，controller/service 仍须二次拒绝非法动作。
  - [ ] 显示“协议模式”“当前模式”“等待外部 TTL/等待呼气”和最近事件；混合协议推进时 UI 必须切换到下一 trial 的声明模式。

- [ ] 增加配置并保持单一配置来源（AC: 3, 9）
  - [ ] 在 `config/default_config.json` 增加 `ttl_input_channel="Dev1/ai6"`、`ttl_high_threshold_v=2.0`、`ttl_low_threshold_v=0.8`、`ttl_debounce_ms=2`、`ttl_poll_hz=1000`；`ttl_poll_hz` 驱动共享 AI task 的采样/处理能力而非第二个软件轮询 task；高阈值必须大于低阈值，无效/非有限配置使用安全默认值并记录中文警告。
  - [ ] 在 `config/local_config.example.json` 展示本机 NI 通道覆盖；真实机器差异仍写 `local_config.json`，不新增第二套配置文件。
  - [ ] 保持 Python 3.11、PySide6 6.7.2、nidaqmx 0.9.0、pytest/pytest-qt 与现有 requirements；本 story 不增加外部依赖。

- [ ] 补充自动化测试（AC: 全部）
  - [ ] 扩展 `tests/test_protocol_executor.py`：开始进入 `WAITING_TRIGGER`；manual/ttl 正确来源接受；错误来源、陈旧 epoch、重复事件、非 SAFE 与错误状态拒绝；接受后仍须呼气才开阀。
  - [ ] 显式覆盖无协议时的 `start/manual/TTL` 三个入口，以及未连接、自检失败、流量未准备、TTL input 未就绪的对应入口；断言状态、trial、epoch、等待起点与阀门均不变且中文原因可操作。
  - [ ] 覆盖模式切换完整矩阵：`ready` 保持 `ready`；合法等待态清理后回 `WAITING_TRIGGER`；幂等；非 SAFE/非就绪/终态拒绝；当前 trial override 不改 frozen 协议；下一 trial 恢复声明模式；活动阀关闭成功/失败两条路径。
  - [ ] 覆盖 `close_failed -> start/reset/rearm` 全部拒绝，旧 document、trial、模式、epoch 与 `active_valve` 保持不变，随后停止路径仍可重试关闭。
  - [ ] 覆盖恢复矩阵：安全阻断且无活动阀时，readiness 恢复不会自动推进，显式 `rearm_current` 保留当前 trial/mode、清空 retry 并创建新 epoch；`STOPPED -> restart` 从 trial 0 和首个声明模式开始；`COMPLETED/IDLE` 仍拒绝直接开始。
  - [ ] 新增 `tests/test_ttl_trigger_service.py`：阈值边界、迟滞、持续高电平只发一次、低电平重新布防、bounce 去抖、无效数值/读取错误、可注入时间以及 pulse payload 捕获布防时 epoch/sequence。
  - [ ] 增加 `ttl -> manual -> ttl` 且 AI6 始终高电平的竞态测试：切回 TTL 不得把旧高电平当作新上升沿，必须先观测有效回落再接收下一次 rise。
  - [ ] 扩展 Real/Mock HAL 与 Worker 测试：Dev1 只创建一个 AI task 且包含 AI0/AI6；sample frame channel 映射正确；AI0 仍按 100Hz 上送；AI6 pulse 发出一次且携带原始时间戳和发射时事件身份；共享读取异常上报而不是触发或静默。
  - [ ] 扩展 controller 集成测试：signal 只连接一次、manual/TTL handler 转发 readiness 与不可变 pulse payload、模式切换前入队而切换后送达的旧 pulse 被拒绝、错误模式不调用阀门、协议替换关闭失败后旧 document 保留但状态为 BLOCKED/epoch 失效、停止/断连使旧 pulse 失效。
  - [ ] 覆盖 TTL/shared-AI read error：等待或刺激中发生错误时 executor 进入 BLOCKED、epoch 失效、活动阀走安全关闭；动作前 AI6 未就绪只拒绝 TTL，不改变执行状态。
  - [ ] 扩展 `tests/test_protocol_view.py`：保留 `idle/skipped` 中文状态；AI6 未就绪时 manual mode/start/trigger 仍可用而 TTL mode/armed 不可用；覆盖互斥模式、恢复 capability、TTL 等待显示和 View signal；不要在 UI 测试中断言业务状态机内部细节。
  - [ ] 保留并迁移 3.2 characterization tests：在 `start()` 后注入一次匹配触发再继续验证既有呼吸超时、开关阀、close_failed、非 SAFE 和 BLOCKED 无日志风暴行为，不得为适配 `WAITING_TRIGGER` 删除或弱化旧断言。
  - [ ] 回归 `tests/test_protocol_parser.py`、`tests/test_protocol_executor.py`、`tests/test_integration_gating.py`、`tests/test_protocol_view.py`、`tests/test_valve_service.py`、安全与全局停止相关测试。

- [ ] 工程验证（AC: 全部）
  - [ ] 运行 `python -m pytest tests/test_ttl_trigger_service.py tests/test_protocol_executor.py tests/test_integration_gating.py tests/test_protocol_view.py`。
  - [ ] 运行 `python -m pytest tests/test_protocol_parser.py tests/test_valve_service.py`。
  - [ ] 运行 `python -m pytest`。
  - [ ] 运行 `python -m ruff check app tests`。
  - [ ] 在真实或 NI MAX 模拟的 USB-6001 上验证 Dev1 单一 AI task 同时读取 AI0/AI6，不出现 `-50103/resource reserved`；记录 AI6 有效采样率、AI0 100Hz 输出连续性及读取错误安全降级结果到 sprint artifact。

## Dev Notes

### 本 story 的运行语义（开发前先确认）

```text
加载有效协议 -> READY
开始 -> WAITING_TRIGGER（模式默认取 current_trial.trigger）
  manual + 手动按钮 ─┐
  ttl + AI6 上升沿 ──┴-> WAITING_EXHALE -> 呼气阈值 -> TRIGGERED/开阀
                                               -> duration 到时/关阀
                                               -> 下一 trial 的 WAITING_TRIGGER

运行态 --stop/disconnect/非 SAFE/模式切换/协议替换--> 先失效旧 trigger epoch
活动阀存在 -> 先走 ValveService 安全关闭
  关闭成功 -> 按动作语义停止、切换、替换或 reset
  关闭失败 -> BLOCKED，保留旧 document/mode/trial/active_valve；start/reset/rearm 均拒绝
```

- “manual/TTL trigger”与“呼气门控”是串联的两个门：外部/手动事件负责给当前 trial 放行到呼吸等待，呼气阈值才负责刺激开阀。不要让 manual/TTL 直接调用 valve writer。
- `下一 trial` 是显式跳过，不等于 `手动触发`。这两个意图必须保留不同 API、事件名和中文文案。
- `ProtocolTrial.trigger` 是当前 trial 默认/声明模式。用户覆盖只存在于执行状态，并在进入下一 trial 时清除；不得修改 parser 输出以保存运行时选择。

### ProtocolExecutor / ProtocolView / MainController 边界

| 组件 | 应负责 | 禁止负责 |
|---|---|---|
| `ProtocolExecutor` | 模式与状态合法性、统一 readiness 校验、trigger 去重/事件身份校验、模式切换原子清理、进入既有呼吸门控、trial 推进、事件与 snapshot | Qt 控件、NI 读取、直接 HAL/驱动访问 |
| `ProtocolView` | 互斥模式选择、手动触发意图、中文展示、按 snapshot 设置控件状态 | trial index、pulse latch、readiness/安全真值、阀门动作、直接调用 executor/HAL |
| `MainController` | signal/slot 编排、构造 readiness 快照、转发不可变 pulse payload、候选协议原子替换、发布日志/UI、通过 `_write_protocol_valve()` 衔接 `ValveService` | 复制 executor 状态机、给旧 pulse 补当前 epoch、AI6 边沿算法、直接改执行模型字段 |
| HAL / Worker / TTL service | 单一 Dev1 AI task 的 AI0/AI6 共享采集与解复用、阈值/迟滞/去抖、捕获 opaque epoch/sequence、一次上升沿不可变事件与错误上报 | 创建并发的第二个 Dev1 AI task、选择 protocol trial、解释 epoch 业务含义、判断 manual/ttl 模式、呼吸门控、开阀 |

### 统一 readiness 与状态转换矩阵

- `SAFE` 只表示当前安全状态，不代表硬件已连接、自检通过、MFC 流量设定已建立或 TTL 输入 task 已创建。执行入口必须显式消费这些独立条件。
- 通用 readiness：有效 `ProtocolDocument`、`telemetry.connected=True`、基础硬件/AI0 自检通过、`flow_setpoints_ready=True`、`safety_state=SAFE`；TTL 布防/接受额外要求共享采集链路包含可用 AI6。AI6 不可用时允许 AI0-only/manual 安全降级，不得把 TTL 专属失败混入 manual capability。Mock HAL 同样通过明确 ready 状态满足契约，不得靠省略检查绕过。
- `set_trigger_mode()` 状态矩阵：`READY` 可更新 override 但保持 `READY`；合法运行等待态清理后进入 `WAITING_TRIGGER`；`TRIGGERED` 先关活动阀，成功后才切换；`IDLE/BLOCKED/STOPPED/COMPLETED` 或任一 readiness 不满足时拒绝且原状态不变。
- `start/reset/rearm` 不得覆盖 `active_valve`。关阀失败后的 `BLOCKED` 只能先通过既有 stop/安全关闭路径重试；安全阻断且无活动阀时可按显式 `rearm_current` 恢复当前 trial，`STOPPED` 可显式 restart 到 trial 0；关闭成功且旧态清理完成后，才能 reset 或替换协议。
- readiness 是时序语义而非单一布尔值：动作提交前不足只拒绝该动作；已经布防/运行后丢失则是安全事件，必须失效 epoch、阻断并尝试关阀。
- executor 是状态矩阵与拒绝原因的唯一业务真值；snapshot 从同一规则计算 capability，controller 只组装输入，View 只渲染。

### 当前代码状态与具体修改点

- `app/models/protocol.py` 已定义 `TriggerMode.MANUAL/TTL`，`ProtocolTrial.trigger` 是 frozen 字段；复用它，不新增字符串常量体系。
- `app/models/protocol_execution.py` 当前没有触发前等待态，snapshot 只暴露通用 `can_start/can_stop/can_advance`；需要增加模式和触发动作能力字段。
- `app/services/protocol_executor.py` 当前 `start()` 与每次 `_prepare_after_advance()` 都直接进入 `WAITING_EXHALE`。3.3 的核心改动是插入 `WAITING_TRIGGER`，并让匹配的 manual/TTL 事件成为唯一入口；已有 `_enter_waiting()`、`_trigger_current()`、`_finish_triggered_trial()`、超时和安全关闭逻辑应复用。
- 当前 `ProtocolExecutor.reset()` 会直接替换整个 state，`start()`/`_enter_waiting()` 也未防止残留 `active_valve` 被覆盖；必须按 AC10 收紧，不能让 close_failed 的恢复依据丢失。
- `app/views/protocol_view.py` 已有未连接、始终禁用的 `_manual_trigger_button` 和 `_ttl_trigger_button`；不要再叠加第三套控件。重构为模式选择与手动动作，并由 snapshot 驱动。
- `app/views/main_window.py` 当前只连接 load/start/stop/next；需要连接 mode/manual signals 到 controller。
- `app/controllers/main_controller.py` 已持有唯一 `ProtocolExecutor`，`_protocol_tick_timer` 每 50ms tick，`handle_breath_samples()` 传入校准后样本，`_write_protocol_valve()` 复用安全关闭，`_publish_protocol_result()` 统一记录日志。扩展这些接入点，不另建 trigger controller。
- `AppState` 中 `telemetry.safety_state` 默认可为 `SAFE`，而 `telemetry.connected`、`hardware_ready`、`flow_setpoints_ready` 分别独立；当前协议 start/snapshot 只传 safety state。3.3 必须补齐统一 readiness，不能等到最终开阀才由 `ValveService` 发现未就绪。
- `app/services/hal.py`、`MockHAL`、`RealHAL`、`HardwareWorker` 当前只有 AI0 呼吸输入，没有 AI6/TTL 契约；TTL 功能必须补齐这一链路。现有 `RealHAL._ai_task` 已占用 Dev1 Analog Input subsystem，因此必须扩展为 AI0/AI6 多通道单 task，不能另建 AI6 task。
- `config/default_config.json` 当前有门控超时配置但没有 TTL 输入配置；新增通用默认值，本机设备名仍允许由 local config 覆盖。

### 必须保留的 3.2 安全行为

- 非 `SAFE` 下不得进入等待呼气、不得推进 trial、不得开阀。
- 安全中断、stop、模式切换或协议替换时，存在活动阀必须调用关闭 writer；关闭失败保留 `active_valve` 和 `BLOCKED`，以便重试。
- `safety_close=True` 只用于关阀；任何误用于开阀的路径必须继续被 `ValveService` 拒绝。
- `BLOCKED` 且无活动阀时 tick/安全更新不得重复生成 `safety_block` 日志。
- 呼吸阈值判断继续复用 `GatingService` 和校准后的样本；不要复制阈值算法或并行订阅呼吸 worker。
- 3.2 的 `breath_gate_timeout_ms/action/max_retries` 只从进入 `WAITING_EXHALE` 时开始计时；等待 manual/TTL 的时间不应误触发呼吸超时。

### TTL 实现守卫

- 项目上下文明确外部 TTL 输入为 NI USB-6001 `Dev1/AI6`。由于它是模拟输入映射，必须通过高/低电压阈值与迟滞判定边沿，不能把每个高电平样本都当作 pulse。
- NI 官方对多功能 DAQ 的约束是每台设备同时最多一个 Analog Input task（硬件定时和软件按需均为 1）；USB-6001 只有一个 ADC，20 kS/s 为所有活动 AI 通道共享的 aggregate rate。AI0/AI6 必须放入同一 task，1 kHz 每通道需求仍远低于该 aggregate 上限。
- `nidaqmx.Task` 支持在一个 task 中添加多个 AI voltage channel；共享任务宜使用硬件定时缓冲采样，避免把 1 kHz 依赖于 Windows/Python 软件 `sleep` 精度。Worker 负责按固定 channel mapping 解复用和维持既有 AI0 100Hz 上游契约。
- TTL 采集频率和去抖参数进入配置，测试必须使用确定性样本序列。不要在 UI 线程 `sleep`，也不要用 `QTimer` 轮询 NI 设备。
- 模式切换建议使用单调递增 epoch/token：切换、stop、reset、协议替换、安全中断时递增；带旧 token 的 queued pulse 被 executor 忽略。等价设计可以接受，但必须由自动化测试证明旧事件不会推进新状态。
- epoch/token 必须在 TTL service 布防及 pulse 产生时捕获到不可变 payload 中。若 signal 只携带 timestamp，而 controller 在 delivery 时读取当前 epoch，切换前排队的旧 pulse 会被错误标记为新事件；禁止这种实现。
- 从 TTL 切到 manual 再切回 TTL 时，若 AI6 从未回落到低阈值以下，即使 epoch 已变化也不能把当前高电平视为新 rise；epoch 负责事件世代，迟滞 latch 负责物理边沿，两者不可互相替代。
- TTL error 是硬件输入故障，不是“暂时没有脉冲”。错误必须让 controller/executor 进入可见的阻断态，并保持危险输出关闭。
- AI6 在连接/自检阶段不可用属于 TTL 专属 readiness 不足，应安全降级为 AI0-only 并允许 manual；共享 task 在运行中读取失败会同时影响呼吸与 TTL，必须作为运行时 readiness 丢失阻断整个协议执行。

### Project Structure Notes / File Structure Requirements

- 主要更新：
  - `app/models/protocol_execution.py`
  - `app/services/protocol_executor.py`
  - `app/controllers/main_controller.py`
  - `app/views/protocol_view.py`
  - `app/views/main_window.py`
  - `app/services/hal.py`
  - `app/services/mock_hal.py`
  - `app/services/real_hal.py`
  - `app/workers/hardware_worker.py`
  - `app/models/__init__.py`、`app/services/__init__.py`（仅在新增导出时）
  - `config/default_config.json`
  - `config/local_config.example.json`
- 建议新增：
  - `app/services/ttl_trigger_service.py`
  - `tests/test_ttl_trigger_service.py`
- 主要更新测试：
  - `tests/test_protocol_executor.py`
  - `tests/test_integration_gating.py`
  - `tests/test_protocol_view.py`
  - TTL/HAL/worker 相关测试（可新建 `tests/test_ttl_input.py`）
- 不要把 TTL 实验脚本、样本输出、事件日志或临时协议放在项目根目录。来源：`docs/project-structure.md#新增文件放置规则`

### Testing Requirements

- 执行器与 TTL detector 优先纯单元测试，使用可注入 clock/样本，不依赖真实硬件或真实 `sleep`。
- Controller 测试验证边界和原子性；UI 测试验证 signal、中文显示和 enablement；真实 NI 设备结果不能替代自动化测试。
- 特别覆盖竞态：模式切换前 pulse 在切换后才送达、双击 manual、TTL→manual→TTL 全程高电平、stop 后 queued pulse、close_failed 后误 start/reset、协议替换清理失败、非 SAFE 恢复后的陈旧 pulse。
- 全量验证遵循项目命令 `python -m pytest` 与 `python -m ruff check app tests`；保持 ruff `py311`、120 字符行宽约定。

### Previous Story Intelligence

- 3.1 已建立 `ProtocolDocument/ProtocolTrial/TriggerMode`、parser、加载失败原子性和协议页摘要；3.3 必须消费 `trigger`，不能另建协议模式模型。
- 3.1 审查曾漏掉读取异常及 `NaN/inf`。TTL 阈值、去抖、采样率和电压样本同样必须拒绝非有限/非法配置，读取异常必须转成结构化中文错误。
- 3.2 已建立唯一 `ProtocolExecutor`、门控状态模型、50ms controller tick、校准后呼吸样本接入、ValveService writer、结构化日志和 UI snapshot。3.3 是扩展，不是重写。
- 3.2 复审重点是关阀失败恢复、非 SAFE 不得推进、UI enablement 不能代替服务校验、`safety_close` 不得打开阀、避免 BLOCKED 日志风暴。模式切换和 TTL queued signal 必须继承这些防线。
- 3.2 最后记录全量 `162 passed` 与 ruff 通过；实现本 story 后至少需要完整回归这些既有测试。

### Git Intelligence

- 当前基线提交：`fe9d1259a6115e58d14887a3b0948cebb06b0ddb`。
- 相关近期提交：
  - `953c1ea`：收紧呼吸门控安全关闭复审问题。
  - `af969d0`：修复呼吸门控安全关闭审查问题。
  - `6fd7256`：实现呼吸门控刺激执行。
  - `3cfb87e`：修复协议解析审查问题。
  - `ed2606c`：实现协议文件解析与加载反馈。
- 近期实现模式是“模型/纯服务 -> Controller/UI 接入 -> 单元/集成/UI 回归 -> 安全复审”。本 story 应沿用，并避免引入 async 框架、数据库或新依赖。

### 明确不做

- 不实现 Story 3.4 的阀门动作 p95 抖动统计、20ms 质量门、严重超限暂停策略或专用低抖动 actuation worker。
- 不实现 Story 3.5 的 `{Timestamp}_{Subject}_{Condition}.raw` / `.log` 会话文件创建与磁盘故障处理。
- 不改变 3.1 的协议格式、trigger 枚举值或 parser 兼容范围，除非新增测试证明真实输入契约不足。
- 不实现 SuperLab 特定网络/API 协议；本 story 的集成边界是 NI AI6 TTL 电平输入。
- 不让 ProtocolView、MainController 或 TTL service 直接写阀门；所有危险动作继续经过 `ProtocolExecutor -> controller writer -> ValveService -> HAL`。
- 不在本 story 提前实现自动清洗、配置 UI 或主阀/流量补偿自动化。

### References

- [Source: docs/sprint-artifacts/sprint-status.yaml#development_status]
- [Source: docs/epics.md#Epic-3-协议执行与数据记录]
- [Source: docs/epics.md#Story-3.3-手动与-TTL-触发模式]
- [Source: docs/prd.md#FR5：协议执行]
- [Source: docs/architecture.md#MVC--Worker]
- [Source: docs/architecture.md#HAL-硬件抽象]
- [Source: docs/architecture.md#安全策略]
- [Source: docs/architecture.md#协议与数据]
- [Source: docs/architecture.md#配置来源]
- [Source: docs/architecture.md#测试策略]
- [Source: docs/project-context.md#关键硬件映射]
- [Source: docs/project-structure.md#app-目录]
- [Source: docs/project-structure.md#新增文件放置规则]
- [Source: docs/ux-design.md#协议页]
- [Source: docs/ux-design.md#文案规范]
- [Source: docs/sprint-artifacts/3-1-protocol-file-parsing-txtcsv.md]
- [Source: docs/sprint-artifacts/3-2-breath-gated-stimulation.md]
- [Source: app/models/protocol.py]
- [Source: app/models/protocol_execution.py]
- [Source: app/services/protocol_executor.py]
- [Source: app/controllers/main_controller.py]
- [Source: app/views/protocol_view.py]
- [Source: app/views/main_window.py]
- [Source: app/services/hal.py]
- [Source: app/services/mock_hal.py]
- [Source: app/services/real_hal.py]
- [Source: app/workers/hardware_worker.py]
- [Source: app/services/valve_service.py]
- [Source: tests/test_protocol_executor.py]
- [Source: tests/test_integration_gating.py]
- [Source: tests/test_protocol_view.py]
- [External: NI - Number of Parallel DAQmx Tasks on NI Multifunction Devices](https://knowledge.ni.com/KnowledgeArticleDetails?id=kA00Z0000019KWYSA2&l=en-US)
- [External: NI - USB-6001 single ADC and aggregate sample-rate behavior](https://knowledge.ni.com/KnowledgeArticleDetails?id=kA00Z000000kIz9SAE&l=en-GB)
- [External: NI-DAQmx Python - multiple AI channels in one task and hardware timing](https://nidaqmx-python.readthedocs.io/en/latest/)

## Dev Agent Record

### Agent Model Used

待开发智能体填写

### Debug Log References

### Completion Notes List

- Ultimate context engine analysis completed - comprehensive developer guide created.

### File List

## Change Log

- 2026-07-18：创建 Story 3.3 手动与 TTL 触发模式，状态设为 ready-for-dev。
- 2026-07-18：补齐统一 readiness、模式切换状态矩阵、关阀失败恢复、TTL 事件身份与边界竞态测试要求。
- 2026-07-18：收敛 AI0/AI6 单 task 采集、协议替换失败、显式恢复路径及运行中 readiness 丢失语义。
