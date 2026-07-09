---
baseline_commit: 3cfb87e
depends_on:
  - docs/sprint-artifacts/3-1-protocol-file-parsing-txtcsv.md
---

# Story 3.2: 呼吸门控刺激

Status: review
Epic: 3 - 协议执行与数据记录
Story Key: 3-2-breath-gated-stimulation
Story ID: 3.2

## Story

作为研究人员，
我需要在 trial 准备完成后等待呼吸信号达到呼气阈值再执行刺激，
以便让气味刺激与受试者呼吸周期对齐，并为后续手动/TTL 触发、低抖动阀门动作和会话记录建立可复用的执行状态机。

## Acceptance Criteria

1. **加载协议后才能进入门控执行**
   - Given 当前没有有效 `ProtocolDocument`；
   - When 用户点击开始、下一 trial 或任何门控执行入口；
   - Then 系统不得进入等待呼吸、触发或阀门准备状态。
   - And UI 显示中文提示：“请先加载有效协议”或等价可操作文案。
   - Given 已通过 Story 3.1 加载有效 `ProtocolDocument`；
   - When 用户启动门控流程；
   - Then 系统从 `document.trials[0]` 开始，按 `ProtocolDocument.trials` 的原始顺序准备 trial。

2. **trial 准备完成后等待呼气阈值**
   - Given 当前 trial 已准备完成，硬件处于 `SAFE`，且呼吸数据未过期；
   - When 呼吸样本经 `AppState.apply_calibration()` 转换后进入门控判断；
   - Then 系统等待样本低于或等于当前 `exhale_threshold`，并将状态显示为“等待呼气”。
   - And 不能改用吸气阈值、原始未校准样本或 UI 文本判断来触发。
   - And 等待期间不得提前打开气味阀、主阀或写入 Alicat 危险流量。

3. **达到呼气阈值后触发当前 trial**
   - Given 系统处于当前 trial 的“等待呼气”状态；
   - When 门控服务报告 `GatingState.EXHALE`；
   - Then 系统通过现有安全阀门路径打开当前 trial 的目标阀门，并在 `duration_ms` 到达后关闭该阀门。
   - And 触发事件必须记录 `trial_id`、阀门通道、计划时长、触发时间、样本值和当前阈值。
   - And UI 显示“已触发”状态、当前 trial、目标阀门和剩余或计划刺激时长。
   - And 阀门动作必须通过现有 `ValveService.set_valve()` 或 controller 中对它的封装调用，不能直接调用 NI/HAL 驱动。
   - And 若 MFC 流量设定未建立、`duration_ms <= 0`、阀门打开失败或关闭失败，系统必须进入阻断/停止状态并显示中文原因，不得继续推进为成功 trial。

4. **超时后按配置跳过或重试**
   - Given 当前 trial 处于“等待呼气”状态；
   - When 等待时间超过配置的 `breath_gate_timeout_ms`；
   - Then 系统按配置 `breath_gate_timeout_action` 执行：
     - `skip`：将当前 trial 标记为“已跳过”，推进到下一 trial，并记录跳过事件；
     - `retry`：保持当前 trial，增加重试次数，重新进入“等待呼气”，并记录重试事件。
   - And 重试次数不得无限增长，超过 `breath_gate_max_retries` 后必须跳过当前 trial 或停止流程，并显示中文提示。
   - And 默认值应保守：`timeout_ms=5000`、`action=skip`、`max_retries=1`，除非 `config/default_config.json` 已提供项目约定值。
   - And 超时判断不能完全依赖新呼吸样本到达；呼吸数据停止时也必须能超时或被标记为安全阻断。

5. **安全状态中断门控**
   - Given 门控等待、触发或 trial 间切换过程中安全状态变为 `LOW_FLOW`、`DATA_STALE`、硬件断开或其他非 `SAFE` 状态；
   - When 状态变化被 controller 或 service 观察到；
   - Then 系统立即停止当前门控流程，关闭或保持关闭危险输出，并显示中文安全提示。
   - And 若已有阀门被打开，必须走现有安全关闭路径或 `ValveService` 关闭路径，不得留下打开状态。
   - And 该中断必须写入门控事件日志。

