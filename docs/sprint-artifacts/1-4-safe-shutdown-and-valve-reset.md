# 故事 1.4: Safe Shutdown and Valve Reset
Status: Ready for Review
Epic: 1 - Safe Hardware Foundations
Story Key: 1-4-safe-shutdown-and-valve-reset
Story ID: 1.4

## Story
作为实验室技术员， 
我希望应用退出或按下紧急 Stop 时系统自动关闭所有阀门并停止加热， 
从而保证硬件随时恢复到安全初始状态。

## Acceptance Criteria
1. **退出/Stop 安全复位（AC1）**：Given 触发正常退出或点击 Stop/E-stop，When shutdown 流程运行，Then 所有气味阀、补偿阀、加热器和串口/DAQ 按安全顺序关闭/释放，记录包含 ts、source、airflow、reason 的日志，UI 提示“已安全关闭”。
2. **异常失败兜底（AC2）**：Given 关闭过程中发生驱动/通信异常，When 捕获到异常，Then 重试关闭失败的阀/加热，在超时后强制标记为已关闭并记录原因，未关闭的硬件需提示人工处理，且禁止继续发命令。
3. **重启安全状态检查（AC3）**：Given 应用重启后，When 读取 last_shutdown_event，Then 在 UI/footer 显示上次退出原因和时间；若上次标记 unsafe/未完成关闭，自动要求重新自检并保持控制禁用直到恢复。
4. **与安全/自检联动（AC4）**：Given 安全关闭完成，When 再次发出控制命令，Then 必须仍通过 hardware_ready + SafetyManager.guard_command 校验；关闭流程也会将安全状态重置为 SAFE/未连接且刷新 telemetry。
5. **数据/日志一致性（AC5）**：Given 任何关闭路径，When 完成后，Then Data Logger 写入 shutdown 事件，清理 session/log 文件句柄，确保 .raw/.log 正常 flush；若未完成写入需给出恢复步骤。

## Tasks / Subtasks
- [x] 设计统一 shutdown 流程（AC1/AC2）：梳理正常退出、Stop/E-stop、硬件异常回调入口，定义状态机与重试/超时。
- [x] 集成安全关闭命令（AC1/AC4）：复用 SafetyManager/HardwareWorker 关闭阀门和加热，解除 NI/RS232 资源。
- [x] 失败兜底与日志（AC2/AC5）：对关闭失败的子步骤重试并输出详细日志/telemetry，更新 last_shutdown_event。
- [x] 重启状态提示（AC3）：在 UI/footer 显示上次 shutdown 摘要；如 unsafe 需阻断控制并引导重新自检。
- [x] 测试与回归（AC1-AC5）：mock 硬件覆盖正常/异常/过期/重复关闭场景，验证日志与 UI 状态。

## Dev Notes
- 覆盖 PRD FR1.3（退出/应急自动关闭阀门）并依赖 FR1.1 自检、FR1.2 安全守卫、FR1.4 工具栏的 Stop 入口，重点是可靠的清理、重试和状态传播。
- 关闭逻辑必须在 Worker/Service 层完成，UI/Controller 仅触发/提示；确保 UI 不阻塞且可重入（重复 Stop/退出不报错）。
- 将关闭步骤（停止协议/手动控制 -> 关阀 -> 停加热 -> 释放 NI/RS232 -> flush 日志 -> 更新状态）序列化，提供可配置超时/重试次数。
- 记录 last_shutdown_event（ts, source, reason, airflow, valves_closed, heaters_off, result）并推送到 UI/footer 与后续启动流程。
- 退出前要先停止数据记录线程并 flush 文件句柄，避免 .raw/.log 损坏；异常时提示恢复步骤（例如检查 session 目录/删除锁文件）。

