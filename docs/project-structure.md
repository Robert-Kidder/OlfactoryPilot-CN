# OlfactoryPilot-CN 项目结构说明

本文档用于帮助新接手开发者理解当前代码仓库的文件组织、工程工具链、BMAD 资料位置和后续开发规则。后续如果目录结构变化，应同步更新本文档。

## 1. 项目目标

OlfactoryPilot-CN 是 Windows 桌面嗅觉刺激实验控制软件，目标是替代原有法国软件 ProgOlfactoTao。项目使用 Python 3.11、PySide6、NI-DAQmx 和 pyserial。当前正式 runtime 只构造手动实验与设置；Auto/Breath、清洗、校准和协议相关模块作为未来或兼容回归资产保留，不代表当前导航已交付。

原法国软件说明书保存在：

- `docs/ManuelUtilisation_ProgOlfacto.pdf`

后续功能对齐和行为确认可以参考该 PDF，但当前项目文档以本仓库 `docs/` 下的中文文档为准。

## 2. 顶层目录

```text
OlfactoryPilot-CN/
  app/                    # 应用主代码
  config/                 # 通用默认配置和本机配置模板
  docs/                   # 当前权威、状态、证据、活动工件与历史归档
  scripts/                # 工程脚本和辅助工具
  tests/                  # 自动化测试
  .github/workflows/      # GitHub Actions 持续集成配置
  .agents/                # 当前 Codex/BMAD 技能文件，本机工具目录，不提交
  _bmad/                  # BMAD 本地安装；仅 custom/config.toml 作为团队路由配置提交
  _bmad-output/           # BMAD 本地输出工作区，不提交；长期资料整理进 docs/
  logs/                   # 运行日志，本地生成目录
```

约定：

- 业务代码只放在 `app/`。
- 可配置参数放在 `config/`，不要硬编码在 UI 或控制器里。
- 当前权威、活动工件、状态、证据和历史资料按 `docs/index.md` 的层级放置。
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

- `app/main.py`：应用入口，持有 Windows 全局 named mutex 以强制单实例，解析启动参数，读取配置，创建 Qt 应用和主窗口。
- `app/controllers/`：控制器层，编排 UI、状态、服务和 Worker。
- `app/models/`：模型层，保存配置、会话、硬件状态和安全状态等结构化数据。
- `app/views/`：界面层，负责 PySide6 组件展示和用户输入。
- `app/services/`：服务层，包含 HAL、硬件自检、安全联锁、阀门、流量、校准等业务逻辑。
- `app/workers/`：后台线程层，负责硬件轮询、状态推送和低抖动执行。

会话记录文件：

- `app/models/session.py`：不可变 session descriptor/path/envelope/fence、受控状态机和文件页 snapshot。
- `app/services/session_file_service.py`：Windows 命名、staging bundle 预留、ownership marker、可取消的流式 manifest/bundle 验证和 recovery quarantine。
- `app/workers/session_writer.py`：唯一 session 文件句柄 owner、有界 ingress、raw/JSONL 写入、fence barrier 与单目录发布。
- `app/views/session_view.py`：中文“文件”页，只发布 subject/condition/output/start/end/recovery 意图。

原则：

- View 尽量被动，不直接写复杂业务逻辑。
- Controller 负责流程编排，但不直接访问底层硬件驱动。
- 硬件访问必须通过 HAL。
- 真实硬件和 Mock HAL 应尽量共享同一接口。

## 4. config 目录

仓库默认配置和本机硬件配置分开管理：

- `config/default_config.json`：提交到 Git，保存通用界面、安全阈值、阀门映射、协议门控、Alicat 参数和 Mock HAL 默认值。该文件必须能在没有真实硬件的开发电脑上启动。
- `config/local_config.example.json`：提交到 Git，作为真实硬件电脑的本机覆盖配置模板。
- `config/local_config.json`：不提交到 Git，用于保存某台电脑自己的真实 COM 端口、NI 设备名、Alicat 配置和现场校准值。

