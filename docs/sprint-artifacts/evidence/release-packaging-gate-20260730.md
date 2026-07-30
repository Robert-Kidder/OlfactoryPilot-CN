# Windows PyInstaller 打包干跑证据 — 2026-07-30

Status: PASS

## 环境

- 平台：Windows 10（build 26200）
- Python：3.11.15（conda）
- PyInstaller：6.3.0
- 规格：`pyinstaller.spec`
- 命令：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 build`

## 结果

- PyInstaller `Analysis`、`PYZ`、`PKG`、`EXE` 和 `COLLECT` 全部成功。
- 单文件产物：`dist/OlfactoryPilot.exe`
- 大小：68,757,834 bytes（65.57 MiB）
- SHA-256：`28E7FF1AD5AE832C7A53B3C363D4EA2040CFBDF697A1B4D8723C31CA7B3C1361`
- 单文件 `OlfactoryPilot.exe --help`：exit 0。
- onedir `dist/OlfactoryPilot/OlfactoryPilot.exe --help`：exit 0。

## 资源审计

以下 onedir 资源存在且可读：

- `_internal/config/default_config.json`
- `_internal/docs/index.md`
- `_internal/docs/project-context.md`

构建警告已审阅：OpenGL、POSIX/macOS 条件模块、可选 gRPC/pkg_resources 等均不在当前 Windows/PySide6 核心运行路径；两种产物的 CLI 启动烟测均成功。真实 NI/RS232 行为仍由独立 HIL Gate 证明，打包烟测不替代硬件验收。

## 结论

本次实验室发布候选前的 PyInstaller 干跑、核心资源检查、产物哈希和启动烟测通过。`build/`、`dist/` 为可再生产物，不提交 Git。
