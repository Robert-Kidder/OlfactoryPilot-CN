# 故事 2.1: Real-Time Breath Visualization
Status: done
Epic: 2 - Calibration & Manual Control
Story Key: 2-1-real-time-breath-visualization
Story ID: 2.1

## Story
作为研究人员， 
我希望以 100Hz 刷新率查看呼吸波形并可一键自动缩放， 
从而在校准阶段获得稳定、直观的信号与阈值反馈。

## Acceptance Criteria
1. **100Hz 流 + 30FPS 渲染（AC1）**：Given 硬件 worker 推送 100Hz 信号，When 校准页显示波形，Then UI 绘制帧率在 10s 滑窗内 avg+p95 >=30 FPS（黑底白线），并在 Data Logger 中记录 fps_avg/fps_p95。
2. **性能告警与日志（AC2）**：Given FPS 低于 30 持续 >2s，When 检测到，Then UI 显示中文警告并写入 session log（含 ts、fps_avg、fps_p95、window_s），告警可恢复（清除条件：FPS 恢复 >=30 持续 5s）。
3. **阈值可视与同步（AC3）**：Given 吸/呼阈值可见，When 用户拖动红虚线/黄点线阈值或通过数值框调整，Then 图形与数值字段双向同步，LED 指示实时反映 crossing，状态与数值会在会话间持久化（样式须符合 UX 规范：红虚线呼气、黄点线吸气）。
4. **安全状态联动与告警时延（AC4）**：Given SafetyState=LOW_FLOW/DATA_STALE，When 信号/状态推送到 UI，Then 校准页显示“LOW FLOW/数据过期”提示并可选灰显阈值交互，仍保持波形渲染但阻止触发下游控制；SAFE 时恢复正常交互；LOW FLOW 告警展示需在 <500ms 内响应 telemetry（与 footer 一致），并显示最近更新时间戳。
5. **数据流健壮性（AC5）**：Given 数据包缺失/异常，When 检测到空数据或异常值，Then 不导致 UI 卡死，使用上次有效样本填充并记录 warning；在 1s 内无数据则标记 DATA_STALE 并提示。

## Tasks / Subtasks
- [x] 设计校准视图与绘制循环（AC1）：使用 pyqtgraph 黑底白线绘制 100Hz 数据，测量 10s 滑窗 FPS（avg/p95）并推送到 logger。
- [x] 性能监控与告警（AC2）：实现 FPS 监控与阈值触发/恢复逻辑，UI 告警提示与日志输出。
- [x] 阈值/LED 同步（AC3）：实现红/黄阈值线拖拽与数值框双向绑定，LED crossing 事件更新（信号来自 worker/safety）。
- [x] 安全/数据过期联动（AC4/AC5）：处理 SafetyState/数据缺失，显示提示、灰显交互并保持波形渲染；记录 DATA_STALE 事件。
- [x] 测试与回归（AC1-AC5）：mock 信号源验证 FPS 计算、告警、阈值同步、数据缺失处理；确保 UI 不阻塞且日志字段完整。

## Dev Notes
- 覆盖 PRD FR3.1（实时 100Hz 呼吸波形 + Auto-scale），依赖 FR1.2 的安全状态（LOW FLOW）、FR1.1 自检的硬件就绪，以及 FR1.4 工具栏的状态显示；与 Epic 2 其余故事（阈值调节/阀矩阵/流量控制）共享同一信号/安全管线。
- 采用 pyqtgraph 曲线（黑底白线），Auto-scale 按钮触发 Y 轴范围自适应；需避免每帧重建对象，使用滚动缓冲/预分配数组减少 GC。
- FPS 计算：基于绘制完成时间戳维护 10s 滑窗（队列），输出 avg/p95；出现长阻塞时确保 UI 不崩溃，告警可自恢复。
- 阈值/LED：阈值数据应持久化到 config/default_config.json 或 AppState（与 Options 页面复用）；LED 颜色/状态同步到跨模块 SafetyState（SAFE/LOW_FLOW/DATA_STALE），并遵循 UX 线型规范（红虚线/黄点线）。
- 日志：Data Logger 记录 `breath_viz` 事件（ts, fps_avg, fps_p95, window_s, warning_flag, reason）；数据缺失/异常值要记录 source 与恢复时间。

