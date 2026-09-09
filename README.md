# OlfactoryPilot-CN

OlfactoryPilot-CN 是一个面向嗅觉刺激实验的 Windows 桌面软件，目标是替代原有法国软件 **ProgOlfactoTao**，提供中文界面、可维护的 Python 代码、硬件安全联锁、实验协议执行和数据记录能力。

当前正式运行界面只包含“手动实验”和“设置”；自动实验与呼吸触发仍是未来能力，不存在隐藏页面或占位入口。协议执行、呼吸门控、清洗和校准的可复用服务与安全回归继续保留，但不等于这些能力已经进入当前产品导航。

当前项目使用 **Python 3.11**、PySide6、pyqtgraph、NI-DAQmx、pyserial 和 PyInstaller。代码采用 MVC + Worker + HAL 的组织方式：界面负责显示和交互，控制器负责编排业务逻辑，硬件线程负责安全、低抖动地访问 NI 采集卡和 Alicat 质量流量控制器。

动作执行分为 Protocol、Manual 和 Maintenance 三个互斥域。每个域只消费归属于自己的 lease、command、receipt 和状态证据；一个域的就绪变化不得失效另一个域。所有真实写入仍由 Worker/HAL 单写者完成，UI 不参与 deadline 或安全判定。

## 目录概览

- `app/`：应用主代码，包含 `controllers/`、`models/`、`views/`、`workers/`、`services/`。
- `config/default_config.json`：仓库内通用默认配置，默认使用 Mock HAL，可在没有真实硬件的电脑上启动。
- `config/local_config.example.json`：本机真实硬件配置模板。
- `docs/`：当前权威文档、活动执行工件、状态、证据和历史归档；层级与入口见 `docs/index.md`。
- `scripts/`：本地 CI、NI HIL 基准和 Alicat 串口探测等辅助脚本。
- `tests/`：pytest 自动化测试。
- `.github/workflows/ci.yml`：GitHub Actions 持续集成流程。
- `requirements.txt`：运行软件所需依赖。
- `requirements-dev.txt`：开发、测试、代码检查和打包所需依赖。
- `ruff.toml`、`pytest.ini`、`pyinstaller.spec`：代码检查、测试和 Windows 打包配置。

## 环境要求

- Windows 10/11。
- Python 3.11。推荐使用 conda、venv 或系统 Python，但不要在项目文档中固定某一台电脑的解释器绝对路径。
- Git。
- 普通模拟运行、开发检查和打包不需要 Node、BMAD 或真实硬件。
- 只有经实验室授权的真实硬件工作才需要 NI-DAQmx 驱动、Alicat 串口设备、本机配置与对应 HIL runbook；不要用开发命令直接驱动 NI、Alicat 或阀门。

## 安装依赖

先激活你自己的 Python 3.11 环境，然后安装开发依赖：

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

`requirements.txt` 只包含运行软件需要的依赖；`requirements-dev.txt` 会先引用 `requirements.txt`，再额外安装 ruff、pytest 和 PyInstaller。Qt 测试使用仓库自带 fixture 与 `PySide6.QtTest`，当前不依赖 pytest-qt。

## 配置策略

仓库默认配置和本机硬件配置分开管理：

- `config/default_config.json` 提交到 Git，保存通用界面、安全阈值、阀门映射和模拟模式默认值。
- `config/local_config.json` 不提交到 Git，保存某台电脑自己的真实硬件端口、NI 设备名、Alicat 串口和校准参数。
- `config/local_config.example.json` 提交到 Git，作为真实硬件电脑的参考模板。

在真实硬件电脑上，可以复制模板并按本机情况修改：

```powershell
Copy-Item config/local_config.example.json config/local_config.json
```

模板保持 `hal_mode: mock`，其余值只是可解析的安全占位值，不能直接用于现场。真实硬件启动前，必须把顶层与 `hardware_profile.connections` 内的两个 `serial_port`（占位值 `COM256`）同步替换为本机 COM 端口；若现场 NI alias 或 Alicat unit ID 不同，也要同步替换两层的 `ni_devices` / `alicat_unit_ids`，并相应更新 `ai0_channel`、`ttl_input_channel` 和线路 target。`signal_offset`、`signal_gain`、`cleaning` 与 `hardware_profile.verification_config` 的数值必须由现场校准和已批准的安全参数替换；全部检查完成后才可由获授权人员把 `hal_mode` 改为 `real`。

源码启动会读取仓库内的 `config/default_config.json`，并在 `config/local_config.json` 存在时叠加本机覆盖配置。打包程序不写安装目录：首次启动会在 `%USERPROFILE%/.olfactorypilot/default_config.json` 创建可写配置，后续从该位置读取。其他开发者无需真实硬件也可以直接使用默认 Mock HAL 或传入 `--simulation`。

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
python -m ruff check .
python -m pytest
```

本地也可以使用统一脚本：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 lint
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 test-fast
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 test
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 build
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 ci
```

`test-fast` 排除 duration 审计标记的 `slow` Controller/UI 集成层，适合作为快速开发反馈；`test` 和 `ci` 仍运行完整测试集。任一 Python 子命令失败时脚本立即返回其非零退出码。`ci` 随后执行 PyInstaller，打包产物位于 `dist/OlfactoryPilot/`，只携带默认配置与本地帮助 PDF，不携带内部 `docs/` 知识库。

## 项目文档

`docs/project-context.md`、`docs/prd.md`、`docs/architecture.md`、`docs/ux-design.md` 和 `docs/project-structure.md` 描述当前长期规则；`docs/sprint-artifacts/sprint-status.yaml` 是唯一动态 Epic/Story 状态源；`docs/sprint-artifacts/evidence/` 保存安全、HIL、极性和发布证据；`docs/archive/` 保存不再维护但仍需审计的历史资料。历史 Story 不覆盖当前权威文档。

`.agents/` 和 `_bmad/` 主要是本机 BMAD/Codex 工具安装目录；团队共享的 `_bmad/custom/config.toml` 把长期规划与实施工件路由到仓库内的 `docs/`。`_bmad-output/` 只是本地临时工作区，权威文档、状态或证据不得依赖其中的文件。项目当前工作流基线为 BMAD Method 6.12，实施入口为 `bmad-build`；它是可选开发工具，不属于产品 runtime 或 CI 依赖。

在另一台电脑克隆代码后，可以重新安装 BMAD 工具链，例如：

```powershell
npx bmad-method@6.12.0 install
```

若 Windows PowerShell 的 execution policy 拦截 `npx.ps1`，使用等价入口 `npx.cmd bmad-method@6.12.0 install`。安装完成后可继续使用 `bmad-build`；不安装 Node/BMAD 时，simulation、ruff、pytest 和 PyInstaller 仍应独立工作。
