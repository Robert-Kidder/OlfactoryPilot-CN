# 故事 1.3: Global Safety Toolbar
Status: real-hardware-review
Epic: 1 - Safe Hardware Foundations
Story Key: 1-3-global-safety-toolbar
Story ID: 1.3

## Story
作为实验室技术员，  
我希望在应用内始终可见 Connect、Reset（硬件恢复）、Stop（软断开）和 Help 控件，  
从而随时初始化、恢复、停止或查看中文手册，保证硬件安全与可用性。

## Acceptance Criteria
1. **Connect 初始化与反馈（AC1）**：应用启动后，点击 Connect 会触发设备自检（NI-USB-6001/6501、RS232 波特率）与连接初始化；成功时状态栏/页脚显示“已连接/安全”摘要并刷新 telemetry；失败时阻断后续硬件命令，给出中文原因与建议（引用自自检/安全守卫），且可重新点击重试而不冻结 UI。
2. **Reset 硬件恢复（AC2）**：硬件故障或连接不一致时，点击 Reset 会重新握手 NI/RS232、关闭全部阀门（调用安全关闭流程）、重置安全状态，并将最近一次恢复事件写入日志；操作完成后更新状态栏与自检摘要，失败则保持阻断并提示下一步操作。
3. **Stop 安全停止（AC3）**：任意时刻点击 Stop，会安全停止当前协议/手动控制命令、关闭阀门和释放硬件句柄，主线程保持可响应；停止原因和安全状态写入日志，UI 反馈“已停止/已关闭阀门”，再次启动需重新通过安全/自检守卫。
4. **Help 本地手册（AC4）**：点击 Help 打开本地中文 PDF/手册（随安装包或 docs 内置路径，可配置）；若文件缺失或无法打开，弹出中文提示并提供恢复指引，不依赖网络。

## Tasks / Subtasks
- [x] 设计全局工具栏 UI（AC1-AC4）：常驻页眉/页脚，按钮状态与安全/自检联动，禁用条件清晰。
- [x] Connect 流程整合（AC1）：复用自检服务 + 安全守卫，异步执行并显示进度/结果，失败可重试。
- [x] Reset 恢复流（AC2）：统一调用关闭阀门/重握手/重新自检的服务接口，写入恢复日志，防重入。
- [x] Stop 安全停止（AC3）：对协议引擎/预检/流量发送入口添加软断开钩子，确保阀门关闭、线程退出、UI 不阻塞。
- [x] Help 打开逻辑（AC4）：实现本地 PDF 打开与缺失兜底提示，路径可配置并能在打包产物中使用。
- [x] 测试与回归（AC1-AC4）：补充单元/集成测试覆盖按钮状态机、命令护栏、日志输出与异常兜底。

## Dev Notes（story_requirements）
- 覆盖 PRD FR1.4（全局工具栏），并复用 FR1.1 自检、FR1.2 安全守卫、FR1.3 安全关闭的能力，避免重复造轮子。
- 按 UX 规范：工具栏全局可见（任何 tab），按钮标签中文；状态栏/页脚持续显示连接、气流、安全状态（5-10 Hz telemetry，LOW FLOW <500ms 闪烁）。
- Connect/Reset/Stop 必须经过 `hardware_ready` + `SafetyManager.guard_command` 双重护栏，禁用或提示时给出中文原因与建议；不可绕过 Safe Start 逻辑。
- Help 需离线可用（随包 PDF），并在缺失时提示“未找到本地手册，请检查安装包或 docs 目录”且不崩溃。
- 所有操作需非阻塞 UI，使用 signals/slots 调度 Worker/Service；任何异常都要记录（时间戳、动作、来源、原因）。

## Developer Context（developer_context_section）
- 平台：Windows 10/11，Python 3.11，PySide6；MVC + Worker 线程；硬件线程负责自检、telemetry、安全守卫，UI 仅被动渲染。
- 硬件：NI-USB-6001/6501（nidaqmx）、RS232 质量流量控制器（pyserial）；气流阈值为安全判定输入。
- 状态模型：`AppState` 内含 telemetry（airflow/safety_state/timestamp）、`hardware_ready`、`low_flow_threshold`、`last_shutdown_event`；已有 `SafetyState`、`SafetyManager` 负责安全判定和命令护栏。
- 现有 UI：`MainWindow` 具备 tab + 状态栏、telemetry 展示与“重新自检”按钮；需要扩展为全局工具栏（新增按钮/布局），并与状态栏联动。
- 性能/可用性：自检/恢复流程应在 Worker/Service 内执行，主线程响应 >30 FPS；按钮状态实时反映连接/安全；Stop/Reset 需立即反馈。

