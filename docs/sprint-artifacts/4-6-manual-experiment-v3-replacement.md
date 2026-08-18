# Story 4.6：方案 B V3 手动实验替换

Status: **in-progress：离线实现与自动化门禁已完成，等待独立复审和用户 Mock 手动验收**

Implementation date: 2026-08-18  
Baseline commit: `5625aa55bea6b916823a1cb676a560b8d5429b61`

## 实施范围

- 新增持久化 `HardwareProfile`、`ChannelRegistry` 与 `FlowSetpoints`，固定呈现机外气口 1–20。
- 默认启用并在 Mock 范围验证 2/4/6/8/12/14/16/18，映射到内部阀位 2–9；selector `Dev2/P1.0` 保持独立。
- 新增方案 B V3 手动实验页和硬件设置页；产品导航不再显示旧预检、呼吸、校准或自动协议入口。
- 手动动作由 Actuation owner 持有：flow receipt → selector odor → 多口 open receipt cohort → 共同 deadline → close → A=0 receipt → selector compensation → 恢复供气。
- UI 只发布 typed intent、渲染 immutable snapshot；QTimer 仅刷新倒计时显示，不负责结束硬件动作。
- 配置保存要求断开、安全终态、owner handoff 和 `CONFIG_CHANGE` lease；使用同目录原子替换、revision 冲突检测与显式 last-known-good 回滚。
- 旧 `PreTestView` 当前保留为不可达兼容代码。删除须在 Story 4.6 适用真实 HIL 另行授权并通过后实施。

## 安全与授权边界

- 本轮全部开发、测试和构建均为离线/Mock；未启动真实模式，未连接或操作 NI、Alicat、串口、真实 DO 或 HIL。
- Controller 在真实模式下明确拒绝 V3 手动动作；Mock 验证只产生 `mock_verified`，不冒充物理气路验证。
- compensation 机械出口映射仍未由 Story 4.5 的电子回执证明。真实 selector/气口验证必须由用户另行授权并按现场步骤执行。
- 未 push。

## 阶段提交

| 阶段 | 本地提交 | 验证 |
|---|---|---|
| 实施规划 | `12d9929` | 权威上下文与范围冻结 |
| 领域模型与配置事务 | `de49df6` | 定向 97 项、全仓 841 项 |
| 手动实验 owner core | `4311b66` | 定向/相关 188 项、全仓 867 项 |
| V3、设置页与 Controller 接线 | `cb4a0ab` | 定向 161 项、全仓 894 项 |

## 最终离线门禁（复审前）

| Gate | Result |
|---|---|
| Story 4.6 + owner/flow 定向测试 | `161 passed in 2.35s` |
| 完整 pytest | `894 passed in 38.70s` |
| Ruff | `All checks passed!` |
| `compileall` | 通过 |
| `git diff --check` | 通过；仅 LF→CRLF 工作副本提示 |
| PyInstaller | `OlfactoryPilot.exe` 构建成功 |

PyInstaller 报告的 OpenGL、`pkg_resources`、macOS framework 提示来自既有可选依赖扫描，不影响 Windows 构建完成；产物位于忽略目录 `dist/`，未提交。

## 验收状态

- 自动化与构建：已完成。
- 独立代码复审：待执行；发现项整改后更新本文件。
- 用户 Mock 手动验收：待独立复审通过后，由 Codex 每次只提供一个启动或操作步骤并等待反馈。
- 真实硬件/HIL：未授权、未执行，不作为当前离线完成证据。