源码启动时按仓库内“`config/default_config.json` + `config/local_config.json`”的顺序合并配置。打包程序不写安装目录：首次启动在 `%USERPROFILE%/.olfactorypilot/default_config.json` 创建可写配置，后续从该位置读取。现场或个人机器特有的值写入对应可写配置，通用项目约定才进入仓库的 `default_config.json`；这条边界不依赖未来页面设计。

## 5. docs 目录

`docs/` 是项目知识库。

- `docs/project-context.md`：项目上下文，供开发者和 AI 快速理解项目。
- `docs/prd.md`：产品需求文档，说明软件要做什么。
- `docs/architecture.md`：架构文档，说明软件如何组织。
- `docs/ux-design.md`：UX 设计说明，说明界面结构和交互规则。
- `docs/epics.md`：产品拆分历史与需求映射，不维护当前进度。
- `docs/index.md`：项目文档索引与归档入口。
- `docs/archive/`：不再维护但需审计的旧 Story、评审、复盘、规划状态和历史资料。
- `docs/project-structure.md`：本文档。
- `docs/sprint-artifacts/sprint-status.yaml`：唯一动态 Epic/Story 状态源。
- `docs/sprint-artifacts/spec-*.md`：活动 execution spec；已完成工件转入归档。
- `docs/sprint-artifacts/evidence/`：安全、HIL、极性和发布证据。
- `docs/ALICAT-MANUAL.md`：Alicat 相关说明。
- `docs/ManuelUtilisation_ProgOlfacto.pdf`：原法国软件说明书。

项目文档默认使用简体中文。历史资料不原地改写结论；当前权威文档必须保持准确，并在必要时显式指出被取代的历史假设。

## 6. scripts 目录

