# 故事 1.2: Safe Start Airflow Interlock
Status: review
Epic: 1 - Safe Hardware Foundations
Story Key: 1-2-safe-start-airflow-interlock
Story ID: 1.2

## Story
作为实验室技术员，  
我希望系统在任何阀门/加热命令前都要求气流值高于安全阈值并持续监测，  
从而避免在无气流或气流骤降时出现过热或硬件损伤。

## Acceptance Criteria
1. **低流量阻断（AC1）**：当气流 < 阈值时，所有阀门/加热/流量指令均被短路阻断；UI footer/安全状态区在 <500ms 内闪烁红色“LOW FLOW”提示并给出重试建议。
2. **安全放行（AC2）**：当气流 ≥ 阈值且硬件自检已通过时，阀门/加热/流量指令按正常流程下发；UI 显示绿色“SAFE”且状态时间戳刷新。
3. **运行中跌落保护（AC3）**：在协议执行/手动控制过程中气流跌破阈值时，立即关闭相关阀门与加热，阻断后续指令，记录事件（时间戳、指令类型、原因），并在 UI 提示恢复步骤。
4. **阈值配置与持久化（AC4）**：阈值由配置/Options 页面提供，保存到持久化配置；非法值（负数/非数字）被拒绝并提示；阈值更新后立即影响安全判定逻辑。
5. **遥测一致性（AC5）**：硬件 Worker 以 5-10Hz 推送气流值与安全状态；UI 在数据陈旧>1s 时提示“数据过期”并默认阻断控制；日志写入包含最新气流值与判定状态。
6. **接口兼容性（AC6）**：Protocol 模式、Pre-test 阀矩阵、Flow Rate Apply、Connect/Reset/Stop 等入口均复用统一的安全校验接口，不允许绕过。

## Tasks / Subtasks
- [x] 建立安全状态判定与事件模型（AC1/AC3）。
- [x] 阀门/加热/流量控制前置安全守卫（AC1/AC2/AC6）。
- [x] 气流跌落监测与紧急关闭流程（AC3）。
- [x] 阈值配置校验与持久化联动 Options 页面（AC4）。
- [x] UI 安全状态展示与数据陈旧提示（AC1/AC5）。
- [x] 日志与遥测输出（AC3/AC5）。
- [x] 单元/集成测试覆盖安全阻断与恢复路径（AC1-AC6）。

## Dev Notes（story_requirements）
- 覆盖 FR1.2（安全互锁）并支撑 FR1.1/FR1.3/FR1.4：所有控制命令必须经过安全检查；跌落时执行安全关闭。
- 安全状态应独立于 UI，逻辑驻留在硬件 Worker/HAL，UI 仅订阅状态并在阻断时禁用操作。
- 阀门/加热命令需带上调用来源（Protocol/Pre-test/Flow Apply/Reset），以便日志追踪与 UI 提示。
- 优先保证实时性：安全检查与关闭逻辑在高优先级线程中执行；UI 仅响应信号。
- 容错：在气流传感器读数缺失/异常时，默认进入 LOW FLOW 阶段并阻断。

## Developer Context（developer_context_section）
- 架构：MVC + Worker（docs/architecture.md）；硬件安全逻辑在 Worker/Service，UI 为被动视图。
- 硬件：NI-USB-6001/6501 + RS232 质量流量控制器；气流传感器读数作为安全判定输入。
- UI/UX：全中⽂；全局 Footer 显示 SAFE/LOW FLOW + 气流值；Connect/Reset/Stop 按钮需受安全状态影响。
- 性能：安全状态推送 5-10Hz；气流跌落检测与关断<200ms；UI FPS ≥30 不得因安全逻辑受阻。
- 日志：安全事件写入数据记录通道（含时间戳、阀/加热 ID、命令、判定状态、原因）。
- 配置：阈值与串口/NI 配置在 `config/default_config.json`/Options 页面；阈值变更实时生效。

## Technical Requirements（technical_requirements）
- **安全判定**：封装 `SafetyState`（safe/low_flow/data_stale, airflow_value, threshold, updated_at, reason）；默认 low_flow。
- **读数输入**：硬件 Worker 从传感器/DAQ 读取气流值；当 last_updated >1s 或读数异常时标记 data_stale= true, safe=false。
- **命令守卫**：在 `SafetyManager`/硬件 Worker 对阀/加热/流量命令添加同步校验；失败返回错误码与中⽂提示，不进入下游。
- **跌落处理**：检测到从 safe→low_flow 的状态转移时，触发紧急关闭（关闭受影响阀门/停加热），抛出信号给 UI 与日志。
- **恢复路径**：气流恢复≥阈值且稳定若干周期（建议2-3个采样窗口）后才允许放行；状态机需防抖。
- **Protocol/Pre-test 集成**：Protocol Engine 与 Pre-test 阀矩阵/Flow Apply 统一调用安全守卫；禁止直接访问底层发送接口。
- **遥测与 UI**：Worker 经信号/槽推送 `{airflow, state, updated_at}`；UI 显示时间戳，过期提示并禁用按钮。
- **日志格式**：记录 `ts, cmd_type, target, airflow, threshold, state, reason, source`；异常情况下追加建议动作。
- **配置校验**：Options 页面保存阈值时进行范围校验（>0 且合理上限）；保存失败时提示且不改动现有阈值。

## Architecture Compliance（architecture_compliance）
- 安全逻辑在 Worker/Service 层（非 UI）；通过信号/槽将状态广播到控制器/UI。
- 命令经控制器→SafetyManager→HAL/Driver；禁止 UI 直接写硬件。
- 保持现有目录与命名：`app/controllers|workers|services|models`，与 architecture.md 一致。
- 日志沿用现有 Data Logger/应用日志通道，避免新建平行 logger。
- 线程安全：安全状态读写使用线程安全原语/Qt 线程上下文，避免竞态。

