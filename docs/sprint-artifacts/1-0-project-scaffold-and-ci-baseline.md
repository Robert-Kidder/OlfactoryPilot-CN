# 故事 1.0：Project Scaffold and CI Baseline
Status: real-hardware-pass
Epic: 1 - Safe Hardware Foundations
Story Key: 1-0-project-scaffold-and-ci-baseline

## Story
作为开发者，  
我需要一个可运行的 PySide6/MVC 脚手架并配好 lint/测试/打包的 CI，  
这样团队可以在第一天就安全构建与交付。

## Acceptance Criteria
1. **骨架可运行**：仓库初始化后，安装依赖（pip/requirements）即可启动 PySide6 MVC 骨架，显示占位窗口且无错误。
2. **本地与 CI 工具链通过**：安装工具后，运行 lint（ruff/flake8）与 pytest 全部通过，本地与 CI 保持一致。
3. **打包可用**：配置 pyinstaller，运行打包任务生成 Windows 可执行产物，并输出大小/哈希/路径。
4. **CI 守护**：push/PR 触发 CI，执行 lint/测试/打包，任一失败则构建失败。

## Tasks / Subtasks
- [x] 初始化项目结构与依赖（PySide6/MVC 骨架、requirements、基础配置）
  - [x] 锁定 Python 3.11 兼容的 PySide6、pyqtgraph、nidaqmx、pyserial 依赖
  - [x] 创建占位 UI（主窗口 + 顶部标签页框架），验证可运行
  - [x] 配置 lint 与测试：ruff/pytest 配置与最小示例测试
  - [x] 配置打包：pyinstaller spec/配置，验证生成的 Windows 可执行文件
  - [x] 配置 CI：在 push/PR 上运行 lint、pytest、pyinstaller（含工件输出与日志）
- [x] 输出构建/打包日志（尺寸、哈希、产物路径）并归档

## Dev Notes
- 采用 MVC + Worker Thread 结构：UI 只做展示，控制器协调逻辑，硬件 Worker 线程预留。
- 代码需保持未来扩展安全模块：空气流量阈值、低延迟执行、线程安全的信号/槽。
- 全量中文界面与消息占位，后续故事复用。

### Project Structure Notes
- 遵循架构文档中的目录：`app/controllers|models|views|workers|services`，`config/`，`docs/`，`tests/`。
- 预留数据记录线程与安全模块占位，保持文件命名/模块接口与后续故事兼容。

### References
- Source: docs/epics.md (Epic 1, Story 1.0)
- Source: docs/architecture.md#1.0
- Source: docs/prd.md
- Source: docs/ux-design.md

## Developer Context
- 目标：提供可运行的 PySide6/MVC 骨架、统一工具链与 CI 基线，为后续安全/硬件故事提供稳定承载。
- 范围：项目结构、依赖、基础 UI 占位、lint/测试/打包、CI 配置；不实现硬件控制或业务流程。
- 成功定义：本地一键安装与运行成功；CI 自动执行 lint/pytest/pyinstaller 并产出可执行文件；中文界面与安全占位清晰。

## Technical Requirements
- 平台：Windows 10/11，Python 3.11（建议锁定 3.10/3.11 以兼容 PySide6、nidaqmx）。
- 线程模型：预置 Worker 线程与信号/槽通路；UI 禁止直接硬件调用。
- 打包：pyinstaller 生成单机可执行，记录产物路径/大小/哈希；确保打包包含 Qt 依赖与资源。
- 配置：requirements.txt 与 requirements-dev.txt 分工明确，CI 使用 requirements-dev.txt。
- 日志：基础日志配置（info/debug），后续故事可扩展到安全/性能日志。

## Architecture Compliance
- 模式：MVC + Worker Thread（参见 docs/architecture.md），视图被动，控制器协调，模型存放全局状态。
- 目录：`app/` 下分 controllers/models/views/workers/services，与 `config/`、`docs/`、`tests/` 对齐架构图。
- 信号/槽：预置 Worker -> UI 的信号（连接状态、气流、安全状态、日志）与 UI -> Worker 的命令入口（后续故事填充）。
- 数据记录：预置 DataLogger 线程接口与占位文件，符合“数据记录线程写盘，UI 与 Worker 通过队列通信”要求。
- 安全占位：UI/控制器在无安全状态时不直接发硬件命令（占位逻辑/注释说明）。

## Library & Framework Requirements
- Python 3.11；固定 minor 版本。
- PySide6（建议 6.7+ LTS），启用 Qt Widgets；确保与 pyinstaller 兼容。
- pyqtgraph（0.13+）用于后续 100Hz 波形；保持与 PySide6 版本匹配。
- nidaqmx（0.9+）与 pyserial（3.5+）预留依赖但可在硬件故事中按需安装；需在 README/CI 中标注可选或使用 extras。
- 工具链：ruff 或 flake8，pytest，pyinstaller 6+，CI 中统一版本以避免漂移。