## Developer Context
- 平台：Windows 10/11，Python 3.10+，PySide6；MVC + Worker Thread；SafetyManager + HardwareWorker 处理硬件安全与 telemetry（5-10 Hz），UI 为被动视图。
- 硬件：NI-USB-6001/6501（nidaqmx）、RS232 质量流量控制器（pyserial）；Stop/退出需释放 DAQ/串口句柄。
- 状态模型：AppState 已含 hardware_ready、low_flow_threshold、last_shutdown_event、telemetry；应扩展字段标记 shutdown 结果/未完成原因。
- 现有组件：MainWindow 全局工具栏已有 Stop；MainController.ensure_safe_command/SafetyManager.guard_command 负责安全校验；Data Logger 记录事件；SafetyState 提供 SAFE/LOW_FLOW/DATA_STALE 判定。
- 性能/可用性：关闭流程在 worker/service 线程执行，UI 需保持响应；超时后也要确保阀门命令不再下发并更新 UI 状态。

## Technical Requirements
- **统一关闭入口**：MainController 暴露 `shutdown(source)`（source: app_exit/stop_button/hw_fault/tests）；先暂停协议/手动控制命令队列，再调用 SafetyManager/HardwareWorker 完成关闭。
- **步骤顺序**：1) 停止协议/预检/流量发送线程；2) 下发关闭所有阀门/停止加热指令；3) 确认/重试未关闭的通道（设定重试次数+间隔）；4) 停止并 flush Data Logger 与 session 文件；5) 断开 NI/RS232；6) 更新 AppState.last_shutdown_event + telemetry。
- **安全守卫**：关闭流程也需通过 SafetyManager.guard_command/hardware_ready 检查；关闭完成后，将 hardware_ready=false、安全状态=SAFE/未连接，阻断后续命令直至重新 Connect+自检。
- **异常兜底**：对 DAQ/串口异常进行捕获+重试，超过超时则记录未关闭列表并提示人工检查；禁止沉默失败。
- **UI/UX 联动**：Footer/状态栏显示“已安全关闭”或“关闭未完成：<原因>”；在 unsafe 记录存在时禁用危险按钮并提示重新自检；文本保持中文且与现有术语一致（SAFE/LOW FLOW）。
- **日志结构**：Data Logger 写入 `shutdown` 事件，字段包含 ts, source, airflow, threshold, valves_closed(bool/list), heaters_off, retries, result, error；确保 flush 成功或记录异常提示。
- **配置/持久化**：如需超时/重试/退出提示等参数，存于 `config/default_config.json` 并与 Options 页面字段对齐；默认值需合理且边界校验。
- **线程/资源**：确保 HardwareWorker stop/shutdown 在 Qt 线程安全上下文调用，避免 UI 线程长阻塞；对重复调用 shutdown 实现幂等。

## Architecture Compliance
- 逻辑仍在 MVC+Worker 架构内：MainController 负责编排，SafetyManager/HardwareWorker 执行硬件动作，UI 仅显示结果与提示。
- 目录/命名保持 `app/controllers|workers|services|models|views|services`，配置在 `config/default_config.json`，测试在 `tests/`；遵循 docs/architecture.md 的线程与日志规范。
- 通过 signals/slots 向 UI 推送 shutdown 状态/日志摘要；严禁在 UI 线程直接访问硬件句柄。

## Library & Framework Requirements
- Python 3.10+；当前 PySide6 6.7.2（最新 6.10.1，升级需验证 PyInstaller 打包/Qt 插件）；pyqtgraph 0.13.7（最新 0.14.0，升级需回归 100Hz 绘制）；nidaqmx 0.9.0（最新 1.3.0，升级需确认 NI 驱动兼容）；pyserial 3.5；PyInstaller 6.x（包含 Help PDF/依赖）。

## File Structure Requirements
- `app/controllers/main_controller.py`：集中 shutdown 协调逻辑、与 UI/toolbar Stop 事件绑定。
- `app/workers/hardware_worker.py`：提供关闭阀门/加热、释放硬件的安全接口与重试。
- `app/services/safety_manager.py`：复用/扩展 guard_command，记录 last_shutdown_event。
- `app/services/shutdown_service.py`（如需新增）：封装关闭步骤与重试策略，供 controller/worker 调用。
- `app/models/app_state.py`：扩展 last_shutdown_event 结构（结果/原因/时间戳），供 UI 显示。
- `app/views/main_window.py`：Stop/退出事件绑定、状态栏提示，禁用危险操作。
- `tests/`：新增 shutdown 单元/集成测试（mock 硬件/日志）。

