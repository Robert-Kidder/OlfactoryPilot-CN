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
  config/                 # 通用默认配置和本机配置模板
  docs/                   # 项目文档、需求、架构、UX、story 和参考资料
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

- `app/main.py`：应用入口，持有 Windows 全局 named mutex 以强制单实例，解析启动参数，读取配置，创建 Qt 应用和主窗口。
- `app/controllers/`：控制器层，编排 UI、状态、服务和 Worker。
- `app/models/`：模型层，保存配置、会话、硬件状态和安全状态等结构化数据。
- `app/views/`：界面层，负责 PySide6 组件展示和用户输入。
- `app/services/`：服务层，包含 HAL、硬件自检、安全联锁、阀门、流量、校准等业务逻辑。
- `app/workers/`：后台线程层，负责硬件轮询、状态推送和低抖动执行。

Story 3.5 的会话记录文件：

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

应用启动时按“默认配置 + 本机覆盖”的顺序合并配置。后续如果做“选项”页面，现场或个人机器特有的值应写入本机覆盖配置，通用项目约定才进入 `default_config.json`。

## 5. docs 目录

`docs/` 是项目知识库。

- `docs/project-context.md`：项目上下文，供开发者和 AI 快速理解项目。
- `docs/prd.md`：产品需求文档，说明软件要做什么。
- `docs/architecture.md`：架构文档，说明软件如何组织。
- `docs/ux-design.md`：UX 设计说明，说明界面结构和交互规则。
- `docs/epics.md`：Epic 和 Story 拆分。
- `docs/index.md`：项目文档索引与归档入口。
- `docs/archive/FeatureList-legacy.md`：已停止维护的历史功能清单快照。
- `docs/project-structure.md`：本文档。
- `docs/bmm-workflow-status.yaml`：BMAD 规划阶段状态。
- `docs/sprint-artifacts/`：开发 story、sprint 状态、回顾、验证报告。
- `docs/ALICAT-MANUAL.md`：Alicat 相关说明。
- `docs/ManuelUtilisation_ProgOlfacto.pdf`：原法国软件说明书。

项目文档默认使用简体中文。历史 sprint artifact 中如果包含旧英文或旧事实，应优先在后续相关 story 更新时修正；主线文档必须保持当前准确。

## 6. scripts 目录

- `scripts/run-ci.ps1`：本地执行 `lint`、`test`、`build` 或完整 `ci` 流程。
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

测试的意义是防止后续开发破坏 Epic 1–3 已建立的硬件安全、校准、协议执行和 session 记录能力，并允许无真实硬件的 CI 环境验证核心逻辑。真实 NI 时序结论仍须由 HIL Gate 证明。

## 8. requirements.txt 与 requirements-dev.txt

项目同时保留两个依赖文件是正常做法。

`requirements.txt` 是运行软件所需依赖，包括 PySide6、pyqtgraph、numpy、nidaqmx、pyserial 等。

安装：

```powershell
python -m pip install -r requirements.txt
```

`requirements-dev.txt` 是开发、测试、检查和打包所需依赖。它先引用 `requirements.txt`，再额外安装 pytest、pytest-qt、ruff、PyInstaller 等工具。

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

`.agents/` 和 `_bmad/` 主要是工具安装、技能和本机配置目录。团队共享的 `_bmad/custom/config.toml` 是唯一纳入 Git 的 `_bmad/` 文件，用于把长期 planning/implementation artifacts 路由到 `docs/`。`_bmad-output/` 是临时工作区，不是长期项目资料的权威来源。

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

其他主线文档不重复写具体进度或“下一 story”，避免状态在多个位置漂移。需要判断下一步开发任务时，先读取 sprint 状态文件，再查看对应 `docs/sprint-artifacts/` story。

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
python -m pytest
python -m ruff check .
python -m PyInstaller pyinstaller.spec
```

## 14. 推荐下一步 BMAD 流程

建议每个 BMAD 技能使用新的对话窗口，以减少旧上下文干扰。

1. 使用 `docs/sprint-artifacts/sprint-status.yaml` 或 `bmad-sprint-status` 确认 sprint 状态。
2. 若上一 Epic 复盘要求修改下一 Epic 的边界，先运行 `bmad-correct-course`，再创建下一条 story。
3. 使用 `bmad-create-story` 创建并校验 story；随后在新窗口使用 `bmad-dev-story` 实施。
4. 开发前阅读对应 story、`docs/architecture.md`、`docs/project-context.md` 和相关测试。

每次准备新开窗口前，应让当前窗口更新“下一步应该找哪个智能体、要说什么、目的是什么”的说明。
