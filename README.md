# OlfactoryPilot 框架

PySide6/MVC 占位脚手架，预留硬件线程与安全接口，并统一 lint/测试/打包链路。

## 目录
- `app/`：MVC 代码骨架（`main.py`、controllers/models/views/workers/services`）。
- `config/default_config.json`：语言、窗口标题、日志级别与安全阈值占位。
- `tests/`：PySide6 最小化冒烟测试。
- `scripts/run-ci.ps1`：本地与 CI 共用的 lint/pytest/pyinstaller 入口。
- `pyinstaller.spec`：Windows onedir 打包配置，包含默认配置文件。

## 环境要求
- Windows 10/11，Python 3.10+（推荐 3.11）。
- GitHub Actions 使用 `windows-latest`，与本地环境一致。

## 安装依赖
```powershell
python -m venv .venv
.\.venv\Scripts\Activate
pip install --upgrade pip
pip install -r requirements-dev.txt
```
硬件相关依赖（`nidaqmx`、`pyserial`）已包含，后续可按需分离 extras。

## 运行应用
```powershell
python -m app.main                   # 默认读取 config/default_config.json
python -m app.main --no-worker       # 跳过占位硬件线程（CI/测试用）
```

## 工具链
- Lint：`python -m ruff check app tests`
- 测试：`python -m pytest`（自动设置 `QT_QPA_PLATFORM=offscreen`）
- 打包：`python -m PyInstaller pyinstaller.spec`，产物位于 `dist/OlfactoryPilot/`
- 一键：`pwsh scripts/run-ci.ps1 lint|test|build|ci`（build 步骤打印前 5 大文件与 exe 的 size/hash）

## CI（GitHub Actions）
- 触发：push/PR。
- 流程：安装 `requirements-dev.txt` -> `ruff` -> `pytest` -> `pyinstaller`。
- 产出：上传 `dist/OlfactoryPilot` 目录，日志打印前 5 个最大文件以便追踪体积/路径。
