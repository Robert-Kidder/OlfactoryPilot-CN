# 故事 1.1: Device Self-Check and Status Report
Status: Ready for Review
Epic: 1 - Safe Hardware Foundations
Story Key: 1-1-device-self-check-and-status-report
Story ID: 1.1

## Story
作为实验室技术员，  
我希望系统在启动时验证 NI-USB-6001、NI-USB-6501 和 RS232 端口并展示状态报告，  
以便在确认硬件连接与波特率正确后再继续任何操作。

## Acceptance Criteria
1. **启动自检可见**：应用启动时自动检测 NI-USB-6001、NI-USB-6501、RS232（COM 口 + 波特率），对每个项返回 Pass/Fail + 失败原因，并在 UI 状态区呈现摘要列表（每项独立状态）。
2. **故障阻断**：若任一设备缺失、被占用或波特率不匹配，则阻断 Connect/Protocol/Pre-test/Apply 等硬件指令，并给出中文错误提示与重试指引（含建议 COM/波特率、重新插拔/重试步骤）。
3. **恢复解锁**：修复故障后（设备重新可用或波特率修正），自检可重新执行并解除阻断，状态报告刷新为 Pass。
4. **日志与可追溯性**：自检结果、失败原因、重试/恢复时间写入日志；UI 同步展示最近一次自检时间。

## Tasks / Subtasks
- [x] 自检引擎（AC1/AC2）
  - [x] 枚举 NI-USB-6001/6501（nidaqmx System.devices，匹配产品名/ID），检测占用/断开并返回详情。
  - [x] 枚举 RS232（pyserial list_ports），读取配置（预设波特率）并验证能否打开；失败时返回占用/权限/配置错误。
  - [x] 生成结构化自检结果对象：每设备状态、原因、建议动作、时间戳。
- [x] UI 状态报告（AC1/AC3）
  - [x] 将自检结果通过信号/槽推送到 UI（被动视图），在全局状态区展示 Pass/Fail 列表、最近检测时间。
  - [x] 提供“重新检测”入口（允许在故障后重试，保持线程安全，避免阻塞 UI）。
- [x] 故障阻断与恢复（AC2/AC3）
  - [x] 在控制器/安全管理层维护“hardware_ready”状态；Fail 时禁用/短路 Connect、Protocol、Pre-test、Apply 流程。
  - [x] 故障恢复后清除阻断并刷新 UI 状态；必要时触发安全复位（关闭阀门/停止命令）。
- [x] 日志与可观测性（AC4）
  - [x] 记录自检结果、错误码、COM/波特率、重试次数到日志；异常捕获写入。
  - [x] 在调试模式下追加详细栈与底层库错误信息，方便支持。

## Dev Notes（story_requirements）
- 本故事覆盖 FR1.1：启动自检；为后续 FR1.2（安全联锁）、FR1.3（安全退出）奠定硬件前置条件，需确保“未通过自检即不允许任何硬件控制”。
- UI 必须中文提示；状态摘要需要一目了然（每设备一行，含通过/失败、原因、建议动作）。
- 自检在硬件 Worker 线程中执行，UI 仅显示结果；避免 UI 线程直接访问硬件。
- 与 Story 1.2 Safe Start Airflow Interlock 兼容：当自检未通过时，同步阻断气流阈值检查链路，防止绕过。

## Developer Context（developer_context_section）
- 场景：Windows 10/11 桌面，Python 3.10+，PySide6 + MVC + Worker 模式，硬件 Worker 负责实时控制与自检，UI 仅被动显示。
- 硬件：NI-USB-6001/6501；RS232 质量流量控制器（可配置 COM 与波特率，默认 115200/9600 依据设备，需在配置文件记录）。
- 安全：任何自检失败都应将安全状态置为“不可用”，并阻断阀门/加热命令；自检成功后才能解除。
- 性能：自检在启动阶段运行一次，可在用户点击“重新检测”时重复；避免长阻塞（>500ms）影响 UI 线程。
- 观测性：日志记录自检结果与重试，便于追踪设备不稳定问题。

