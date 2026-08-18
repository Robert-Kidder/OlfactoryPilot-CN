# Story 4.6：方案 B V3 手动实验替换

Status: **review：离线实现、自动化、构建与独立复审已完成，等待用户 Mock 手动验收**

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
| 首轮独立复审整改 | `f5a2959` | 定向 344 项、全仓 928 项 |
| 整改后复审收敛 | `5b2561e` | 定向 283 项、全仓 934 项 |

## 首轮独立复审与整改（2026-08-19）

Blind Hunter 与 Edge Case Hunter 从 baseline 独立审阅完整 diff，确认并整改以下 Story 内问题：

- 当前 HardwareProfile 映射没有贯穿异常/安全关闭；现改为 registry target 优先并与 legacy target 作兼容并集，按物理 target 保留失败证据。
- 保存/回滚后 selector、ValveService、DO adapter、shutdown 与 session receipt consumer 缓存不一致；现采用断开事务内统一重绑，失败显式回滚磁盘与运行时。
- manual receipt 可在 deadline 后成功推进，且已完成的 `flow_zero` timeout 会污染后续阶段；现逐命令保存单调 deadline，并让相关成功回执终结 pending identity。
- 第二次 operation 会继承前次 A=0/selector/supply 证据，且双 start 可同时排队；现每次建立全新 Snapshot，并增加原子 pending-start 门禁。
- 反向 selector 极性下的 odor-route `CLOSE` 可被误当安全动作；现按 route 语义而非 OPEN/CLOSE 字面执行联锁。
- UI 的“开启”状态未扣除 close receipt，供气和 readiness 又存在 Controller/View 第二份状态；现由 owner Snapshot 统一驱动并在页面内显示门禁原因。
- Mock 验证原先只更新 fingerprint；现执行隔离 MockHAL + adapter 的相关 open/close/极性回路，只有匹配 receipt 才标记 `mock_verified`。
- NI target 校验过宽；现只接受支持的 USB-6001 DO 语法与范围。
- 初版规格漏接权威 FR7.1 的 COM、NI device ID 和 Alicat unit ID；现纳入同一 profile/revision/原子保存/回滚事务和设置页，仍不探测硬件。
- rollback 控件、profile 保存后 V3 availability 与文档 whitespace 证据不一致问题一并修正。

清洗页候选 finding 被驳回：权威 PRD FR6 与 UX 明确要求保留清洗页；Story 4.1 的代码资产暂停不等于隐藏产品入口。未发现需要真实硬件才能整改的项目。

## 整改后独立复审

两名独立复审者重新从 baseline 检查完整 diff。确认并完成第二轮 patch：

- selector 危险路线谓词贯穿执行前、写入中和写入后二次联锁，支持身份绑定的 safe-high 安全路线。
- legacy alias 自带其历史物理关闭电平；同 logical valve 的新 target 失败不会被旧 alias 成功掩盖。
- future/late manual receipt、旧 operation flow result和 pending-start 取消分别隔离、fail-closed 或精确释放 lease。
- 供气状态改为 receipt 驱动的开启/关闭/切换中/未知，不再在 A=0 提交时乐观显示关闭。
- HardwareProfile nested connections 为唯一真源；现代显式 alias 冲突拒绝，旧无 revision 配置可单向迁移。
- 保存/回滚采用 prepare → runtime publish → CAS disk commit；补偿失败锁定 configuration-divergence。
- COM/NI/Alicat 标识变化后明确要求重启并阻止同进程旧 HAL 重连；mapping-only 保存不触发该 latch。
- 新 cleaning operation 从当前 ChannelRegistry 重建 target；设置权限变化只刷新 gate，不覆盖未保存草稿。
- 默认安全关闭维持 Story 4.5 已归档的 logical 1→20 顺序；remap 时同一 logical valve 内新 target 优先旧 alias。

“完全相同 duplicate receipt 应幂等忽略”的候选未采纳：当前安全契约将重复硬件证据视为异常并保守进入恢复态。所有采纳项均由确定性离线回归覆盖；未运行真实 HIL。

## 最终离线门禁（独立复审整改后）

| Gate | Result |
|---|---|
| Story 4.6 + owner/flow/配置/清洗定向测试 | `283 passed in 8.09s` |
| Story 4.5 FakeHAL manifest 兼容回归 | `87 passed in 2.66s` |
| 完整 pytest | `934 passed in 42.72s` |
| Ruff | `All checks passed!` |
| `compileall` | 通过 |
| `git diff --check` | 通过；仅 LF→CRLF 工作副本提示 |
| PyInstaller `--noconfirm` | `OlfactoryPilot.exe` 构建成功 |

PyInstaller 报告的 OpenGL、`pkg_resources`、macOS framework 提示来自既有可选依赖扫描，不影响 Windows 构建完成；产物位于忽略目录 `dist/`，未提交。

## 验收状态

- 自动化与构建：已完成。
- 独立代码复审：两轮完成；确认项已整改并通过全量门禁。
- 用户 Mock 手动验收：待独立复审通过后，由 Codex 每次只提供一个启动或操作步骤并等待反馈。
- 真实硬件/HIL：未授权、未执行，不作为当前离线完成证据。
