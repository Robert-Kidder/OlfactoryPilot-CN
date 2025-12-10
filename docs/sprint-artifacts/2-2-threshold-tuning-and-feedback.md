# 故事 2.2: Threshold Tuning and Feedback
Status: done
Epic: 2 - Calibration & Manual Control
Story Key: 2-2-threshold-tuning-and-feedback
Story ID: 2.2

## Story
作为研究人员，  
我希望拖拽或数值微调吸/呼气阈值并看到实时反馈，  
从而可以精确、可重复地设置 gating，并与下游控制保持一致。

## Acceptance Criteria
1. **阈值可视化与双向同步（AC1）**：Given 校准页显示黑底白线波形，红色虚线（呼气）和黄色点线（吸气）阈值均可拖拽；When 拖动任一阈值或在数值微调框中输入数值；Then 图形阈值位置与数值输入实时双向同步（UI 响应 <100ms，不掉帧），阈值以统一单位（与传感器读数一致，如伏特）和小数位显示（≥2 位），最小步进可配置（默认为 0.01），默认 inhale/exhale 阈值取自 config/default_config.json 并立即写入内存状态。  
2. **跨越反馈与状态标签（AC2）**：Given 硬件 worker 以 100Hz 推送信号；When 信号越过吸/呼气阈值；Then LED 指示灯亮灭与阈值状态一致，状态标签显示当前 gating（如“吸气越界/呼气越界/区间内”），更新延迟 <100ms，并遵守 UX 颜色规范（红=呼气，黄=吸气，灰=未触发）。  
3. **持久化与加载（AC3）**：Given 已经调整阈值；When 重启应用或切换到 Options 保存后返回；Then 阈值按“用户覆写路径 %USERPROFILE%/.olfactorypilot -> 默认 config/default_config.json” 的顺序恢复，非法/缺失值自动回退到默认并用中文提示告警（含回退原因），且不影响 UI 启动。  
4. **安全联动与封锁（AC4）**：Given SafetyState=LOW_FLOW 或 DATA_STALE；When UI 渲染阈值与 LED；Then 仍显示阈值但标记 gating 状态为“已封锁”，LED 灰显且不触发下游控制事件，解除后自动恢复，无需重启；与 footer 低流量提示一致（<500ms）。  
5. **日志与校验（AC5）**：Given 发生阈值变更或越界事件；When 记录到 Data Logger；Then 输出条目包含 ts、source（drag/spin/telemetry）、old/new 值、单位、结果（success/blocked）、safety_state，日志写入不影响 30 FPS 渲染；提供可验证的单元/集成测试用例。

## Tasks / Subtasks
- [x] 构建阈值控件与数据管线（AC1）：在校准视图中实现 PySide6+pyqtgraph 阈值线对象和数值微调框，信号/slot 双向绑定并限幅/格式化显示。  
- [x] 跨越检测与状态标签（AC2）：在控制器或专用 service 中消费 100Hz 信号，计算吸/呼气跨越与 gating 状态，驱动 LED 与中文标签刷新，避免阻塞 UI；封装为可复用的 gating service，供 2.3/2.4/2.5 共用，避免重复实现。  
- [x] 阈值持久化（AC3）：扩展 AppState/config 读写吸/呼气阈值字段；Options 页保存时写入 config/default_config.json（及用户覆写），启动/切换标签时回填并处理非法值。  
- [x] 安全联动与事件封装（AC4/AC5）：当 SafetyManager 判定 LOW_FLOW/DATA_STALE 时屏蔽下游控制事件但保持波形展示；记录阈值调整与越界事件到 Data Logger（含安全状态）；提供回放/调试钩子。  
- [x] 测试与回归（AC1-AC5）：编写单元测试覆盖阈值校验、持久化、状态标签逻辑；集成测试（mock 100Hz 信号+安全状态）验证 LED/标签响应与日志输出；确保 30 FPS 目标不回退。

## Dev Notes
- 覆盖 PRD FR3.2（阈值可视设置，红=呼气，黄=吸气），依赖 FR3.1 的 100Hz 波形与 FR1.2 的安全联动；需与后续 2.3 阀矩阵/2.4 流量控制共用同一 gating 状态与阈值来源。  
- 阈值状态应存入 AppState 并与 SafetyManager 低流量判定共存：低流量时可以调整阈值但阻断阈值触发下游动作；恢复 SAFE 后自动恢复事件流。  
- UI 文案保持中文，与 footer/toolbar 术语一致（SAFE/LOW FLOW/数据过期）；颜色遵循 UX 规范（#DC3545 红，#28A745 绿，阈值线红虚/黄点）。  
- 需避免在 UI 线程进行重计算：跨越检测可在 controller/service 层使用轻量窗口缓存，pyqtgraph 使用 setData/移动线条对象而非重建，保持 >=30 FPS。  
- 阈值/状态变更写入 Data Logger（与 2.1 的 breath_viz 事件兼容），方便后续 Protocol/Breath-gated 逻辑复用。

