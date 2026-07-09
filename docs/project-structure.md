# OlfactoryPilot-CN 项目结构说明

本文档用于帮助新接手开发者理解当前代码仓库的文件组织、工程工具链、BMAD 资料位置和后续开发规则。后续如果目录结构变化，应同步更新本文档。

## 1. 项目目标

OlfactoryPilot-CN 是 Windows 桌面嗅觉刺激实验控制软件，目标是替代原有法国软件 ProgOlfactoTao。项目使用 Python 3.11、PySide6、NI-DAQmx 和 pyserial 实现中文本地化、硬件安全联锁、呼吸校准、阀门控制、协议执行和数据记录。

原法国软件说明书保存在：

- `docs/ManuelUtilisation_ProgOlfacto.pdf`

后续功能对齐和行为确认可以参考该 PDF，但当前项目文档以本仓库 `docs/` 下的中文文档为准。

## 2. 顶层目录

```text
OlfactoryPilot-CN/
  app/                    # 应用主代码
  config/                 # 默认配置
  docs/                   # 项目文档、需求、架构、UX、story 和参考资料
  scripts/                # 工程脚本和辅助工具
  tests/                  # 自动化测试
  .github/workflows/      # GitHub Actions 持续集成配置
  .agents/                # 当前 Codex/BMAD 技能文件，本地工具目录
  _bmad/                  # 当前 BMAD 本地配置，本地工具目录
  _bmad-output/           # BMAD 输出目录，本地工具目录
  logs/                   # 运行日志，本地生成目录
```

约定：

- 业务代码只放在 `app/`。
- 可配置参数放在 `config/`，不要硬编码在 UI 或控制器里。
- 长期文档、需求、story、验证记录放在 `docs/`。
- 可复用脚本放在 `scripts/`。
- 自动化测试放在 `tests/`。
- 临时文件不要长期保留在根目录或 `tmp/`。

## 3. app 目录

`app/` 是软件主体，采用 MVC + Worker + HAL。

```text
app/
  main.py
  controllers/
  models/
  services/
  views/
  workers/
```

- `app/main.py`：应用入口，解析启动参数，读取配置，创建 Qt 应用和主窗口。
- `app/controllers/`：控制器层，编排 UI、状态、服务和 Worker。
- `app/models/`：模型层，保存配置、会话、硬件状态和安全状态等结构化数据。
- `app/views/`：界面层，负责 PySide6 组件展示和用户输入。
- `app/services/`：服务层，包含 HAL、硬件自检、安全联锁、阀门、流量、校准等业务逻辑。
- `app/workers/`：后台线程层，负责硬件轮询、状态推送和低抖动执行。

原则：

- View 尽量被动，不直接写复杂业务逻辑。
- Controller 负责流程编排，但不直接访问底层硬件驱动。
- 硬件访问必须通过 HAL。
- 真实硬件和 Mock HAL 应尽量共享同一接口。

## 4. config 目录

`config/default_config.json` 是当前默认配置来源，包含：

- 窗口标题和界面默认值。
- NI 设备 ID 和通道映射。
- 阀门映射。
- Alicat 串口和 MFC 默认参数。
- 气流安全阈值。
- 模拟模式默认值。

后续如果做“选项”页面，保存结果也应进入配置体系，不能让多个配置文件同时成为真实来源。

## 5. docs 目录

`docs/` 是项目知识库。

- `docs/project-context.md`：项目上下文，供开发者和 AI 快速理解项目。
- `docs/prd.md`：产品需求文档，说明软件要做什么。
- `docs/architecture.md`：架构文档，说明软件如何组织。
- `docs/ux-design.md`：UX 设计说明，说明界面结构和交互规则。
- `docs/epics.md`：Epic 和 Story 拆分。
- `docs/FeatureList.md`：功能清单。
- `docs/project-structure.md`：本文档。
- `docs/bmm-workflow-status.yaml`：BMAD 规划阶段状态。
- `docs/sprint-artifacts/`：开发 story、sprint 状态、回顾、验证报告。
- `docs/ALICAT-MANUAL.md`：Alicat 相关说明。
- `docs/ManuelUtilisation_ProgOlfacto.pdf`：原法国软件说明书。

项目文档默认使用简体中文。历史 sprint artifact 中如果包含旧英文或旧事实，应优先在后续相关 story 更新时修正；主线文档必须保持当前准确。

## 6. scripts 目录

- `scripts/run-ci.ps1`：本地执行 `lint`、`test`、`build` 或完整 `ci` 流程。
- `scripts/probe_alicat.py`：Alicat 串口设备探测辅助脚本。
- `scripts/generate_sprint_status.py`：根据 `docs/epics.md` 生成或维护 sprint 状态。

脚本原则：

- 可以复用的工程命令放在 `scripts/`。
- 脚本提示默认使用中文。
- 脚本应使用项目现有 Python 环境和 requirements，不引入额外隐式依赖。

## 7. tests 目录

`tests/` 保存 pytest 自动化测试。测试主要覆盖：