## Developer Context
- 平台：Windows 10/11，Python 3.11，PySide6；MVC + Worker Thread；硬件 worker 提供 100Hz 信号与 SafetyState（5-10 Hz）telemetry，UI 被动订阅。
- 硬件：NI-USB-6001/6501（nidaqmx），信号源为呼吸传感器；安全阈值来自 config/Options；LOW FLOW 会阻断下游控制但不应阻塞波形渲染。
- 现有组件：MainWindow tab 布局与 footer 状态显示；SafetyManager.guard_command + hardware_ready 已存在；Data Logger 用于事件记录；AppState/SafetyState 已定义 safe/low_flow/data_stale。
- 性能：UI 主线程需保持 >=30 FPS（绘制），worker 在独立线程；避免过度对象创建与主线程阻塞；必要时使用 Qt 定时器/pyqtgraph setData 重用。

## Technical Requirements
- **数据管线**：HardwareWorker 以信号/队列推送 100Hz 数据（批量或滑窗），UI 订阅后使用固定长度 ring buffer 更新曲线；处理缺失时填充上次有效值并标记 DATA_STALE。
- **FPS 监控**：绘制结束时记录时间戳；维护 10s 队列计算 avg/p95；当 p95<30 持续 >2s 触发 warning，恢复条件 p95>=30 持续 5s；将指标发送到 logger/状态栏。
- **Auto-scale**：提供按钮/快捷操作触发 pyqtgraph enableAutoRange 或自定义 y-range 计算，需节流避免每帧重设。
- **阈值同步**：红/黄阈值线对象支持拖拽回调；数值输入框变更时更新线位置；变化事件写入 AppState/配置并推送 LED 状态（跨越/未跨越）。
- **安全联动**：UI 接收 SafetyState（SAFE/LOW_FLOW/DATA_STALE）并在 footer/校准区显示，显示最近更新时间戳；LOW_FLOW/DATA_STALE 时禁用“应用阈值/下游控制”按钮但持续渲染，LOW FLOW 告警响应时间 <500ms。
- **日志格式**：Data Logger 事件 `breath_viz`/`breath_viz_warning` 包含 ts, fps_avg, fps_p95, window_s, sample_count, warning_flag, reason；数据缺失事件包含 last_valid_age。
- **稳健性**：绘制/更新为幂等可重入；数据通道断开后自动重连提示；异常捕获需中文提示且不崩溃 UI。

## Architecture Compliance
- 遵循 docs/architecture.md 的 MVC+Worker：数据在 worker 线程读取，UI 通过信号/slots 渲染；禁止 UI 线程直接访问硬件。
- 源码放置在 `app/views`（如新建 calibration 视图组件）与 `app/controllers`（信号绑定），保持现有目录命名；日志与安全逻辑继续复用 services/workers。
- 遵循状态推送节奏（telemetry 5-10 Hz，数据 100Hz），保持线程安全与最小阻塞；配置读写走 config/default_config.json。

## Library & Framework Requirements
- Python 3.11；PySide6 6.7.2（最新 6.10.1，升级需验证打包/Qt 插件）；pyqtgraph 0.13.7（最新 0.14.0，升级需回归 100Hz 性能与 Qt 兼容）；nidaqmx 0.9.0（最新 1.3.0，升级需验证驱动）；pyserial 3.5；PyInstaller 6.x。

## File Structure Requirements
- `app/views/...`：校准视图组件/pyqtgraph 曲线与阈值线、LED 指示器；MainWindow 中挂载 Tab。
- `app/controllers/main_controller.py`（或相关控制器）：订阅 worker 信号、驱动视图更新、处理 auto-scale/FPS 指标与警告。
- `app/workers/hardware_worker.py`：提供 100Hz 数据推送与安全状态；数据缺失/异常标记 DATA_STALE。
- `app/models/app_state.py`：存储阈值、last_shutdown_event、FPS 指标等必要状态以便 UI/日志访问。
- `config/default_config.json`：持久化阈值/显示设置（颜色、窗口长度、FPS 警戒值如需）。
- `tests/`：新增 breath visualization 相关单测/集成测试（mock 信号源、FPS 监控、阈值同步、数据缺失）。