6. **协议页显示门控运行状态**
   - Given 用户在“协议”页加载协议并启动门控流程；
   - When 流程进入 `idle`、`ready`、`waiting_exhale`、`triggered`、`skipped`、`completed`、`blocked` 或 `stopped`；
   - Then UI 用简体中文显示对应状态、当前 trial 编号、目标阀门、触发模式、等待时长和最近事件。
   - And 开始、停止、下一 trial/手动推进按钮的启用状态必须反映当前安全状态和是否有有效协议。
   - And UI 层只渲染状态和发出用户意图，不保存执行状态机的真实来源。

7. **事件日志可追踪**
   - Given 门控流程发生开始等待、呼气触发、超时、跳过、重试、安全阻断、停止或完成；
   - When 事件发生；
   - Then 系统通过现有 logging 体系记录结构化日志，至少包含 `event`、`trial_id`、`trial_index`、`valve`、`timestamp`、`gate_state`、`sample_value`、`exhale_threshold`、`safety_state` 和 `result`。
   - And 本 story 不要求写入最终 `.log` 会话文件，但事件字段必须能被 Story 3.5 复用。

8. **不会破坏 3.1 协议加载原子性**
   - Given 已加载一个有效协议并可能正在查看 trial 预览；
   - When 门控流程启动、停止、跳过或完成；
   - Then 不得修改 `ProtocolDocument`、`ProtocolTrial` 或 parser 的解析结果。
   - And 解析失败仍不得覆盖上一份有效协议。
   - And 3.1 的 parser、ProtocolView 冒烟测试和控制器加载测试必须继续通过。

## Tasks / Subtasks

- [x] 建立协议执行状态模型（AC: 1, 2, 3, 4, 5, 7, 8）
  - [x] 新增 `app/models/protocol_execution.py`，定义门控运行状态、当前 trial 指针、重试次数、等待开始时间、最近事件等轻量 dataclass 或 enum。
  - [x] 状态至少覆盖 `idle`、`ready`、`waiting_exhale`、`triggered`、`skipped`、`completed`、`blocked`、`stopped`。
  - [x] 状态对象引用 `ProtocolDocument` 和 `ProtocolTrial`，但不得修改 3.1 的 frozen 协议对象。
  - [x] 更新 `app/models/__init__.py` 导出新模型。

- [x] 实现门控执行服务（AC: 1, 2, 3, 4, 5, 7, 8）
  - [x] 新增 `app/services/protocol_executor.py` 或同等命名服务，封装开始、停止、准备当前 trial、处理呼吸样本、处理超时和推进 trial。
  - [x] 复用现有 `GatingService` 的阈值判断和 `GatingState.EXHALE`，不要复制第二套阈值算法。
  - [x] 输入样本必须使用 `AppState.apply_calibration()` 后的值，保持与 Story 2.6/2.7 的校准体验一致。
  - [x] 服务不得直接导入或调用 PySide6 组件。
  - [x] 服务不得直接访问 NI、Alicat 或 HAL；如需要开关阀门，暴露可注入动作回调或由 controller 调用 `ValveService`。
  - [x] 触发后必须请求打开当前 trial 的目标阀门，并在 `duration_ms` 到达后请求关闭；本 story 不要求达到 20ms 抖动目标，但必须记录 planned/actual 时间供 3.4 复用。
  - [x] 超时策略从配置读取，缺省使用 `breath_gate_timeout_ms=5000`、`breath_gate_timeout_action="skip"`、`breath_gate_max_retries=1`。
  - [x] 提供可注入 clock 或 tick 方法，使超时和刺激结束不依赖真实睡眠，便于自动化测试。
  - [x] 每个门控事件返回结构化结果，供 controller 更新 UI 和日志。
  - [x] 更新 `app/services/__init__.py` 导出服务和事件类型。

