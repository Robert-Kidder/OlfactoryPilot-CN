# OlfactoryPilot-CN 项目结构说明

本文档用于帮助新接手开发者理解当前仓库结构、工程工具链、BMAD 资料位置和项目进度。后续如果目录结构发生变化，应同步更新本文档，避免代码和文档分散到不明确的位置。

## 1. 项目目标

OlfactoryPilot-CN 是一个 Windows 桌面软件，目标是替代原有法国 LabView 软件 ProgOlfacto，实现嗅觉刺激实验控制软件的国产化。

核心目标包括：

- 使用中文界面降低本地实验人员使用门槛。
- 控制 NI USB-6001/6501、RS232 Alicat 质量流量控制器和气味阀门。
- 提供安全联锁，避免无气流时误开阀门或加热导致硬件损坏。
- 支持呼吸信号采集、阈值校准、预实验手动控制、协议执行、TTL 触发和数据记录。
- 保留硬件模拟模式，使没有真实硬件时也能开发、测试和演示。

原法国软件说明书保存在：

- `docs/ManuelUtilisation_ProgOlfacto.pdf`

它是后续对照功能、补齐需求和确认兼容行为的重要参考资料。

## 2. 顶层目录职责

当前项目应保持以下结构：

```text
OlfactoryPilot-CN/
  app/                    # 应用主代码
  config/                 # 默认配置和配置包
  docs/                   # 项目知识库、需求、设计、BMAD 产物、手册
  scripts/                # 工程脚本和辅助脚本
  tests/                  # 自动化测试
  .github/workflows/      # GitHub Actions CI 配置
  .vscode/                # 本地编辑器设置
  .agents/                # 当前 Codex/BMAD 技能文件，通常不提交
  _bmad/                  # 当前 BMAD 本地工具配置，通常不提交
  _bmad-output/           # 当前 BMAD 输出目录，通常不提交
  logs/                   # 运行日志，通常不提交
```

后续约定：

- 业务代码只放在 `app/`。
- 可配置参数放在 `config/`，不要散落在业务代码里。
- 长期项目资料、需求、说明书、故事文件放在 `docs/`。
- 临时文件不要长期保留在根目录或 `tmp/`，需要留档的内容应移动到 `docs/` 或 `_bmad-output/`。
- 自动测试只放在 `tests/`。
- 一次性或可复用工程命令放在 `scripts/`。

## 3. app 目录

`app/` 是软件本体，采用 PySide6 + MVC + Worker Thread 的结构。

```text
app/
  main.py
  controllers/
  models/
  services/
  views/
  workers/
```

### 3.1 app/main.py

应用入口文件。

主要职责：

- 解析启动参数，例如模拟模式、跳过 worker 等。
- 读取 `config/default_config.json`。
- 创建 Qt 应用、主窗口、控制器、状态对象和硬件 worker。
- 启动桌面程序。

常用运行方式：

```powershell
D:\miniconda3\envs\code\python.exe -m app.main
D:\miniconda3\envs\code\python.exe -m app.main --simulation
D:\miniconda3\envs\code\python.exe -m app.main --no-worker
```

### 3.2 app/controllers

控制器层，负责连接 UI、状态和服务。

当前核心文件：

- `app/controllers/main_controller.py`

主要职责：

- 响应界面按钮和用户操作。
- 调用服务层执行安全检查、阀门控制、流量设置、校准等逻辑。
- 把硬件 worker 或 HAL 的结果更新到界面和应用状态。
- 维护主窗口各功能区之间的协调。

原则：

- 控制器可以编排流程，但不应直接写复杂硬件细节。
- 硬件细节应放到 `services/` 或 `workers/`。

### 3.3 app/models

模型层，保存结构化状态和数据对象。

当前主要文件：

- `app/models/app_state.py`：全局应用状态，例如连接状态、安全状态、流量、阈值、当前模式等。
- `app/models/safety_state.py`：安全状态枚举或安全状态相关模型。
- `app/models/self_check.py`：设备自检结果模型。

原则：

- model 只表达状态和数据，不直接操作 UI 或硬件。
- 新增长期状态时优先放在 model，而不是临时挂在窗口或控制器对象上。

### 3.4 app/views

视图层，负责 PySide6 界面组件。

当前主要文件：

- `app/views/main_window.py`：主窗口、顶部工具栏、状态区和页面容器。
- `app/views/calibration_view.py`：呼吸信号校准界面。
- `app/views/pretest_view.py`：预实验手动控制、阀门矩阵、流量设置等界面。