## Developer Context
- 平台：Windows 10/11，Python 3.10+，PySide6；MVC + Worker Thread；硬件 worker 推送 100Hz 信号与 5-10 Hz SafetyState telemetry。  
- 硬件：NI-USB-6001/6501（nidaqmx），RS232 质量流量控制器（pyserial）；Air Flow 阈值与 Options 配置共用。  
- 状态模型：AppState 已含 low_flow_threshold、telemetry、config_path；本故事需新增 inhale/exhale 阈值、gating 状态缓存，并保持与 SafetyState 共存不冲突。  
- 现有组件：SafetyManager.guard_command、MainController.ensure_safe_command 与 footer 状态展示已存在；config/default_config.json 提供默认阈值；Data Logger 事件格式在 2.1 中定义（breath_viz）。  
- 性能：UI 主线程需维持 >=30 FPS；信号/slot 处理和日志写入应避免长阻塞，可用批量/节流策略（如每帧最多一次越界计算、日志异步队列）。

## Technical Requirements
- **阈值模型与校验**：在 AppState 中新增 `inhale_threshold`、`exhale_threshold`，提供校验/限幅（>0、非 NaN/Inf、合理上限、最小步进默认为 0.01，单位与传感器一致）；Options/校准页共享同一校验函数，非法值回退并提示。  
- **双向绑定**：校准视图的阈值线（pyqtgraph InfiniteLine）与 QDoubleSpinBox 通过 signal/slot 双向更新；值变更事件标记来源（drag/spin/load）以便日志。  
- **跨越计算**：针对 100Hz buffer 计算当前样本是否越界，输出 gating 状态（INHALE_ABOVE/EXHALE_ABOVE/NEUTRAL）与 LED 布尔值，保持无锁/轻量；低流量或数据过期时直接输出 BLOCKED 状态。  
- **持久化**：启动时加载 config/default_config.json（或用户覆写）写入 AppState；Options 保存时写回（UTF-8，ensure_ascii=false），保持 manual_path 等既有字段；缺失字段时补默认值，并按“用户覆写 -> 默认”顺序回退；无效值需记录中文警告。  
- **共享服务**：在 services 层提供阈值/gating 计算与状态访问接口，供 2.3 阀矩阵、2.4 流量控制、2.5 变体 UI 直接读取，避免重复实现逻辑与状态分叉。  
- **日志格式**：阈值调整事件 `threshold_update`：ts, source, inhale, exhale, safety_state, result；越界事件 `threshold_cross`：ts, sample_value, gate_state, inhale, exhale, safety_state。格式与 2.1 的 Data Logger 风格一致，避免破坏现有日志消费者。  
- **日志/UI 节流**：对越界事件与 UI 更新做去抖/节流（例如每帧最多一次 UI 刷新，日志可窗口合并），确保维持 30 FPS 且日志不过载。  
- **UX 联动**：低流量/数据过期时禁用“应用阈值/下游控制”按钮但允许调整、查看；状态标签显示最近 telemetry 时间戳，与 footer 更新节奏一致（<500ms）。  
- **防回归**：保持 2.1 已实现的 100Hz 渲染与 FPS 监控，不得降低 p95 FPS；新增逻辑必须可在无硬件（CI/offscreen）模式下运行。  

## Architecture Compliance
- 继续使用 MVC+Worker：硬件信号在 worker 线程产生，经 signals/slots 传递到 controller/service 处理，再驱动 view；避免在 UI 线程访问硬件。  
- 代码路径遵循现有结构：视图置于 `app/views`，控制/计算逻辑置于 `app/controllers` 或 `app/services`，状态模型在 `app/models`，配置在 `config/`，测试在 `tests/`。  
- 按 docs/architecture.md 的 telemetry 流（5-10 Hz 状态 + 100Hz 信号）和安全封锁要求实现，保持与 SafetyManager 低流量判定兼容。

## Library & Framework Requirements
- Python 3.10+；PySide6 6.7.2（上游最新 6.10.1，升级需验证打包/兼容性）；pyqtgraph 0.13.7（上游 0.14.0，升级需回归 100Hz 性能）；nidaqmx 0.9.0（上游 1.3.0，升级需验证 NI 驱动）；pyserial 3.5；PyInstaller 6.x。