## Testing Requirements
- **单元**：shutdown 状态机（正常/异常/重复调用）、重试与超时逻辑、last_shutdown_event 持久化；SafetyManager/guard_command 在关闭场景的返回值。
- **集成（mock 硬件）**：模拟 Stop/退出/硬件故障触发，验证阀门/加热关闭顺序、资源释放、日志输出、UI 状态更新；验证 unsafe 状态下阻断再次控制。
- **回归**：确保现有 Connect/Reset/安全互锁路径仍通过（AC 交叉影响）；日志格式与 Data Logger 兼容。
- **性能/可靠性**：关闭流程不阻塞 UI（<200ms 主线程阻塞），并在通信异常下提供稳定的兜底提示。

## Previous Story Intelligence
- Story 1.2（Safe Start）：已有 SafetyManager.guard_command、SafetyState（SAFE/LOW_FLOW/DATA_STALE）、last_shutdown_event 记录路径；关闭命令需沿用统一安全守卫与日志字段，避免绕过。
- Story 1.3（Global Safety Toolbar）：Stop 按钮已定义，要求非阻塞 UI，所有命令经 hardware_ready + SafetyManager.guard_command，状态栏显示操作结果；本故事需复用同一入口与提示风格。
- Story 1.1（Device Self-Check）：hardware_ready 状态与自检结果已存在；关闭后应将 hardware_ready 置为 false 并提示需重新自检。
- 现有日志/CI 流水线可复用；保持中文提示一致性。

## Git Intelligence Summary
- 仓库仅初始提交（a9283e8）；无新增实现可参考，提交时需附带本故事文件与 sprint 状态更新。

## Latest Tech Information
- PySide6 最新 6.10.1（当前 6.7.2），升级需验证打包与插件；pyqtgraph 最新 0.14.0（当前 0.13.7）；nidaqmx 最新 1.3.0（当前 0.9.0）；pyserial 3.5 为最新。

## Project Context Reference
- docs/epics.md（Epic 1 & Story 1.4 AC）
- docs/prd.md（FR1.3 自动安全复位）
- docs/architecture.md（MVC+Worker、日志/线程约束）
- docs/ux-design.md（全局工具栏/状态栏提示）
- docs/project-context.md（安全/性能目标）

## Story Completion Status
- 状态：Ready for Review
- 产物：docs/sprint-artifacts/1-4-safe-shutdown-and-valve-reset.md
- 完成说明：统一安全关闭/重试/持久化与 UI 提示已实现，测试通过等待代码审核。

## Dev Agent Record
### Context Reference
- 本故事文件；如有新测试/日志请在此补充路径。

### Agent Model Used
- Codex (GPT-5)

### Debug Log References
- 2025-12-09：python -m pytest（offscreen，覆盖 shutdown/persistence 用例）

### Completion Notes List
- 新增 ShutdownService，封装阀门/加热关闭、重试与持久化记录，MainController 统一 Stop/app_exit 流程并重置安全状态与 telemetry。
- UI 状态栏增加上次关闭摘要，启动时读取 last_shutdown_event 阻断危险操作并提示重新自检；默认配置补充重试参数与记录路径。
- 补充 shutdown 成功/失败重试/持久化/重启禁用的单测，运行 python -m pytest 全量通过。

### File List
- docs/sprint-artifacts/1-4-safe-shutdown-and-valve-reset.md
- docs/sprint-artifacts/sprint-status.yaml
- app/services/shutdown_service.py
- app/services/__init__.py
- app/controllers/main_controller.py
- app/main.py
- app/workers/hardware_worker.py
- app/views/main_window.py
- config/default_config.json
- tests/test_app.py

### Change Log
- 2025-12-09：实现统一安全关闭服务与 UI 关闭摘要，配置化重试/记录路径，新增回归测试并标记 Ready for Review。
