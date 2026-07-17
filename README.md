# OlfactoryPilot-CN

OlfactoryPilot-CN 是一个面向嗅觉刺激实验的 Windows 桌面软件，目标是替代原有法国软件 **ProgOlfactoTao**，提供中文界面、可维护的 Python 代码、硬件安全联锁、实验协议执行和数据记录能力。

当前项目使用 **Python 3.11**、PySide6、pyqtgraph、NI-DAQmx、pyserial 和 PyInstaller。代码采用 MVC + Worker + HAL 的组织方式：界面负责显示和交互，控制器负责编排业务逻辑，硬件线程负责安全、低抖动地访问 NI 采集卡和 Alicat 质量流量控制器。

## 目录概览

- `app/`：应用主代码，包含 `controllers/`、`models/`、`views/`、`workers/`、`services/`。
- `config/default_config.json`：仓库内通用默认配置，默认使用 Mock HAL，可在没有真实硬件的电脑上启动。
- `config/local_config.example.json`：本机真实硬件配置模板。
- `docs/`：产品需求、架构、UX、项目结构、开发故事、真实硬件记录和 sprint 状态文档。这些是项目知识，应提交到 Git。
- `scripts/`：本地 CI、sprint 状态生成、Alicat 串口探测等辅助脚本。
- `tests/`：pytest 自动化测试。
- `.github/workflows/ci.yml`：GitHub Actions 持续集成流程。
- `requirements.txt`：运行软件所需依赖。
- `requirements-dev.txt`：开发、测试、代码检查和打包所需依赖。
- `ruff.toml`、`pytest.ini`、`pyinstaller.spec`：代码检查、测试和 Windows 打包配置。

## 环境要求

- Windows 10/11。
- Python 3.11。推荐使用 conda、venv 或系统 Python，但不要在项目文档中固定某一台电脑的解释器绝对路径。
- Git。
- 如需连接真实硬件，需要安装并正确配置 NI-DAQmx 驱动、Alicat 串口设备和对应 COM 端口。

## 安装依赖

先激活你自己的 Python 3.11 环境，然后安装开发依赖：

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

`requirements.txt` 只包含运行软件需要的依赖；`requirements-dev.txt` 会先引用 `requirements.txt`，再额外安装 ruff、pytest、pytest-qt、PyInstaller 等开发工具。

## 配置策略

仓库默认配置和本机硬件配置分开管理：

- `config/default_config.json` 提交到 Git，保存通用界面、安全阈值、阀门映射和模拟模式默认值。
- `config/local_config.json` 不提交到 Git，保存某台电脑自己的真实硬件端口、NI 设备名、Alicat 串口和校准参数。
- `config/local_config.example.json` 提交到 Git，作为真实硬件电脑的参考模板。

在真实硬件电脑上，可以复制模板并按本机情况修改：

```powershell
Copy-Item config/local_config.example.json config/local_config.json
```

默认启动会自动读取 `config/default_config.json`，并在 `config/local_config.json` 存在时叠加本机覆盖配置。其他开发者无需真实硬件也可以直接使用默认 Mock HAL 或传入 `--simulation`。

## 运行软件

```powershell
python -m app.main
```

常用参数：

```powershell
python -m app.main --simulation
python -m app.main --no-worker
python -m app.main --local-config config/local_config.json
```

- `--simulation`：使用 Mock HAL，不连接真实硬件，适合开发、演示和自动化测试。
- `--no-worker`：跳过硬件工作线程，适合快速检查 UI 启动。
- `--local-config`：指定本机覆盖配置路径，适合临时切换不同硬件或端口。

## 质量检查

```powershell
python -m ruff check app tests
python -m pytest
```

本地也可以使用统一脚本：

```powershell
pwsh scripts/run-ci.ps1 lint
pwsh scripts/run-ci.ps1 test
pwsh scripts/run-ci.ps1 build
pwsh scripts/run-ci.ps1 ci
```

`ci` 会依次执行代码检查、测试和 PyInstaller 打包。打包产物位于 `dist/OlfactoryPilot/`。

## BMAD 项目文档

需要同步到 Git 的 BMAD 项目知识已经放在 `docs/` 下，包括 PRD、架构、UX、epics、sprint artifacts、workflow/status 文档和真实硬件验证记录。

`.agents/` 和 `_bmad/` 是本机 BMAD/Codex 工具安装与配置目录，不提交到 Git。`_bmad-output/` 是 BMAD 工作流可能生成的本地输出工作区，不作为本仓库长期资料的权威来源；需要长期保存、协作和追踪的项目资料应整理进 `docs/` 或 `docs/sprint-artifacts/` 后再提交。

在另一台电脑克隆代码后，可以重新安装 BMAD 工具链，例如：

```powershell
npx bmad-method install
```

## 项目进度

当前 Epic/Story 状态只维护在 `docs/sprint-artifacts/sprint-status.yaml`。README 不重复写具体进度，避免状态在多个文档中漂移。