## File Structure Requirements
- `app/views/calibration_view.py`（或同名文件）：实现阈值线、LED、状态标签与数值输入控件，暴露更新接口供 controller 调用。  
- `app/controllers/calibration_controller.py`（或扩展 MainController）：消费 worker 信号、计算 gating 状态、驱动视图更新与日志写入。  
- `app/models/app_state.py`：新增吸/呼气阈值与 gating 状态字段、加载/保存逻辑；保持与 existing telemetry/self_check 兼容。  
- `app/services/safety_manager.py`：复用低流量判定，提供封锁状态给阈值逻辑；如需新增阈值校验可共用此处。  
- `config/default_config.json`：新增 inhale/exhale 阈值默认值，保持 UTF-8 与现有字段。  
- `tests/`：新增单元/集成测试（mock 信号 + UI/控制器）验证阈值同步、持久化、封锁与日志输出。

## Testing Requirements
- **单元**：阈值校验/限幅/步进、AppState 读写、gating 状态机（含 BLOCKED）、日志 payload 构造、共享 service 对外接口。  
- **集成（mock）**：模拟 100Hz 信号和 SafetyState 切换，验证 LED/标签响应时间、阈值同步、低流量封锁与日志输出；在 offscreen/无硬件模式运行；验证越界/日志节流有效。  
- **回归/性能**：确保添加逻辑后 10s 窗口 p95 FPS 仍 ≥30（与 2.1 要求一致）；验证与 SafetyManager.guard_command、toolbar/footer 提示不冲突。  
- **持久化**：写入/读取 config/default_config.json（含用户覆写路径）不破坏其他字段，非法值按“用户覆写 -> 默认”回退并给出中文提示（含回退原因），启动/运行不中断。  

## Previous Story Intelligence
- Story 2.1（100Hz 呼吸波形）：已定义 pyqtgraph 渲染、FPS 监控、breath_viz 日志格式，阈值线/LED/状态提示初版存在；需复用 ring buffer 与性能节流策略，避免二次创建绘图对象。  
- Story 1.2（Safe Start）与 SafetyManager：LOW_FLOW/DATA_STALE 判定与 guard_command 已有，阈值触发不得绕过安全封锁。  
- Story 1.3（Global Safety Toolbar）：footer/toolbar 状态文案与低流量提示需一致，阈值标签语言保持统一；状态更新节奏需与 telemetry 一致。  
- Story 1.4（Safe Shutdown）：last_shutdown_event 结构可用于提示数据过期/断连场景，阈值逻辑需在断连时优雅降级。

## Git Intelligence Summary
- 仓库目前仅初始提交（a9283e8），暂无可参考实现；提交时需附带本故事文件与 sprint 状态更新。

## Latest Tech Information
- PySide6 最新 6.10.1（当前 6.7.2，升级需验证打包与 Qt 插件）；pyqtgraph 最新 0.14.0（当前 0.13.7）；nidaqmx 最新 1.3.0（当前 0.9.0）；pyserial 3.5 为最新。

## Project Context Reference
- docs/epics.md（Epic 2 & Story 2.2 AC）  
- docs/prd.md（FR3.2 阈值可视化）  
- docs/ux-design.md（阈值颜色、LED/状态标签、100Hz/30FPS）  
- docs/architecture.md（MVC+Worker、telemetry 节奏、安全封锁）  
- docs/project-context.md（性能/安全/本地化目标）  
- docs/sprint-artifacts/2-1-real-time-breath-visualization.md（上一故事实现与日志格式）

## Story Completion Status
- 状态：ready-for-dev
- 产物：docs/sprint-artifacts/2-2-threshold-tuning-and-feedback.md
- 完成说明：Ultimate context engine analysis completed - comprehensive developer guide created.

## Dev Agent Record
### Context Reference
- `tests/test_gating_service.py` created for unit testing the new GatingService.
### Agent Model Used
- Gemini 2.0 Flash

### Debug Log References
- Verified with temporary test suite `tests/test_story_2_2.py` (deleted after success).
- All tests passed: GatingService logic, MainController integration, UI updates, persistence, and safety interlock.
### Completion Notes List
- Implemented `GatingService` in `app/services/gating_service.py` to handle 100Hz gating logic (AC2).
- Integrated `GatingService` into `MainController` to process breath samples and log transitions (AC2, AC5).
- Updated `CalibrationView` to display gating state label and sync LEDs with safety state (AC1, AC4).
- Added `gating_state` to `Telemetry` model and updated `MainController` to propagate it.
- Ensured threshold updates are persisted to config via `MainController` (AC3).
- Added `tests/test_gating_service.py` for permanent regression testing.
- Verified that safety state (LOW_FLOW/DATA_STALE) correctly blocks gating output (AC4).
- **Code Review Fix**: Added `tests/test_integration_gating.py` to ensure `MainController` wiring is permanently tested.
- **Code Review Fix**: Added `config/default_config.json` to File List.

## File List
- app/services/gating_service.py
- app/services/__init__.py
- app/models/app_state.py
- app/controllers/main_controller.py
- app/views/calibration_view.py
- app/views/main_window.py
- config/default_config.json
- tests/test_gating_service.py
- tests/test_integration_gating.py