## Technical Requirements（technical_requirements）
- **自检实现**
  - NI 检测：使用 `nidaqmx.system.System.local().devices`，校验 6001/6501 是否存在；捕获 `DaqError` 作为 Fail 并报告驱动/权限问题。
  - RS232 检测：使用 `serial.tools.list_ports.comports()` 列出端口，匹配配置中的 COM；尝试以配置波特率打开串口并立即关闭；占用/权限/波特率错误均需分类提示。
  - 结果结构：`[{name, type, status, reason, suggestion, checked_at}]`，供 UI 直接渲染。
- **阻断与解锁**
  - 维护 `hardware_ready: bool` 与故障详情；Fail 时禁用连接/协议/预检/Apply 调用（控制器侧守护），Worker 收到命令时也应二次短路。
  - 恢复路径：重新检测成功后自动解锁，并在日志与 UI 中记录恢复时间。
- **UI 展示**
  - 在全局状态区（footer/toolbar）显示列表：设备名、状态、原因/建议、最近检测时间；颜色/图标区分 Pass/Fail。
  - 提示文案中文、简洁，示例：“NI-USB-6501：失败（未检测到设备）。请检查 USB 连接并重试。”
- **配置与持久化**
  - 在 `config/default_config.json` 保留/新增串口与波特率配置；确保与 Options 页保存格式兼容。
  - 自检不修改配置，仅读取；日志写入现有 logging 配置。
- **错误处理**
  - 捕获驱动缺失、设备被占用、权限不足、波特率错误等典型异常；附带建议动作（插拔、调整波特率、关闭占用程序、安装 NI-DAQmx）。
  - 避免无限重试；交由用户点击“重新检测”触发。

## Architecture Compliance（architecture_compliance）
- 模式：严格遵守 MVC + Worker（docs/architecture.md）；自检逻辑在硬件 Worker/Service 层，UI 为被动视图。
- 线程：Worker -> UI 使用信号/槽推送自检结果；避免 UI 直接访问硬件；阻断逻辑在控制器 + Safety/Hardware 状态集中管理。
- 日志：沿用 Data Logger/应用日志渠道；记录时间戳、设备名、状态、原因。
- 性能/安全：保持 UI 响应，失败时立即短路控制命令，避免未授权硬件操作。

## Library & Framework Requirements（library_framework_requirements）
- Python 3.10+（推荐 3.11）；与 PySide6 6.10.1 兼容性良好，若使用 3.12 需验证 PyInstaller。
- PySide6 最新 6.10.1（当前安装 6.7.2，可评估升级；升级后需验证 PyInstaller onedir/onefile）。
- pyqtgraph 最新 0.14.0（当前安装 0.13.7，若需升级需验证绘图性能与依赖）。
- nidaqmx 最新 1.3.0（当前 0.9.0，升级可获取新设备支持；注意 NI-DAQmx 驱动版本匹配）。
- pyserial 3.5（已为最新）。
- PyInstaller 6.x：升级 PySide6 时需确认 hidden-import/Qt 插件完整，中文字体打包可用。

## File Structure Requirements（file_structure_requirements）
- 复用现有骨架（Story 1.0）：
  - `app/workers/hardware_worker.py`：添加自检任务与结果信号；在命令入口处短路检查 `hardware_ready`。
  - `app/services/`：若已有 SafetyManager/SerialService，添加自检方法与状态聚合；否则新增 `hardware_check_service.py`。
  - `app/controllers/main_controller.py`：协调启动自检、处理结果、更新 UI 状态、控制阻断。
  - `app/views/main_window.py` + 相关状态区组件：展示自检列表与“重新检测”按钮。
  - `config/default_config.json`：确保包含串口/波特率配置键（示例：`serial_port`, `baud_rate`），并与 Options 页一致。
  - `tests/`：新增自检单元/集成测试（见下）。
- 保持目录与命名：`app/controllers|models|views|workers|services`、`config/`、`docs/`、`tests/`，符合 architecture.md。