## Library & Framework Requirements（library_framework_requirements）
- Python 3.10+；PySide6 当前 6.7.2，最新 6.10.1（pip index）；若评估升级需验证 PyInstaller 打包。
- pyqtgraph 已装 0.13.7，最新 0.14.0，升级需回归 100Hz 绘制与 Qt 兼容性。
- nidaqmx 已装 0.9.0，最新 1.3.0，升级需确认 NI-DAQmx 驱动匹配与 API 变更。
- pyserial 3.5 为最新；保持。

## File Structure Requirements（file_structure_requirements）
- `app/workers/hardware_worker.py`：气流读取、状态机、紧急关断、信号发送。
- `app/services/safety_manager.py`（或新增）：安全守卫接口，集中判定与日志。
- `app/controllers/main_controller.py` / Protocol 引擎：在命令入口统一调用安全守卫；处理错误码并反馈 UI。
- `app/views/...`（Footer/Pre-test/Protocol/Options）：展示 SAFE/LOW FLOW/数据过期状态，禁用按钮，阈值配置入口。
- `config/default_config.json`：阈值与设备配置；持久化与 UI 一致。
- `tests/`：新增安全状态机、命令阻断、跌落恢复相关单测/集成测试。

## Testing Requirements（testing_requirements）
- 单元测试：安全状态机（safe↔low_flow↔stale）、阈值校验、命令守卫返回值。
- 集成/Mock 测试：模拟气流跌落，验证阀门/加热关闭与日志；Protocol/Pre-test/Flow Apply 入口被阻断。
- 性能测试：跌落检测到关断<200ms（模拟）；遥测推送 5-10Hz，无 UI 阻塞。
- 回归：阈值持久化读写与 Options 页面互通；日志格式与既有 logger 兼容。

## Previous Story Intelligence（previous_story_intelligence）
- Story 1.0 已建立 PySide6 MVC + Worker 框架、CI/打包；目录结构与命名需复用。
- Story 1.1 完成硬件自检与阻断：已有 `hardware_ready` 保护、NI/RS232 状态信号、中文错误提示。安全互锁应复用同一守卫路径，并在自检未通过或设备缺失时保持阻断。
- 已有日志与 CI 流水线可直接复用；保持中文提示一致性。

## Git Intelligence Summary（git_intelligence_summary）
- 仓库存在 git，最近可见提交仅初始提交（a9283e8），暂无可供参考的实现变更；实现时请创建新提交记录。

## Latest Tech Information（latest_tech_information）
- PySide6 最新 6.10.1（已装 6.7.2）：含 Qt bugfix；升级需验证 PyInstaller hidden-import 与字体打包。
- pyqtgraph 最新 0.14.0（已装 0.13.7）：升级需确认 100Hz 绘图性能与 Qt 兼容。
- nidaqmx 最新 1.3.0（已装 0.9.0）：升级前需确认 NI-DAQmx 驱动版本匹配，API 可能有差异。
- pyserial 3.5 为最新。

## Project Context Reference（project_context_reference）
- docs/project-context.md（范围/约束/安全目标）
- docs/epics.md（Epic 1 & Story 1.2 需求与 AC）
- docs/prd.md（FR1.2 安全互锁等需求）
- docs/architecture.md（MVC+Worker、安全/性能约束、目录结构）
- docs/ux-design.md（全局状态区、安全提示、页面布局）

## Story Completion Status（story_completion_status）
- 状态：in-progress
- 产物：docs/sprint-artifacts/1-2-safe-start-airflow-interlock.md
- 完成说明：Ultimate context engine analysis completed - comprehensive developer guide created.

## Dev Agent Record
### Context Reference
- 本故事文档；如有新增日志/调试记录请补充。

### Agent Model Used
- Codex (GPT-5)

### Debug Log References
- pytest -q（27 passed）

### Completion Notes List
- Task1（AC1/AC3）：引入 SafetyState（SAFE/LOW_FLOW/DATA_STALE）状态机与滞后恢复；检测读数异常/时间超阈值标记过期；硬件安全上报可覆盖流量判定；控制器/UI 状态文案统一；新增单元测试覆盖阈值、滞后、过期、异常读数与硬件覆盖。
- Task2（AC1/AC2/AC6）：新增统一安全守卫接口（SafetyManager.guard_command + MainController.ensure_safe_command），硬件未就绪/LOW_FLOW/DATA_STALE 时阻断命令，SAFE 且硬件就绪才放行，状态提示统一。
- Task3（AC3）：检测 SAFE→LOW_FLOW/过期/硬件故障转移时记录 last_shutdown_event，并以“紧急关闭”提示，防止后续命令绕过。
- Task4（AC4）：阈值校验规则（必须为正、有限、不过大）+ Options 持久化更新；配置写回 `config/default_config.json`，非法值拒绝且提示。
- Task5（AC1/AC5）：UI Telemetry 标签显示安全状态+原因，对 DATA_STALE 显示“数据过期”；新增数据过期状态提示测试。
- Task6（AC3/AC5）：Telemetry 接口与安全状态更新写入日志（flow/hardware/原因/source），记录 last_shutdown_event 含 source，便于追踪阻断事件。

### File List
- docs/sprint-artifacts/1-2-safe-start-airflow-interlock.md
- docs/sprint-artifacts/sprint-status.yaml
- app/services/safety_manager.py
- app/models/safety_state.py
- app/models/app_state.py
- app/controllers/main_controller.py
- app/views/main_window.py
- app/workers/hardware_worker.py
- tests/test_safety_manager.py
- tests/test_app.py
- app/main.py