原则：

- view 应尽量是被动的，只负责展示和发出信号。
- 业务判断不要塞进 view，交给 controller 或 service。
- 界面文字应使用中文。

### 3.5 app/services

服务层，承载核心业务逻辑和硬件抽象。

当前主要文件：

- `app/services/hal.py`：硬件抽象接口，定义真实硬件和模拟硬件共同遵守的方法。
- `app/services/real_hal.py`：真实硬件实现，连接 NI-DAQmx 和串口设备。
- `app/services/mock_hal.py`：模拟硬件实现，用于无硬件开发和测试。
- `app/services/hardware_check_service.py`：设备自检逻辑。
- `app/services/safety_manager.py`：安全联锁和安全状态判断。
- `app/services/shutdown_service.py`：退出、急停和阀门复位逻辑。
- `app/services/valve_service.py`：阀门控制逻辑。
- `app/services/flow_service.py`：A/B/C 流量控制和补偿逻辑。
- `app/services/calibration_service.py`：呼吸阈值和校准逻辑。
- `app/services/breath_metrics.py`：呼吸数据缓存、帧率统计等。

原则：

- 硬件驱动细节只应通过 HAL 访问。
- 安全逻辑必须集中，不能在多个 UI 按钮里各写一套。
- 真实硬件和模拟硬件应尽量共享同一接口，方便测试。

### 3.6 app/workers

后台线程层。

当前主要文件：

- `app/workers/hardware_worker.py`

主要职责：

- 在专用线程中与 HAL 交互。
- 周期性读取硬件状态、气流值、呼吸信号。
- 通过 Qt signals/slots 把状态发送回 UI。
- 降低 UI 卡顿对硬件控制和安全状态更新的影响。

原则：

- 不要在 UI 主线程里直接执行耗时硬件操作。
- worker 与 UI 之间使用信号传递，不共享复杂可变状态。

## 4. config 目录

`config/default_config.json` 是默认运行配置。

它通常包含：

- 窗口标题和界面默认值。
- NI 设备 ID 和通道映射。
- 阀门映射。
- 串口和 Alicat MFC 默认参数。
- 气流安全阈值。
- 模拟模式相关默认值。

原则：

- 硬件接线、端口、阈值等可变参数应放到配置里。
- 不要把实验室特定端口硬编码在业务代码里。
- 后续如果做 Options 设置页，保存结果也应走配置体系。

## 5. docs 目录

`docs/` 是项目知识库，也是 BMAD 当前配置里的 `project_knowledge` 路径。

重要文件：

- `docs/project-context.md`：项目上下文，给 AI 和开发者快速理解项目。
- `docs/prd.md`：产品需求文档，定义软件要做什么。
- `docs/ux-design.md`：UX 设计说明，定义界面结构、交互和视觉规则。
- `docs/architecture.md`：架构说明，定义系统如何组织。
- `docs/epics.md`：Epic 和 Story 拆解。
- `docs/implementation-readiness-report-2025-12-08.md`：实施就绪评估报告。
- `docs/bmm-workflow-status.yaml`：BMAD 规划阶段状态。
- `docs/FeatureList.md`：功能清单。
- `docs/ALICAT-MANUAL.md`：Alicat 相关说明。
- `docs/ManuelUtilisation_ProgOlfacto.pdf`：原法国 ProgOlfacto 软件说明书。

`docs/sprint-artifacts/` 保存开发故事、sprint 状态、回顾和验证报告。

后续约定：

- 新 story、验证报告、回顾、需求修订都放在 `docs/sprint-artifacts/`。
- 面向长期维护者的说明放在 `docs/` 根部。
- 大型外部参考资料也放在 `docs/`，并在相关说明文档中引用。

## 6. tests 目录

`tests/` 保存 pytest 自动化测试。

测试覆盖了：

- 应用启动和基础行为。
- 安全联锁。
- 模拟模式。
- 呼吸信号与校准逻辑。
- 阀门服务。
- 流量服务。
- 预实验界面。
- 控制器与服务的集成行为。

测试的价值：

- 防止后续修改破坏已经完成的 Epic 1 和 Epic 2 功能。
- 在没有真实硬件时，用 Mock HAL 验证主要逻辑。
- 每次提交前提供最低限度的安全网。

运行方式：

```powershell
D:\miniconda3\envs\code\python.exe -m pytest
```

## 7. scripts 目录