- [x] 接入 `MainController` 的协议执行编排（AC: 1, 2, 3, 4, 5, 6, 7, 8）
  - [x] 在 `MainController.__init__` 中创建门控执行服务，复用现有 `state.loaded_protocol`、`state.telemetry`、`gating_service`、`valve_service` 和安全状态。
  - [x] 在 `handle_protocol_file_selected()` 成功加载新协议后重置旧的门控执行状态到 `ready` 或 `idle`，避免 trial 指针沿用上一份协议。
  - [x] 扩展 `handle_breath_samples()`：保留现有图形/校准/阈值 transition 日志，再将校准后的批次传给门控执行服务。
  - [x] 为等待超时和刺激结束接入 Qt `QTimer`、controller tick 或等价机制，确保无新样本时也能推进超时/关闭阀门；不要在 UI 线程 `sleep`。
  - [x] 处理 `LOW_FLOW`、`DATA_STALE`、硬件断开和 stop/reset：门控状态必须进入 `blocked` 或 `stopped`，并关闭或保持关闭危险输出。
  - [x] 不要改变 3.1 的解析失败原子性，不要在解析失败分支清空上一份有效 `loaded_protocol`。

- [x] 扩展协议页 UI（AC: 1, 3, 4, 6）
  - [x] 在 `app/views/protocol_view.py` 中补充开始、停止、下一 trial/继续、门控状态、当前 trial、目标阀门、等待时长、最近事件显示。
  - [x] 现有 `开始`、`手动触发`、`TTL 触发` 预留按钮可重命名或拆分，但本 story 只启用呼吸门控启动/停止所需控件。
  - [x] UI 文案全部使用简体中文，错误说明要包含“发生了什么”和“下一步怎么做”。
  - [x] UI 不直接访问 `ProtocolDocument.trials` 做推进决策；推进由 controller/service 状态驱动。
  - [x] `MainWindow.update_gating_state()` 已同步校准页和预检页；协议页如显示门控状态，应通过 controller 显式渲染，不要依赖私有属性轮询。

- [x] 增加配置项（AC: 4）
  - [x] 在 `config/default_config.json` 中加入 `breath_gate_timeout_ms`、`breath_gate_timeout_action`、`breath_gate_max_retries`。
  - [x] 配置读取应兼容旧配置缺字段的情况。
  - [x] 不新增第二个真实配置来源。

- [x] 补充自动化测试（AC: 全部）
  - [x] 新增 `tests/test_protocol_executor.py`，覆盖开始前无协议、按顺序准备 trial、等待呼气、触发、跳过、重试、完成和停止。
  - [x] 覆盖阀门动作路径：达到呼气阈值后请求打开目标阀，`duration_ms` 到达后请求关闭；打开/关闭失败时进入阻断或停止状态。
  - [x] 覆盖无新呼吸样本时的等待超时，避免只有样本到达才会跳过。
  - [x] 新增或扩展 controller 测试，验证 `handle_breath_samples()` 会把校准样本送入门控流程，并在 `EXHALE` 时触发当前 trial。
  - [x] 新增安全中断测试：`LOW_FLOW`、`DATA_STALE` 或断连时门控进入 `blocked/stopped`，不会打开阀门，必要时调用关闭路径。
  - [x] 新增 UI 冒烟测试或扩展 `tests/test_protocol_view.py`，验证中文状态、按钮启用/禁用和最近事件渲染。
  - [x] 回归运行 3.1 相关测试：`tests/test_protocol_parser.py`、`tests/test_protocol_view.py`。

- [x] 工程验证（AC: 全部）
  - [x] 运行 `D:\miniconda3\envs\code\python.exe -m pytest tests/test_protocol_executor.py`。
  - [x] 运行 `D:\miniconda3\envs\code\python.exe -m pytest tests/test_protocol_parser.py tests/test_protocol_view.py`。
  - [x] 运行 `D:\miniconda3\envs\code\python.exe -m pytest`。
  - [x] 运行 `D:\miniconda3\envs\code\python.exe -m ruff check app tests`。

## Dev Notes

### 需求来源