- `scripts/run-ci.ps1`：本地执行 `lint`、`test-fast`、`test`、`build` 或完整 `ci` 流程，并传播 Python 原生退出码。
- `scripts/probe_alicat.py`：Alicat 串口设备探测辅助脚本。
- `scripts/hil_actuation_benchmark.py`：真实 NI HIL 动作时延与抖动基准脚本。

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
python -m pytest
```

测试用于防止后续开发破坏硬件安全、执行域隔离、校准、协议执行和 session 记录能力，并允许无真实硬件的 CI 环境验证核心逻辑。真实 NI 时序结论仍须由 HIL Gate 证明。

## 8. requirements.txt 与 requirements-dev.txt

项目同时保留两个依赖文件是正常做法。

`requirements.txt` 是运行软件所需依赖，包括 PySide6、pyqtgraph、numpy、nidaqmx、pyserial 等。

安装：

```powershell
python -m pip install -r requirements.txt
```

`requirements-dev.txt` 是开发、测试、检查和打包所需依赖。它先引用 `requirements.txt`，再额外安装 pytest、ruff、PyInstaller 等工具；Qt 测试使用项目 fixture 与 `PySide6.QtTest`，当前未声明 pytest-qt。

安装：

```powershell
python -m pip install -r requirements-dev.txt
```

简单理解：只运行软件用 `requirements.txt`；参与开发用 `requirements-dev.txt`。

## 9. pytest、ruff、CI 和 PyInstaller

### pytest

pytest 是 Python 测试框架，配置文件是 `pytest.ini`。

运行：

```powershell
python -m pytest
```

### ruff

ruff 是代码检查工具，配置文件是 `ruff.toml`。

当前关键配置：

- Python 目标版本：`py311`。
- 单行长度：`120`。
- 检查规则：基础错误、导入排序、现代 Python 写法和常见 bug 风险。

运行：

```powershell
python -m ruff check .
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
python -m PyInstaller pyinstaller.spec
```

产物通常位于：

- `dist/OlfactoryPilot/`

## 10. BMAD 相关目录

当前有效的本地 BMAD/Codex 工具与输出目录是：

- `.agents/`
- `_bmad/`
- `_bmad-output/`

`.agents/` 和 `_bmad/` 主要是工具安装、技能和本机配置目录。团队共享的 `_bmad/custom/config.toml` 是唯一纳入 Git 的 `_bmad/` 文件，用于把长期 planning/implementation artifacts 路由到 `docs/`。`_bmad-output/` 是临时工作区，不是长期项目资料的权威来源。当前工具基线是 BMAD Method 6.12，实施入口为 `bmad-build`；Node/BMAD 均不属于产品 runtime 或 CI 依赖。

全新 clone 如需该可选工作流，可另行执行：

```powershell
npx bmad-method@6.12.0 install
```

若 Windows PowerShell 的 execution policy 拦截 `npx.ps1`，改用 `npx.cmd bmad-method@6.12.0 install`；安装内容仍是固定的 BMAD 6.12.0 工具链。Node/BMAD 始终是可选开发工具，不参与产品 runtime 或 CI。

长期项目资料应整理并保存在：

- `docs/`
- `docs/sprint-artifacts/`

上一版旧工具安装产物已经清理，不再属于当前有效结构：

- `.bmad/`
- `.codex/`
- `codex.cmd`

如果以后又出现类似目录，需要先确认是否为当前工具链必需，再决定是否保留。

## 11. 项目进度来源

当前 Epic/Story 状态只维护在：

- `docs/sprint-artifacts/sprint-status.yaml`

其他主线文档不重复写具体进度或推荐实施顺序，避免状态在多个位置漂移。需要判断当前工作时，先读取 sprint 状态文件，再查看它指向的活动工件。

## 12. 新增文件放置规则

- 新 UI：`app/views/`。
- 新业务编排：`app/controllers/`。
- 新状态对象：`app/models/`。
- 新硬件或业务服务：`app/services/`。
- 新后台线程：`app/workers/`。
- 动作命令/回执模型：`app/models/actuation.py`；动作统计与 HAL adapter：`app/services/actuation_metrics.py`、`app/services/actuation_do_adapter.py`。
- 单写者线程：`app/workers/actuation_worker.py`（DO/协议状态）、`app/workers/flow_worker.py`（serial/MFC）与 `app/workers/session_writer.py`（raw/log/manifest）；`hardware_worker.py` 只负责 AI producer。
- 会话成功输出：用户选择目录下的 `<stem>/<stem>.raw`、`<stem>/<stem>.log`、`<stem>/manifest.json`；活动/失败输出保留在 `.<stem>.session.part/` 或 `recovery/`。
- 新通用配置项：`config/default_config.json`。
- 新本机硬件/端口/校准覆盖：`config/local_config.json`，并视需要同步更新 `config/local_config.example.json`。
- 新测试：`tests/test_*.py`。
- 新工程脚本：`scripts/`。
- 新需求、story、验证和回顾：`docs/` 或 `docs/sprint-artifacts/`。

不要把业务代码、临时脚本、日志、打包产物或实验输出数据散落在项目根目录。

## 13. 常用命令

```powershell
python -m pip install -r requirements-dev.txt
python -m app.main
python -m app.main --simulation
python -m pytest -m "not slow"
python -m pytest
python -m ruff check .
python -m PyInstaller pyinstaller.spec
```

也可以使用 `scripts/run-ci.ps1 test-fast|test|lint|build|ci`。PyInstaller 只携带默认配置与本地帮助 PDF，不携带内部 `docs/` 知识库。

## 14. 文档维护门禁

- 当前事实只写入权威层；动态进度只写入 `sprint-status.yaml`。
- 旧 Story、评审和复盘完成后转入 `docs/archive/`，不要继续占用活动工件目录。
- 安全/HIL/极性/发布证据保留在 evidence 层。删除任何资料前先做全仓引用与独有事实检查；不确定时归档而不是删除。