- 应用启动。
- Mock HAL。
- 安全联锁。
- 阀门和流量服务。
- 呼吸校准逻辑。
- 预实验界面。
- 控制器和服务集成行为。

运行：

```powershell
D:\miniconda3\envs\code\python.exe -m pytest
```

测试的意义是防止后续开发破坏已经完成的 Epic 1 和 Epic 2 功能，并允许无真实硬件的 CI 环境验证核心逻辑。

## 8. requirements.txt 与 requirements-dev.txt

项目同时保留两个依赖文件是正常做法。

`requirements.txt` 是运行软件所需依赖，包括 PySide6、pyqtgraph、numpy、nidaqmx、pyserial 等。

安装：

```powershell
D:\miniconda3\envs\code\python.exe -m pip install -r requirements.txt
```

`requirements-dev.txt` 是开发、测试、检查和打包所需依赖。它先引用 `requirements.txt`，再额外安装 pytest、pytest-qt、ruff、PyInstaller 等工具。

安装：

```powershell
D:\miniconda3\envs\code\python.exe -m pip install -r requirements-dev.txt
```

简单理解：只运行软件用 `requirements.txt`；参与开发用 `requirements-dev.txt`。

## 9. pytest、ruff、CI 和 PyInstaller

### pytest

pytest 是 Python 测试框架，配置文件是 `pytest.ini`。

运行：

```powershell
D:\miniconda3\envs\code\python.exe -m pytest
```

### ruff

ruff 是代码检查工具，配置文件是 `ruff.toml`。

当前关键配置：

- Python 目标版本：`py311`。
- 单行长度：`120`。
- 检查规则：基础错误、导入排序、现代 Python 写法和常见 bug 风险。

运行：

```powershell
D:\miniconda3\envs\code\python.exe -m ruff check app tests
```

### CI

CI 是“持续集成”。当前 GitHub Actions 配置位于：

- `.github/workflows/ci.yml`

它会在 push 或 pull request 时安装依赖、运行 ruff、运行 pytest，并用 PyInstaller 打包。这样可以防止只在本地能运行、推送后才发现问题。

### PyInstaller

PyInstaller 用于把 Python 桌面程序打包成 Windows 可执行文件。

配置文件：

- `pyinstaller.spec`

运行：

```powershell
D:\miniconda3\envs\code\python.exe -m PyInstaller pyinstaller.spec
```

产物通常位于：

- `dist/OlfactoryPilot/`

## 10. BMAD 相关目录

当前有效的本地 BMAD/Codex 工具目录是：

- `.agents/`
- `_bmad/`
- `_bmad-output/`

长期项目资料应保存在：

- `docs/`
- `docs/sprint-artifacts/`

上一版旧工具安装产物已经清理，不再属于当前有效结构：

- `.bmad/`
- `.codex/`
- `codex.cmd`

如果以后又出现类似目录，需要先确认是否为当前工具链必需，再决定是否保留。

## 11. 当前项目进度

根据当前文档和 sprint 状态：

- Epic 1：安全硬件基础，主体功能完成，部分记录处于真实硬件复核阶段。
- Epic 2：校准与手动控制，主体功能完成。
- Epic 3：协议执行与数据记录，下一阶段优先开发。
- Epic 4：运行维护、清洗与本地化，后续开发。

当前推荐下一 story：

- `3-1-protocol-file-parsing-txtcsv`

目标是实现 `.txt/.csv` 协议文件解析，为后续手动/TTL 触发、呼吸门控、低抖动执行和数据记录打基础。

## 12. 新增文件放置规则

- 新 UI：`app/views/`。
- 新业务编排：`app/controllers/`。
- 新状态对象：`app/models/`。
- 新硬件或业务服务：`app/services/`。
- 新后台线程：`app/workers/`。
- 新配置项：`config/default_config.json`。
- 新测试：`tests/test_*.py`。
- 新工程脚本：`scripts/`。
- 新需求、story、验证和回顾：`docs/` 或 `docs/sprint-artifacts/`。

不要把业务代码、临时脚本、日志、打包产物或实验输出数据散落在项目根目录。

## 13. 常用命令

```powershell
D:\miniconda3\envs\code\python.exe -m pip install -r requirements-dev.txt
D:\miniconda3\envs\code\python.exe -m app.main
D:\miniconda3\envs\code\python.exe -m app.main --simulation
D:\miniconda3\envs\code\python.exe -m pytest
D:\miniconda3\envs\code\python.exe -m ruff check app tests
D:\miniconda3\envs\code\python.exe -m PyInstaller pyinstaller.spec
```

## 14. 推荐下一步 BMAD 流程

建议每个 BMAD 技能使用新的对话窗口，以减少旧上下文干扰。

1. 使用 `bmad-sprint-status` 确认 sprint 状态。
2. 使用 `bmad-create-story` 为 Story 3.1 生成开发用 story。
3. 使用 `bmad-dev-story` 实现 Story 3.1。

每次准备新开窗口前，应让当前窗口更新“下一步应该找哪个智能体、要说什么、目的是什么”的说明。