`scripts/` 保存工程辅助脚本。

当前主要文件：

- `scripts/run-ci.ps1`：本地执行 lint、test、build 或完整 CI 流程。
- `scripts/probe_alicat.py`：Alicat 串口设备探测辅助脚本。
- `scripts/generate_sprint_status.py`：生成或维护 sprint 状态的辅助脚本。

原则：

- 可以重复使用的工程命令放到 `scripts/`。
- 不应把临时命令散落在根目录。

## 8. requirements.txt 和 requirements-dev.txt

项目有两个依赖文件是正常做法。

### 8.1 requirements.txt

`requirements.txt` 是运行软件必须安装的依赖。

当前包含：

- `PySide6`：桌面 UI 框架。
- `pyqtgraph`：实时图表绘制，主要用于呼吸波形。
- `numpy`：数值计算。
- `nidaqmx`：NI 数据采集设备驱动接口。
- `pyserial`：串口通信，用于 Alicat MFC。

简单理解：只想运行软件，至少需要这些。

安装方式：

```powershell
D:\miniconda3\envs\code\python.exe -m pip install -r requirements.txt
```

### 8.2 requirements-dev.txt

`requirements-dev.txt` 是开发、测试、检查和打包时需要的依赖。

它第一行是：

```text
-r requirements.txt
```

意思是：先安装运行依赖，再安装开发工具。

当前额外包含：

- `pytest`：运行自动化测试。
- `ruff`：代码质量检查和格式整理。
- `pyinstaller`：把 Python 程序打包成 Windows 可执行程序。

简单理解：要开发这个项目，应安装 `requirements-dev.txt`；只部署运行时才考虑只装 `requirements.txt`。

安装方式：

```powershell
D:\miniconda3\envs\code\python.exe -m pip install -r requirements-dev.txt
```

## 9. pytest、ruff、CI 和 PyInstaller

### 9.1 pytest

pytest 是 Python 测试框架。

本项目通过 `pytest.ini` 配置测试：

- 测试目录：`tests`
- 默认参数：`-ra`
- 忽略部分第三方包的弃用警告

运行：

```powershell
D:\miniconda3\envs\code\python.exe -m pytest
```

当前验证结果：

```text
122 passed
```

### 9.2 ruff

ruff 是代码检查工具，速度快，适合在每次提交前运行。

本项目通过 `ruff.toml` 配置：

- Python 目标版本：`py310`
- 单行长度：`120`
- 检查规则：基础错误、导入排序、现代 Python 写法、常见 bug 风险
- 忽略：`E402` 和 `E501`

运行：

```powershell
D:\miniconda3\envs\code\python.exe -m ruff check app tests
```

自动修复可安全机械整理的问题：

```powershell
D:\miniconda3\envs\code\python.exe -m ruff check app tests --fix
```

当前验证结果：

```text
All checks passed!
```

### 9.3 CI

CI 是 Continuous Integration，中文可以理解为“持续集成”。

本项目的 GitHub Actions 配置在：

- `.github/workflows/ci.yml`

它的作用是在推送到 GitHub 或创建 Pull Request 时自动执行检查，通常包括：

- 安装依赖。
- 运行 ruff。
- 运行 pytest。
- 用 PyInstaller 打包。

价值：

- 防止只在本地能跑、推到仓库后才发现坏掉。
- 给每次提交一个自动质量门槛。
- 以后多人协作时尤其重要。

### 9.4 PyInstaller

PyInstaller 用来把 Python 桌面程序打包成 Windows 可执行文件。

配置文件：

- `pyinstaller.spec`

运行：

```powershell
D:\miniconda3\envs\code\python.exe -m PyInstaller pyinstaller.spec
```

打包产物通常在：

- `dist/OlfactoryPilot/`

## 10. .github、.vscode、.gitignore

### 10.1 .github/workflows

保存 GitHub Actions 自动化流程。

这个目录应该提交到 Git，因为它定义了远程仓库如何自动检查项目。

### 10.2 .vscode

保存 VS Code/Cursor 等编辑器设置。

目前 `.gitignore` 忽略 `.vscode/`，说明它被视为本地开发者设置。除非团队明确需要共享编辑器设置，否则可以不提交。

### 10.3 .gitignore

`.gitignore` 定义哪些文件不进入 Git。

当前应忽略：

- Python 缓存。
- 虚拟环境。
- 构建产物。
- 测试和 lint 缓存。
- 本地 BMAD/AI 工具产物。
- 运行日志。
- 实验输出数据。