- PRD FR5.2 要求刺激前等待呼吸信号超过呼气阈值，实现呼吸门控。来源：`docs/prd.md#FR5：协议执行`
- Epic 3 目标是协议执行与数据记录，覆盖 FR2.1、FR2.2、FR2.3、FR5.1、FR5.2、FR5.3。Story 3.2 只实现呼吸门控刺激，不实现 TTL 触发模式、低抖动指标或 `.raw/.log` 会话文件。来源：`docs/epics.md#Epic-3-协议执行与数据记录`
- Story 3.2 验收要求包括：trial 准备完成后等待呼吸信号超过呼气阈值；超时后按配置跳过或重试并记录事件；UI 显示等待、触发、跳过等状态。来源：`docs/epics.md#Story-3.2-呼吸门控刺激`

### 3.1 ProtocolDocument 复用说明

- 3.1 已实现 `app/models/protocol.py`：`ProtocolDocument`、`ProtocolTrial`、`TriggerMode`，并在 `AppState.loaded_protocol` 保存当前有效协议。3.2 必须复用这些类型，不要新增第二套协议模型。
- 3.1 的 `ProtocolDocument`/`ProtocolTrial` 是 frozen dataclass。3.2 的 trial 指针、运行状态、重试次数和事件历史必须放在新的执行状态对象中，不能写回 `ProtocolDocument.trials`。
- 3.1 的 parser 已保证 trigger、valve、timing、duration 和 metadata 结构化，并校验当前硬件变体允许的阀门范围。3.2 可信任已加载 document 的字段形状，但仍必须在执行前通过安全状态和 `ValveService` 守卫危险动作。
- 3.1 的 `handle_protocol_file_selected()` 成功时才写入 `state.loaded_protocol`，失败时保留上一份有效协议。3.2 不得破坏该原子性。
- 3.1 当前 sprint 状态为 `review`，但 Dev Agent Record 记录 parser、UI、全量 pytest 和 ruff 均已通过，且审查发现的文件读取/非有限数值问题已修复。3.2 可以基于这些代码契约继续，但仍需回归 3.1 测试。

### 架构约束

- 项目采用 MVC + Worker + HAL。门控状态机和超时逻辑应在 `services/` 与 `models/` 中实现；View 只展示状态和发出按钮意图；Controller 负责编排服务、状态、日志和 UI。来源：`docs/architecture.md#分层结构`
- 所有真实硬件访问必须经过 HAL，危险动作必须经过安全守卫。3.2 不允许在 UI 或 executor 中直接调用 NI/Alicat 驱动。来源：`docs/architecture.md#HAL-硬件抽象`
- 现有 `GatingService` 已处理 `inhale_threshold`、`exhale_threshold`、`process_batch()`、`GatingState.EXHALE` 和非 SAFE 时 `BLOCKED`。3.2 应扩展使用它，而不是复制阈值算法。
- 现有 `MainController.handle_breath_samples()` 已将呼吸样本送给校准页/预检页，并用 `AppState.apply_calibration()` 后的样本做阈值 transition 日志。3.2 应在这个路径上接入门控执行，避免并行订阅 worker 造成状态竞争。
- 现有 `ValveService.set_valve()` 会解析当前硬件变体映射、检查 `flow_setpoints_ready`、走 `SafetyManager.guard_command()` 并处理主阀。任何真实阀门动作必须复用该服务。
- 3.2 可以使用现有 `ValveService` 做“门控后开阀、持续 duration、关阀”的基本刺激执行；20ms 抖动统计和质量门仍留给 Story 3.4。
- 默认配置以 `config/default_config.json` 为唯一默认来源。新增门控超时配置必须进入同一配置体系。来源：`docs/architecture.md#配置来源`

### UX 约束

- 协议页应显示当前 trial、下一个气味、触发模式、剩余时间和运行状态；3.2 至少显示当前 trial、目标阀门、触发模式、等待/触发/跳过/完成状态和最近事件。来源：`docs/ux-design.md#协议页`
- 开始、暂停、停止按钮必须遵守安全联锁。3.2 的开始/停止按钮启用状态不能只由 UI 控制，Controller/Service 也必须拒绝非法状态转换。来源：`docs/ux-design.md#协议页`
- 所有面向用户的按钮、标签、错误和提示使用简体中文。错误提示要说明发生了什么以及用户下一步应做什么。来源：`docs/ux-design.md#文案规范`