## Testing Requirements（testing_requirements）
- 单元测试：
  - 模拟 NI 设备列表，验证缺失/存在时结果正确分类。
  - 模拟串口占用/波特率错误/权限错误，验证错误消息与阻断标志。
  - 验证结果结构含时间戳、建议动作。
- 集成测试（可使用 mock）：
  - 启动时自检被调用且 UI 状态信号触发。
  - 阻断逻辑：自检 Fail 时 Connect/Protocol/Pre-test/Apply 被拒绝（返回错误码/提示），恢复后解锁。
  - 日志写入包含失败原因与重试次数。
- 性能/稳定性：
  - 自检执行时间 <500ms（mock 环境）且不阻塞 UI。
  - 多次“重新检测”不会泄露资源（串口关闭、线程清理）。

## Previous Story Intelligence（previous_story_intelligence）
- Story 1.0 已建立 PySide6 MVC + Worker 骨架、CI、打包；路径与模块命名应沿用（如 `app/main.py`、`controllers/main_controller.py`、`workers/hardware_worker.py`、`services/safety_manager.py`）。
- 现有日志与 CI/打包配置可直接复用；新增自检日志需遵守同一 logging 配置。
- UI 语言已中文化，继续保持中文提示与一致措辞。

## Git Intelligence（git_intelligence_summary）
- 当前目录未检测到 git 仓库；如需提交，请初始化 git 并将故事文件纳入版本控制。

## Latest Tech Information（latest_tech_information）
- PySide6 最新 6.10.1（当前 6.7.2）；升级可获得 Qt bugfix，需重新验证 PyInstaller 打包。
- pyqtgraph 最新 0.14.0（当前 0.13.7）；升级后检查 100Hz 绘图性能与 Qt 兼容。
- nidaqmx 最新 1.3.0（当前 0.9.0）；升级前确认 NI-DAQmx 驱动版本匹配，避免接口变更。
- pyserial 3.5 为最新；保持。

## Project Context Reference（project_context_reference）
- docs/project-context.md（项目范围/目标/风险）
- docs/epics.md（Epic 1 & Story 1.1 需求与 AC）
- docs/prd.md（FR1.1、FR1.2 等安全要求）
- docs/architecture.md（MVC + Worker、线程/日志/结构约束）
- docs/ux-design.md（全局状态区、中文 UI 规范）

## Story Completion Status（story_completion_status）
- 状态：Ready for Review
- 产物：docs/sprint-artifacts/1-1-device-self-check-and-status-report.md
- 下一步：可直接运行 dev-story 开发；建议完成后执行 *validate-create-story 复核质量。
- 完成说明：Ultimate context engine analysis completed - comprehensive developer guide created.

## Change Log
- 2025-12-08：自检引擎完成（NI/RS232 枚举、结构化结果、Worker 启动自检），新增配置键与单元测试。
- 2025-12-08：完成 UI 状态报告、重新检测入口、阻断逻辑与自检日志；新增交互单测，状态更新 Ready for Review。

## Dev Agent Record
### Context Reference
- 本故事文件即上下文；若有附加调试日志，请在此添加路径。

### Agent Model Used
- Codex (GPT-5, yolo mode)

### Debug Log References
- 待开发完成后补充实际运行日志路径。

### Completion Notes List
- 完成自检引擎：新增 hardware_check_service，Worker 启动与重新检测信号，生成结构化 NI/RS232 结果并更新状态。
- 新增单元测试覆盖 NI/串口自检路径（tests/test_app.py）；pytest 全量通过。
- UI 渲染自检摘要，重新检测按钮触发线程安全自检，控制器添加硬件阻断守卫；自检结果写入日志。

### File List
- app/controllers/main_controller.py
- app/main.py
- app/models/__init__.py
- app/models/app_state.py
- app/models/self_check.py
- app/services/__init__.py
- app/services/hardware_check_service.py
- app/workers/hardware_worker.py
- config/default_config.json
- docs/sprint-artifacts/sprint-status.yaml
- docs/sprint-artifacts/1-1-device-self-check-and-status-report.md
- tests/test_app.py