这样可以避免把环境文件、日志、打包产物、临时数据提交到仓库。

## 11. BMAD 相关文件应该如何理解

当前项目正在使用 BMAD 方法管理需求和开发节奏。

当前有效的 BMAD/Codex 本地工具目录：

- `.agents/`
- `_bmad/`
- `_bmad-output/`

这些目录是当前本地 AI/BMAD 工具运行需要的环境，一般不作为应用源代码提交。

长期项目知识与 BMAD 产物应保存在：

- `docs/`
- `docs/sprint-artifacts/`

旧的上一版 BMAD 工具安装产物：

- `.bmad/`
- `.codex/`
- `codex.cmd`

这些已经不属于当前有效项目结构。清理它们可以减少重复工具链造成的混乱。提交时应明确写成“清理旧 BMAD 工具产物”，方便以后从 Git 历史理解原因。

## 12. 当前项目进度

根据 `docs/bmm-workflow-status.yaml`：

- PRD 已完成：`docs/prd.md`
- UX 设计已完成：`docs/ux-design.md`
- 架构设计已完成：`docs/architecture.md`
- Epic 和 Story 拆解已完成：`docs/epics.md`
- 实施就绪检查已完成或已有报告：`docs/implementation-readiness-report-2025-12-08.md`
- Sprint planning 已有状态文件：`docs/sprint-artifacts/sprint-status.yaml`

根据 `docs/sprint-artifacts/sprint-status.yaml`：

- Epic 1：安全硬件基础，已经完成并进入真实硬件验证/回顾阶段。
- Epic 2：校准与手动控制，故事 2.1 到 2.7 均标记为 done。
- Epic 3：协议执行与数据记录，仍在 backlog。
- Epic 4：运维、清洗和中文本地化，仍在 backlog。

因此当前最合理的下一步是进入 Epic 3。

建议下一个故事：

- `3-1-protocol-file-parsing-txtcsv`

目标是实现或完善 `.txt/.csv` 协议文件解析，为后续呼吸门控刺激、手动/TTL 触发、低抖动执行和数据记录打基础。

## 13. 后续开发放置规则

新增功能时建议遵守：

- 新 UI：放入 `app/views/`。
- 新业务编排：放入 `app/controllers/`。
- 新状态对象：放入 `app/models/`。
- 新硬件或业务服务：放入 `app/services/`。
- 新后台线程：放入 `app/workers/`。
- 新配置项：放入 `config/default_config.json`。
- 新测试：放入 `tests/`，文件名使用 `test_*.py`。
- 新工程脚本：放入 `scripts/`。
- 新需求、故事、评审、回顾：放入 `docs/` 或 `docs/sprint-artifacts/`。

不建议：

- 把业务代码直接放在项目根目录。
- 把临时测试脚本长期留在根目录。
- 把硬件端口写死在 UI 代码里。
- 把同一个安全逻辑复制到多个按钮回调里。
- 把生成的日志、打包结果、缓存文件提交到 Git。

## 14. 常用命令

安装开发依赖：

```powershell
D:\miniconda3\envs\code\python.exe -m pip install -r requirements-dev.txt
```

运行测试：

```powershell
D:\miniconda3\envs\code\python.exe -m pytest
```

运行代码检查：

```powershell
D:\miniconda3\envs\code\python.exe -m ruff check app tests
```

自动修复格式类问题：

```powershell
D:\miniconda3\envs\code\python.exe -m ruff check app tests --fix
```

运行软件：

```powershell
D:\miniconda3\envs\code\python.exe -m app.main
```

模拟模式运行：

```powershell
D:\miniconda3\envs\code\python.exe -m app.main --simulation
```

打包：

```powershell
D:\miniconda3\envs\code\python.exe -m PyInstaller pyinstaller.spec
```

## 15. 推荐下一步 BMAD 流程

建议在新上下文窗口中依次执行：

1. `[SS] Sprint Status`，技能名：`bmad-sprint-status`
   - 目的：确认当前 sprint 状态，锁定下一个待开发 story。

2. `[CS] Create Story`，技能名：`bmad-create-story`
   - 目的：为 Epic 3 的第一个故事生成或更新开发用 story 文件。

3. `[DS] Dev Story`，技能名：`bmad-dev-story`
   - 目的：按 story 实现功能、补充测试并完成验证。

当前推荐从 `3-1-protocol-file-parsing-txtcsv` 开始。
