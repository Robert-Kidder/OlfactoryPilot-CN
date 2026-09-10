# OlfactoryPilot-CN

## 项目简介

OlfactoryPilot-CN 是面向嗅觉刺激实验的 Windows 桌面控制软件，提供中文操作界面、实验设备控制、安全联锁、协议执行和数据记录能力。项目使用 Python 3.11、PySide6、NI-DAQmx 与 pyserial，并可通过 PyInstaller 打包交付。

## 主要能力

- 控制 NI 数据采集与数字输出设备、Alicat 质量流量控制器和气味阀矩阵。
- 配置实验台连接、气口映射和安全参数。
- 提供可复用的实验协议编排与信号、事件记录基础。
- 通过 Worker 与 HAL 隔离界面、业务流程和硬件访问。
- 提供 Mock HAL simulation，用于无真实硬件的开发、演示和自动化测试。

## 环境要求

- Windows 10/11。
- Python 3.11。
- Git。
- 真实硬件运行另需 NI-DAQmx 驱动、可用串口及实验台授权配置。

普通 simulation、开发检查和打包不需要 Node、BMAD 或真实硬件。

## 安装

在 Python 3.11 环境中安装运行依赖：

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

参与开发时安装开发依赖：

```powershell
python -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` 在运行依赖之外提供 Ruff、pytest 和 PyInstaller。Qt 测试使用项目自带 fixture 与 `PySide6.QtTest`。

## 运行

```powershell
python -m app.main
```

常用开发参数：

```powershell
python -m app.main --simulation
python -m app.main --no-worker
python -m app.main --local-config config/local_config.json
```

- `--simulation` 使用 Mock HAL，不连接真实硬件。
- `--no-worker` 跳过硬件工作线程，适合快速检查界面构造。
- `--local-config` 指定本机覆盖配置。

## 开发与测试

```powershell
python -m ruff check .
python -m pytest
```

统一开发入口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 lint
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 test-fast
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 test
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 build
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 ci
```

`test-fast` 排除经 duration 审计标记的慢速 Controller/UI 集成层；`test` 和 `ci` 运行完整测试集。`ci` 还执行 PyInstaller，产物位于 `dist/OlfactoryPilot/`。

pytest、CI、截图和 clean-clone 的临时工作区统一位于 `.devtmp/<purpose>/run-<uuid>/`，由 `scripts/dev_temp.py` 创建和清理，不提交到 Git。中断遗留只会在 ownership marker 有效且 owner 已确认失活后清理；未知目录只报告并保留。需要长期保存的诊断资料应显式迁移到 `docs/sprint-artifacts/evidence/`。

从当前已提交版本执行 tracked-only 全新环境验证：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-clean-clone.ps1
```

该脚本在一个 owned session 内创建 clean clone 与 venv，并执行依赖安装、Ruff、完整 pytest、PyInstaller 和 offscreen simulation smoke，结束后清理 session。

BMAD 是可选开发工具，不属于 runtime 或 CI 依赖。需要时使用当前 stable 版本安装：

```powershell
npx bmad-method install
```

Windows PowerShell 若拦截 `npx.ps1`，可使用 `npx.cmd bmad-method install`。项目实施入口为 `bmad-build`。

## 真实硬件配置

真实硬件环境请从模板创建本机配置：

```powershell
Copy-Item config/local_config.example.json config/local_config.json
```

随后按照实验台实际情况配置 NI 设备、串口、Alicat 和相关安全参数。仓库默认配置仅用于开发和测试，不要直接作为实验台配置。详细字段、映射约束和真实硬件门禁见项目文档与对应 HIL runbook。

## 项目文档

- `docs/project-context.md`：技术基线与长期协作规则。
- `docs/prd.md`：产品需求与领域约束。
- `docs/architecture.md`：系统架构与安全边界。
- `docs/ux-design.md`：产品 UI 与交互规则。
- `docs/project-structure.md`：仓库结构与开发入口。
- `docs/sprint-artifacts/sprint-status.yaml`：动态 Epic/Story 状态。
- `docs/sprint-artifacts/evidence/`：安全、HIL、极性、诊断和发布证据。
- `docs/archive/`：保留用于审计的历史资料。