### 当前代码状态

- `app/models/app_state.py` 已有 `loaded_protocol: ProtocolDocument | None`、`inhale_threshold`、`exhale_threshold`、`signal_offset`、`signal_gain`、`telemetry.gating_state` 和 `get_active_valve_map()`。
- `app/services/gating_service.py` 已有门控阈值判断，但它只描述呼吸阈值状态，不知道协议 trial、超时、跳过或重试。3.2 应新增协议执行服务包裹这些业务语义。
- `app/controllers/main_controller.py` 当前在 `handle_breath_samples()` 中按 100Hz 处理校准后样本，并记录 `threshold_cross`。3.2 应保持这些日志，不要删除或改名。
- `app/views/protocol_view.py` 当前只有加载按钮、摘要、错误、trial 预览，`开始`、`手动触发`、`TTL 触发` 默认禁用。3.2 可以启用与呼吸门控相关的开始/停止，但手动/TTL 模式属于 Story 3.3。
- `app/views/main_window.py` 已把 `ProtocolView.load_requested` 连接到 `MainController.handle_protocol_file_selected()`，并将呼吸样本同步给校准页和预检页。
- `app/services/valve_service.py` 当前 `set_valve()` 的 `source` 固定为 `"manual-toggle"`。如果 3.2 需要更准确的日志来源，可小范围扩展参数，但必须保持现有手动阀门测试行为不变。

### File Structure Requirements

- 新模型：`app/models/protocol_execution.py`
- 新服务：`app/services/protocol_executor.py`
- 可能更新：`app/models/__init__.py`、`app/services/__init__.py`、`app/models/app_state.py`、`app/controllers/main_controller.py`、`app/views/protocol_view.py`、`app/views/main_window.py`、`config/default_config.json`
- 新测试：`tests/test_protocol_executor.py`
- 可能更新测试：`tests/test_integration_gating.py`、`tests/test_protocol_view.py`、`tests/test_protocol_parser.py`
- 不要把样例协议、临时执行脚本、事件输出或实验数据放在项目根目录。来源：`docs/project-structure.md#新增文件放置规则`

### Testing Requirements

- 单元测试优先覆盖纯执行服务，不需要 Qt 或真实硬件。
- Controller 测试需要验证服务接入点、校准样本路径、安全阻断和 3.1 加载原子性。
- UI 测试只做轻量冒烟，验证中文状态和控件启用状态，避免把业务断言塞进 View。
- 回归测试必须覆盖 3.1 parser 和 ProtocolView，因为 3.2 会扩展同一页面和 controller。
- 全量验证使用项目既有命令：`pytest` 与 `ruff check app tests`。来源：`docs/project-structure.md#pytest、ruff、CI 和 PyInstaller`

### Previous Story Intelligence

- 3.1 已建立协议模型、parser、ProtocolView 和 controller 加载路径，完成后记录 `138 passed` 和 `ruff` 通过。3.2 不应重写这些代码，只应扩展执行层。
- 3.1 审查发现说明：文件读取异常和 `NaN`/`inf` 这类边界值容易漏掉。3.2 的超时配置、等待时间、重试次数和样本值也必须拒绝非有限数或非法配置。
- 3.1 明确不做 trial 执行状态机、呼吸门控等待、手动/TTL 推进、低抖动阀门动作或 `.raw/.log` 写入。3.2 正是补上呼吸门控执行状态机，但仍不扩大到 3.3、3.4、3.5 的范围。

### Git Intelligence

- 最近提交：
  - `3cfb87e 修复协议解析审查问题`
  - `ed2606c 实现协议文件解析与加载反馈`
  - `91b3830 统一项目文档与工程基线`
- 近期模式是先实现独立模型/服务，再接 Controller/UI，并补 parser/UI/controller 回归测试。3.2 应沿用该节奏。
- 不要引入 pandas、async 框架、数据库或新的外部依赖；当前技术栈已能完成本 story。

