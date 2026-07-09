# OlfactoryPilot-CN

OlfactoryPilot-CN 是一个面向嗅觉刺激实验的 Windows 桌面软件，目标是替代原有法国软件 **ProgOlfactoTao**，提供中文界面、可维护的 Python 代码、硬件安全联锁、实验协议执行和数据记录能力。

当前项目使用 **Python 3.11**、PySide6、pyqtgraph、NI-DAQmx、pyserial 和 PyInstaller。代码采用 MVC + Worker + HAL 的组织方式：界面负责显示和交互，控制器负责编排业务逻辑，硬件线程负责安全、低抖动地访问 NI 采集卡和 Alicat 质量流量控制器。

## 目录概览

- `app/`：应用主代码，包含 `controllers/`、`models/`、`views/`、`workers/`、`services/`。
- `config/default_config.json`：默认硬件、界面、安全阈值和通道映射配置。
- `docs/`：产品需求、架构、UX、项目结构、开发故事和 sprint 状态文档。
- `scripts/`：本地 CI、sprint 状态生成、Alicat 串口探测等辅助脚本。
- `tests/`：pytest 自动化测试。
- `.github/workflows/ci.yml`：GitHub Actions 持续集成流程。
- `requirements.txt`：运行软件所需依赖。
- `requirements-dev.txt`：开发、测试、代码检查和打包所需依赖。
- `ruff.toml`、`pytest.ini`、`pyinstaller.spec`：代码检查、测试和 Windows 打包配置。

## 环境要求

- Windows 10/11。
- Python 3.11。当前开发环境为 `D:\miniconda3\envs\code\python.exe`。
- Git。
- 如需连接真实硬件，需要安装并正确配置 NI-DAQmx 驱动、Alicat 串口设备和对应 COM 端口。

## 安装依赖

建议直接使用当前 conda 环境中的 Python：

```powershell
D:\miniconda3\envs\code\python.exe -m pip install --upgrade pip
D:\miniconda3\envs\code\python.exe -m pip install -r requirements-dev.txt
```

`requirements.txt` 只包含运行软件需要的依赖；`requirements-dev.txt` 会先引用 `requirements.txt`，再额外安装 ruff、pytest、pytest-qt、PyInstaller 等开发工具。

## 运行软件

```powershell
D:\miniconda3\envs\code\python.exe -m app.main
```

常用参数：

```powershell
D:\miniconda3\envs\code\python.exe -m app.main --simulation
D:\miniconda3\envs\code\python.exe -m app.main --no-worker
```

- `--simulation`：使用 Mock HAL，不连接真实硬件，适合开发、演示和自动化测试。
- `--no-worker`：跳过硬件工作线程，适合快速检查 UI 启动。

## 质量检查

```powershell
D:\miniconda3\envs\code\python.exe -m ruff check app tests
D:\miniconda3\envs\code\python.exe -m pytest
```

本地也可以使用统一脚本：

```powershell
pwsh scripts/run-ci.ps1 lint
pwsh scripts/run-ci.ps1 test
pwsh scripts/run-ci.ps1 build
pwsh scripts/run-ci.ps1 ci
```

`ci` 会依次执行代码检查、测试和 PyInstaller 打包。打包产物位于 `dist/OlfactoryPilot/`。

## 当前开发重点

Epic 1 和 Epic 2 的主体能力已经进入完成或真实硬件复核阶段。下一阶段建议优先推进 **Epic 3：协议执行与数据记录**，先从 Story 3.1 “协议文件解析 .txt/.csv” 开始，把旧软件实验协议导入能力建立起来。