## Technical Requirements（technical_requirements）
- **命令护栏**：所有 Connect/Reset/Stop 入口必须调用 `MainController.ensure_hardware_ready`（自检通过）和 `ensure_safe_command`（安全态 SAFE & 硬件连通）；失败返回原因并更新状态栏。
- **Connect 流程**：启动硬件线程（如未运行）、调用 `HardwareCheckService.run_checks()`，初始化 NI/RS232 连接；成功后刷新 telemetry，状态栏显示“已连接 SAFE”，失败则阻断并提示可重试。
- **Reset 流程**：关闭阀门/停止加热（复用安全关闭接口或 SafetyManager 事件），重新初始化 NI/RS232，强制刷新自检结果与 `hardware_ready`; 记录恢复事件到日志（含 ts、原因、结果）。
- **Stop 流程**：触发协议/预检/流量发送等入口的软断开（后续故事可调用同一接口），关闭阀门，调用 worker.stop() 释放资源；更新 `last_shutdown_event` 与 UI 提示。
- **Help 打开**：读取可配置的手册路径（默认指向本地 PDF）；使用系统默认应用打开；异常（缺失/权限）要捕获并弹窗提示。
- **UI 反馈**：按钮文案中文清晰；正常运行时 Connect/Reset/Stop/Help/预检启动/阀门按钮不使用 tooltip 或弹窗提示，禁用/失败原因通过状态栏、页脚或页面标签呈现；状态栏/页脚显示最近操作结果与时间戳。
- **日志**：统一使用现有 logging/Data Logger；记录按钮事件（动作、source、airflow/safety_state、结果、耗时/错误）。

## Architecture Compliance（architecture_compliance）
- 继续遵循 MVC + Worker（docs/architecture.md）：硬件/安全逻辑在 Worker/Service；UI 不直接操作硬件。
- 工具栏 UI 放在 `MainWindow`（或子组件）中，控制器负责命令分发与安全护栏；Service/Worker 负责具体硬件动作。
- 通过 signals/slots 将操作结果与 telemetry 推送到 UI；避免在 UI 线程阻塞或长时间 IO。
- 保持现有目录与命名：`app/controllers|views|workers|services|models`，`config/default_config.json`，`tests/`。

## Library & Framework Requirements（library_framework_requirements）
- Python 3.11；PySide6 当前 6.7.2，最新 6.10.1（升级需验证 PyInstaller 打包/Qt 插件）。
- pyqtgraph 0.13.7（最新 0.14.0，升级需验证 100Hz 绘图性能）。
- nidaqmx 0.9.0（最新 1.3.0，升级需确认 NI-DAQmx 驱动兼容）。
- pyserial 3.5（最新）。
- PyInstaller 6.x：若升级 PySide6，需检查 hidden-import/字体打包，确保 Help PDF 一并打包。

## File Structure Requirements（file_structure_requirements）
- 扩展 `app/views/main_window.py` 增加全局工具栏按钮（Connect/Reset/Stop/Help）与状态栏绑定，保持现有 tab 布局。
- 在 `app/controllers/main_controller.py` 中集中处理按钮动作、调用 `SafetyManager`/`HardwareWorker`/硬件服务；不要让 UI 直接触碰硬件。
- 如需辅助服务（例如阀门关闭/硬件重连），放入 `app/services/`（可复用 `hardware_check_service.py`，避免重复逻辑）。
- 配置项（手册路径、按钮文案、阈值）存放于 `config/default_config.json`，与 Options 页面保持一致（后续故事可复用）。
- 测试放在 `tests/`，使用 mock 替代真实硬件。

## Testing Requirements（testing_requirements）
- 单元测试：`MainController.ensure_safe_command/ensure_hardware_ready` 在 Connect/Reset/Stop 中的行为；按钮禁用条件；Help 缺失兜底。
- 集成测试（mock Worker/Service）：Connect 触发自检并更新 telemetry；Reset 关闭阀门->重握手->刷新状态；Stop 触发软断开并调用 worker.stop。
- 日志校验：按钮事件写入日志含动作、结果、原因；异常不会导致崩溃。
- UI/状态测试：telemetry 5-10 Hz 更新时按钮/状态栏文案正确；LOW FLOW/未连接时禁用危险操作。

## Previous Story Intelligence（previous_story_intelligence）
- Story 1.1（自检）：已有 `hardware_ready` 状态、自检结果信号与中文错误提示；Connect 需复用自检服务与结果结构，避免重复自检逻辑。
- Story 1.2（Safe Start）：`SafetyManager` + `MainController.ensure_safe_command` 已实现低流/过期/硬件上报的安全阻断和日志；Reset/Stop/Connect 需沿用同一安全护栏与日志字段，避免绕过或分叉。

## Git Intelligence Summary（git_intelligence_summary）
- 未分析新增提交记录；保持现有目录/命名，后续提交请包含故事文件与 sprint 状态更新。

## Latest Tech Information（latest_tech_information）
- PySide6 最新 6.10.1（当前 6.7.2），升级需验证打包与 Qt 插件。
- pyqtgraph 最新 0.14.0（当前 0.13.7），升级需验证 100Hz 绘图性能。
- nidaqmx 最新 1.3.0（当前 0.9.0），升级需确认 NI-DAQmx 驱动匹配。
- pyserial 3.5 已为最新。

## Project Context Reference（project_context_reference）
- docs/project-context.md（范围/安全目标/性能约束）
- docs/epics.md（Epic 1 & Story 1.3 AC）
- docs/prd.md（FR1.4 全局工具栏）
- docs/architecture.md（MVC + Worker、安全与 telemetry 约束）
- docs/ux-design.md（全局工具栏/状态栏、中文 UI 规范）