## Testing Requirements
- **单元**：FPS 计算（avg/p95 滑窗）、告警触发/恢复逻辑、阈值同步（拖拽/数值输入）、数据缺失填充与 DATA_STALE 标记。
- **集成（mock）**：模拟 100Hz 信号流验证 30 FPS 渲染路径（可通过计数/定时器测量），LOW_FLOW/DATA_STALE 提示与按钮禁用，告警日志输出。
- **回归**：确保与 SafetyManager/toolbar 状态显示一致，不影响 Stop/Reset 流程；确保日志格式与 Data Logger 兼容。
- **性能/健壮**：在低端 CPU 场景模拟负载，确认 UI 无明显卡顿、无内存泄漏，Auto-scale 不阻塞。

## Previous Story Intelligence
- Story 1.2（Safe Start）提供 SafetyState 与 guard_command，需复用 LOW_FLOW/DATA_STALE 提示与安全阻断路径。
- Story 1.3（Global Safety Toolbar）已提供状态栏/按钮与 telemetry；校准页提示应与 footer 文案一致，Stop/Reset/Connect 入口保持联动。
- Story 1.4（Safe Shutdown）要求退出时安全关闭并记录 last_shutdown_event；校准页重启时可显示最近一次关机原因以便诊断数据中断。
- Story 1.1（Self-Check）定义 hardware_ready；校准页仅在自检通过后显示“SAFE/已连接”状态。

## Git Intelligence Summary
- 仓库目前仅初始提交（a9283e8），无可参考实现；本故事完成后需创建新提交并更新 sprint 状态。

## Latest Tech Information
- PySide6 最新 6.10.1（当前 6.7.2）；pyqtgraph 最新 0.14.0（当前 0.13.7）；nidaqmx 最新 1.3.0（当前 0.9.0）；pyserial 3.5 为最新。

## Project Context Reference
- docs/epics.md（Epic 2 & Story 2.1 AC）
- docs/prd.md（FR3.1 呼吸波形）
- docs/ux-design.md（黑底白线、阈值颜色、LED 反馈）
- docs/architecture.md（MVC+Worker、日志/线程要求）
- docs/project-context.md（性能/安全目标）

## Story Completion Status
- 状态：review
- 产物：docs/sprint-artifacts/2-1-real-time-breath-visualization.md
- 完成说明：实现校准视图（100Hz 占位流、30FPS 追踪）、FPS 告警/恢复、阈值/LED 同步、安全灰显与数据过期提示；新增单测并完成一轮 pytest。

## Dev Agent Record
### Context Reference
- 本故事文件；如有新测试/日志请在此补充路径。

### Agent Model Used
- Codex (GPT-5)

### Debug Log References
- pytest（全部通过）：python -m pytest

### Completion Notes List
- 选择 Story 2.1（FR3.1）并补充 100Hz 波形、30FPS 指标、性能告警、阈值同步、数据缺失处理、日志要求。
- 复用 SafetyState/toolbar/自检上下文，定义 UI/worker/日志文件位置与测试覆盖点，保持中文提示与既有目录一致。
- 新增 CalibrationView（pyqtgraph 黑底白线 + 阈值线/LED），10s 滑窗 FPS 追踪与低于 30FPS>2s 告警（自动恢复），数据过期提示。
- HardwareWorker 输出占位 100Hz 波形；新增 BreathSampleBuffer/FrameRateTracker 服务与阈值持久化；breath_viz logger 记录 fps_avg/fps_p95/window_s/warning_flag/reason。

### File List
- docs/sprint-artifacts/2-1-real-time-breath-visualization.md
- docs/sprint-artifacts/sprint-status.yaml
- app/services/breath_metrics.py
- app/services/__init__.py
- app/models/app_state.py
- app/controllers/main_controller.py
- app/workers/hardware_worker.py
- app/views/calibration_view.py
- app/views/main_window.py
- app/views/__init__.py
- config/default_config.json
- tests/test_breath_metrics.py