### 明确不做

- 不实现手动触发与 TTL 触发模式切换；这是 Story 3.3。
- 不实现 p95 抖动统计、20ms 抖动质量门或低抖动 worker；这是 Story 3.4。
- 不实现最终 `{Timestamp}_{Subject}_{Condition}.raw` 和 `.log` 文件输出；这是 Story 3.5。
- 不扩展 3.1 的旧协议格式兼容范围，除非门控执行测试发现已解析字段不足。
- 不绕过 `SafetyManager`、`ValveService` 或 HAL 安全路径直接写硬件。

## References

- `docs/prd.md#FR5：协议执行`
- `docs/epics.md#Epic-3-协议执行与数据记录`
- `docs/epics.md#Story-3.2-呼吸门控刺激`
- `docs/architecture.md#分层结构`
- `docs/architecture.md#HAL-硬件抽象`
- `docs/architecture.md#协议与数据`
- `docs/ux-design.md#协议页`
- `docs/project-context.md#架构原则`
- `docs/project-structure.md#新增文件放置规则`
- `docs/sprint-artifacts/3-1-protocol-file-parsing-txtcsv.md`
- `app/models/protocol.py`
- `app/models/app_state.py`
- `app/services/gating_service.py`
- `app/services/valve_service.py`
- `app/controllers/main_controller.py`
- `app/views/protocol_view.py`
- `tests/test_gating_service.py`
- `tests/test_integration_gating.py`
- `tests/test_protocol_parser.py`
- `tests/test_protocol_view.py`

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Debug Log References

- `D:\miniconda3\envs\code\python.exe -m pytest tests/test_protocol_executor.py`：9 passed。
- `D:\miniconda3\envs\code\python.exe -m pytest tests/test_protocol_parser.py tests/test_protocol_view.py`：17 passed。
- `D:\miniconda3\envs\code\python.exe -m pytest`：150 passed。
- `D:\miniconda3\envs\code\python.exe -m ruff check app tests`：All checks passed。

### Completion Notes List

- 新增 `ProtocolExecutionState`、`ProtocolGateEvent` 和 `ProtocolExecutionSnapshot`，将 trial 指针、等待/触发/跳过/阻断状态与事件历史放在独立执行模型中，未修改 3.1 的 frozen 协议对象。
- 新增 `ProtocolExecutor`，复用 `GatingService` 的 `GatingState.EXHALE` 判断，支持开始、停止、等待呼气、触发、超时 skip/retry、完成和安全阻断；阀门动作只通过注入回调，由 controller 调用 `ValveService.set_valve()`。
- `MainController` 已接入协议加载 reset、校准后呼吸样本、Qt tick、LOW_FLOW/DATA_STALE/断连阻断和结构化 `protocol_execution` 日志；协议解析失败仍保留上一份有效 `loaded_protocol`。
- 协议页新增门控状态、当前 trial、目标阀门、触发模式、等待时长、最近事件，以及开始/停止/下一 trial 控件；手动/TTL 仍保留为后续 story。
- 新增默认配置 `breath_gate_timeout_ms=5000`、`breath_gate_timeout_action=skip`、`breath_gate_max_retries=1`，旧配置缺字段时由服务默认值兜底。

### File List

- `app/controllers/main_controller.py`
- `app/models/__init__.py`
- `app/models/protocol_execution.py`
- `app/services/__init__.py`
- `app/services/protocol_executor.py`
- `app/views/main_window.py`
- `app/views/protocol_view.py`
- `config/default_config.json`
- `tests/test_integration_gating.py`
- `tests/test_protocol_executor.py`
- `tests/test_protocol_view.py`
- `docs/sprint-artifacts/3-2-breath-gated-stimulation.md`
- `docs/sprint-artifacts/sprint-status.yaml`

## Change Log

- 2026-07-09：创建 Story 3.2 呼吸门控刺激，状态设为 ready-for-dev。
- 2026-07-09：实现呼吸门控执行状态机、controller/UI 接入、配置项和自动化测试，状态更新为 review。