## File Structure Requirements
- 根目录：`app/main.py` 启动应用；`app/views/main_window.py` 创建占位窗口与 Tab 框架。
- 子目录：`app/controllers/main_controller.py`，`app/workers/hardware_worker.py`（线程占位），`app/services/*`（HAL/串口/NI 接口占位），`app/models/app_state.py`。
- 配置：`config/default_config.json`（含语言=zh-CN、窗口标题、日志级别占位）；`pyproject.toml` 或 `requirements.txt` + `requirements-dev.txt`。
- 构建：`pyinstaller.spec`（或脚本），`scripts/*.ps1` 用于本地与 CI 调用一致。
- 文档：`docs/` 保持 PRD/架构/UX；在 README 中列出安装、运行、打包、CI 说明。

## Testing Requirements
- Lint：ruff/flake8 配置（如 ruff.toml）且 CI 强制执行。
- 单测：pytest 最小用例（窗口可创建、模块可导入、线程占位可初始化）；可用 `app/main.py --help` 或模块加载进行冒烟。
- CI：工作流执行顺序 lint -> pytest -> pyinstaller；缓存依赖；工件上传 exe/日志；失败即阻断。
- 度量：记录测试/打包日志并在 CI 控制台输出，便于后续回溯。

## Latest Tech Information
- Python 3.11/3.12 与 PySide6 6.7+ 兼容性良好；若需支持更高版本，需验证 pyinstaller 与 Qt 插件打包。
- PySide6 6.7 LTS/6.8 稳定版推荐；打包时需启用 `--hidden-import` 针对 Qt 插件并测试中文字体。
- pyinstaller 6.x 建议使用 onedir 方案以降低体积与依赖缺失风险；如需 onefile，需验证启动时解压速度。
- ruff/pytest 版本固定于 CI（如 ruff 0.6+/pytest 7.4+）以避免规则漂移；锁定到 requirements-dev。
- nidaqmx/pyserial 标注为可选 extra，避免 CI 硬件依赖；必要时使用 mock/skip 以通过测试。

## Project Context Reference
- docs/project-context.md：Windows 10/11 桌面、PySide6、气流安全阈值、20ms 执行抖动目标、全中文 UI。
- FR 覆盖：FR1.1-1.4 是后续故事核心，脚手架需支持安全线程/日志与全局工具栏占位。
- UX：顶部标签栏与全局状态/安全提示区域需在占位窗口中预留。

## Story Completion Status
- 状态：review
- 产物：docs/sprint-artifacts/1-0-project-scaffold-and-ci-baseline.md
- 下一步：可运行脚手架与 CI pipeline 实作后严格对标本故事；完成后可运行 `*validate-create-story` 做质量竞赛复核。

## Dev Agent Record
### Implementation Notes
- 按 MVC+Worker 结构创建骨架：`app/main.py` 启动，`views/main_window.py` 标签页占位，`controllers/main_controller.py` 绑定，`workers/hardware_worker.py` 占位线程信号，`models/app_state.py` 状态与遥测，`services/safety_manager.py` 安全阈值占位。
- 工具链与配置：`requirements*.txt` 锁定依赖，`ruff.toml`、`pytest.ini`、`.gitignore`、`scripts/run-ci.ps1`（统一 lint/pytest/pyinstaller），`pyinstaller.spec` onedir 打包携带默认配置并裁剪未用 Qt 模块。
- CI：`.github/workflows/ci.yml` Windows 上依次跑 ruff -> pytest -> PyInstaller，上传 dist 工件并打印前 10 大文件与哈希。
- 文档：`README.md` 说明安装、运行、打包、CI；默认配置 `config/default_config.json`（zh-CN、窗口标题、日志级别、安全阈值/telemetry 频率）。

### Completion Notes
- Lint：`python -m ruff check app tests` 通过。
- 测试：`python -m pytest` 通过（4 项）。
- 打包：`python -m PyInstaller pyinstaller.spec` 通过，产物 `dist/OlfactoryPilot.exe`，大小约 44.9 MB，SHA256=8D2635CE58669F1FAAE14A623175A389BCD95D2F5F23D141D8B36914135768DD；onidir 目录 `dist/OlfactoryPilot/`。
- 已记录 PyInstaller 警告（WinRT 相关插件缺失 DLL，不阻塞打包）；打包配置排除 WebEngine/Quick/Multimedia 等未用模块以缩小体积。

## File List
- .gitignore
- .github/workflows/ci.yml
- README.md
- requirements.txt
- requirements-dev.txt
- ruff.toml
- pytest.ini
- config/default_config.json
- config/__init__.py
- app/__init__.py
- app/main.py
- app/controllers/__init__.py
- app/controllers/main_controller.py
- app/models/__init__.py
- app/models/app_state.py
- app/views/__init__.py
- app/views/main_window.py
- app/workers/__init__.py
- app/workers/hardware_worker.py
- app/services/__init__.py
- app/services/safety_manager.py
- tests/__init__.py
- tests/test_app.py
- pyinstaller.spec
- scripts/run-ci.ps1
- docs/sprint-artifacts/1-0-project-scaffold-and-ci-baseline.md
- docs/sprint-artifacts/sprint-status.yaml

## Change Log
- 初始化 PySide6/MVC 骨架与占位 UI/Worker，添加安全与配置占位。
- 配置 ruff/pytest/pyinstaller 工具链与本地/CI 脚本，完善 README。
- 建立 CI 工作流（Windows）执行 lint/pytest/pyinstaller 并上传工件。
- 完成打包验证并记录产物大小与哈希。