## Story Completion Status（story_completion_status）
- 状态：ready-for-review
- 产物：docs/sprint-artifacts/1-3-global-safety-toolbar.md
- 下一步：提交 code-review；如需复核故事质量，可运行 *validate-create-story。
- 完成说明：Ultimate context engine analysis completed - comprehensive developer guide created.

## Dev Agent Record
### Context Reference
- 本故事文件；如有新增调试/日志，请在此补充路径。

### Agent Model Used
- Codex (GPT-5)

### Debug Log References
- 2025-12-09：python -m pytest（offscreen），覆盖 toolbar 状态护栏与自检失败反馈。
- 2026-06-30：现场反馈 Stop 后再 Connect 时 UI 被长自检文本撑开且 Connect 清零阶段界面冻结；修复自检摘要换行/分行显示、Connect 去重自检，并将自检后 A/B/C 清零移到后台线程。`.venv-win\python.exe -m pytest -q` 通过，118 passed。
- 2026-06-30：现场反馈 Reset 后再 Connect 时持续弹无响应/弹窗无法关闭；修复 Reset 后仍向已停止 worker 遗留 pending self-check 的状态机问题。Reset 语义恢复为“一键硬件恢复”：先安全关阀/释放资源，再自动重新初始化硬件并自检；worker stop/mark_disconnected 会清除 pending self-check，避免重复自检/清零。`.venv-win\python.exe -m pytest -q` 通过，119 passed。
- 2026-06-30：复核底部“重新检查”按钮，确认其功能已被全局工具栏 Connect（自检/重检）与 Reset（一键恢复）覆盖；移除该重复入口，降低现场误操作与 UI 噪声。`.venv-win\python.exe -m pytest -q` 通过，119 passed。
- 2026-06-30：现场要求取消正常运行按钮弹窗/悬浮提示；移除全局工具栏 Connect/Reset/Stop/Help 与预检启动/阀门按钮 tooltip，保留启动失败致命错误弹窗作为异常兜底；按钮状态与失败原因改由现有状态栏/页面文本承载。`.venv-win\python.exe -m pytest -q` 通过，119 passed。
- 2026-06-30：现场反馈成功连接后点击 Stop 仍出现安全状态提示；修复 Stop/Reset/app_exit 成功关闭后的迟到 disconnected telemetry 被误判为异常断连的问题，保留真正意外断连的 DATA_STALE 安全提示。`.venv-win\python.exe -m pytest -q` 通过，120 passed。
- 2026-06-30：现场反馈 Stop 后再 Connect 仍显示“安全状态：SAFE”类提示；调整预检安全状态提示规则，SAFE 状态无论是否带有关闭/恢复原因都不显示提示框，仅保留非 SAFE/禁用状态提示。同时修复 Reset 后气道按钮未复位的问题，Reset 会清空预选、打开状态、绿色勾选和主阀指示。`.venv-win\python.exe -m pytest -q` 通过，122 passed。

### Completion Notes List
- 选择 FR1.4 全局工具栏故事并整合自检/安全守卫上下文，定义 AC1-AC4。
- 补充 Connect/Reset/Stop/Help 的技术/日志/UX 细节，强调复用 SafetyManager 与自检服务，避免绕过护栏。
- 明确 UI 常驻、异步执行与测试覆盖要求，提供文件/配置/打包参考。
- 完成全局工具栏 UI：新增禁用原因提示与连接/自检状态文案，并为按钮状态护栏补充单元测试。
- Connect 失败时提示首个自检失败原因与建议，重试按钮保持非阻塞；补充自检失败用例测试。
- Stop 后状态会显式回到 disconnected/not-ready；重新 Connect 时不再重复触发两次自检，长错误文本不会撑宽主窗口，自检后清零不会阻塞 UI 或导致弹窗/窗口假死。
- Reset 后会显式进入重新初始化流程，不遗留后台自检请求；自动重启 worker 并执行唯一一次自检/清零。
- 移除底部“重新检查”按钮；重新自检由 Connect 覆盖，硬件恢复由 Reset 覆盖。
- 移除正常运行按钮 tooltip/弹窗式提示；连接、停止、重置、帮助、预检启动和阀门按钮点击时只更新按钮状态、状态栏或页面文本。
- Stop 成功后如 worker 后续补发 disconnected telemetry，不再生成安全状态提示；只有非预期断连才进入 DATA_STALE/紧急关闭路径。
- 预检区不再显示“安全状态：SAFE”提示框；Reset 会把气道按钮恢复到初始未选/未亮状态，持续时间结束仍保持预选状态不变。

### File List
- docs/sprint-artifacts/1-3-global-safety-toolbar.md
- docs/sprint-artifacts/sprint-status.yaml
- app/controllers/main_controller.py
- app/views/main_window.py
- tests/test_app.py

### Change Log
- 2025-12-09：完成全局工具栏 UI，新增禁用提示与按钮状态护栏测试，保持状态栏文案与安全/自检联动。

