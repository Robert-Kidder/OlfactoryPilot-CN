# OlfactoryPilot-CN 架构文档

## 1. 架构目标

本项目是 Windows 桌面硬件控制软件，核心目标是安全、稳定、可测试地替代 ProgOlfactoTao 的实验控制能力。架构必须让 UI、业务流程、硬件访问和数据记录彼此解耦，避免所有逻辑堆在界面代码中。

## 2. 技术栈

- Python：3.11。
- GUI：PySide6 6.7.2 + PySide6-Fluent-Widgets / QFluentWidgets；正式窗口基类为 `FluentWindow`。
- 图形显示：pyqtgraph。
- NI 设备：nidaqmx。
- RS232：pyserial。
- 测试：pytest、pytest-qt。
- 代码检查：ruff，目标版本 `py311`。
- 打包：PyInstaller。
- 依赖管理：`requirements.txt` 和 `requirements-dev.txt`。
- CI：GitHub Actions，Windows runner，执行 ruff、pytest 和 PyInstaller 打包。

## 3. 分层结构

```text
app/
  main.py              # 应用入口、命令行参数、QApplication 初始化
  controllers/         # 业务编排、界面状态、安全动作入口
  models/              # 会话、配置、硬件状态、协议等数据结构
  views/               # PySide6 界面组件
  workers/             # 硬件工作线程、低抖动执行逻辑
  services/            # HAL、配置、日志、协议解析等服务
config/
  default_config.json  # 通用默认配置
  local_config.example.json  # 本机覆盖配置模板
docs/                  # 中文需求、架构、UX、story 和项目说明
scripts/               # 本地 CI、硬件探测、状态生成脚本
tests/                 # 自动化测试
```

## 4. 关键设计

### MVC + Worker

- View 只处理显示和用户输入。
- Controller 接收界面事件，调用模型和服务，发出状态更新。
- Worker 在线程中处理硬件读写、协议时序和安全检查，避免阻塞 UI。
- Qt signal/slot 用于 UI 线程和工作线程之间通信。

### 正式产品 UI

- 正式运行树只构造已验收的 QFluentWidgets `FluentWindow` 产品页面；不得 import、隐藏托管或提供旧 UI 兼容入口。未验收能力不预设页面或占位导航。
- 全局使用 Dark Theme 与 `#E2AD50` 主题色，实时曲线继续使用 pyqtgraph。产品组件优先采用 QFluentWidgets 原生 Card、Label、SpinBox、Button、Badge、ToolTip 和 InfoBar。
- 气口选择属于 View draft；真实开启和故障来自 immutable Snapshot。UI `QTimer` 只刷新倒计时和显示，不提交自动关闭动作。

### 低抖动动作与资源所有权

- `HardwareWorker` 独占唯一 AI0/AI6 continuous task，并把带 AI epoch、sample sequence 与采样点 `monotonic_ns` 的 frozen batch 直接提交给 `ActuationWorker`；UI signal 不参与协议 deadline。
- `ActuationWorker` 独占 `ProtocolExecutor`、`GatingService`、动作质量窗口和全部 DO session。协议、手动、预检与安全动作统一进入其 deadline/紧急队列，HAL 成功回执点明确为 `daqmx_write_ack`，不代表机械阀物理完成。
- `FlowWorker` 是 Alicat 串口单写者。Controller 只提交 flow intent；`ActuationWorker` 先检查协议设备租约与 interlock，再把获准命令交给 `FlowWorker`。
- `ActuationInterlockIngress` 是 producer-safe 的 immutable readiness store。AI/telemetry/serial producer 先更新 generation 和 unsafe latch，再发 UI 消息；只有动作 owner 在 readiness 恢复且阀门已确认关闭后才能清除 latch。
- shutdown 的强制安全偏序为：停止新提交与失效 normal epoch → 请求 MFC A 清零并等待匹配成功 receipt → 才允许把 A 路三通选择阀切换到定义的安全路线。气味阀 1–20、B/C、ActuationWorker/DO、HardwareWorker/AI 与 FlowWorker/serial 的其余收敛顺序由 `SafeStopPlan` 明确定义；关键回执失败或状态不确定时进入 `RECOVERY_REQUIRED`。DO owner 未交还时禁止跨线程复用旧 task 做兜底写入。
- RealHAL 按 device/port 建立持久 DO task，deadline 路径只更新端口状态向量并调用 on-demand `Task.write(auto_start=False)`；最终资源分组及 `<20ms` 性能仍必须由真实 Windows/NI HIL 证据确认。

### 执行域隔离

- Protocol、Manual、Maintenance 分别持有明确 lease、command identity/category、receipt 和生命周期。Protocol lease 是 Protocol context 的最强证据。
- Protocol readiness 失效只在 Protocol lease、active Protocol executor state、当前 NORMAL command identity 或 Protocol safe-transition identity 存在时触发 Protocol invalidation。
- 文档已加载、非零 epoch、普通 flow ready、任意非 idle lease、active valve 或 possibly-open 状态都不是充分的 Protocol ownership 证据。Manual/Maintenance 的对应状态不得污染 Protocol epoch、blocked event 或 background safe stop。
- 正常 Manual completion 严格按目标 close receipts → A=0 receipt → selector compensation receipt → 恢复既定供气 receipt → COMPLETED → 精确释放 MANUAL lease；异常 `SafeStopPlan` 是独立 fail-closed 路径，不与正常链混写。

### HAL 硬件抽象

所有硬件访问必须通过 HAL：

- Real HAL：连接真实 NI 设备和 Alicat 串口设备。
- Mock HAL：模拟呼吸信号、阀门状态和流量反馈，用于开发、演示和测试。

新增硬件功能时，应优先扩展 HAL 接口和测试，而不是在界面代码里直接调用驱动。

### 安全策略

- 气流不满足安全条件时，阻止气味阀、A 路三通选择阀和其他危险动作。
- `Dev2/P1.0` 是无独立全关态的 A 路三通选择阀，低电平路由到补偿出口、高电平路由到气味阀 1–20 总入口；不得建模成第 21 只普通阀。
- 退出、停止、异常断连时按 `SafeStopPlan` 收敛，且 A 清零成功 receipt 必须先于三通阀安全路线切换。
- 安全状态必须由 Worker/HAL 层保证，不能只依赖按钮是否可点击。
- 关键安全事件写入日志。

### 协议与数据

协议文件解析、TTL 和呼吸门控底层能力继续保留；当前产品 UI 不暴露这些入口，既有底层契约和证据不得因入口收敛而回滚。相关模块包括：

- 协议模型：保存 trial、timing、valve、trigger、metadata。
- 协议解析服务：负责 `.txt`、`.csv` 解析和错误定位。
- 会话记录服务：负责 `.raw` 信号和 `.log` 事件输出。
- 执行控制器：处理手动触发、TTL 触发、呼吸门控和暂停/停止。

未来 Auto TXT 与当前 parser 的模型不同：每行是 duration、20 个独立 0/1 channel columns、A/B/C 和可忽略尾部 NULL，而当前 parser/model 仍表达 trial/timing/单 valve/trigger。该差异是未来 Auto Build 的显式迁移边界，本轮不把 20 列折叠为 bitmask，也不擅自修改未验收 parser。

Auto external-trigger ingress 与 canonical execution core 必须解耦。USB-6501 ingress 只负责把已确认的 SuperLab/c-pod 8-bit `Trig.In`（包括 `10000001 → 1-based 第128行`）转换为经过范围校验的 trial index；执行仍生成 canonical trial command，经现有 ActuationWorker/FlowWorker/HAL、lease、epoch 和 receipt 链完成。具体 NI MAX alias、port/terminals、DAQmx task、polling/edge/debounce、reconnect、latency 与可靠性待 Auto HIL，不得因此把 USB-6501 的产品角色重新标成未知，也不得加入当前 Manual readiness。

### 会话 bundle 与单写者记录

- Windows GUI 入口以全局 named mutex 强制单实例；mutex 句柄覆盖完整 Qt event loop，并在正常退出或进程崩溃时由操作系统释放。第二实例在创建 Controller/HAL 前显示中文提示并退出，避免跨进程 recovery 误隔离活动 staging，也避免争用 NI/serial owner。
- 每次成功会话发布为 `<output>/<stem>/` 单目录 bundle，包含同 stem 的 `.raw`、`.log` 和 `manifest.json`。活动或失败数据只存在于同父目录 `.<stem>.session.part/` 或 `recovery/`，不得用最终目录冒充完成。
- `SessionFileService` 负责 Windows NFC/非法字符/保留名清洗、240 UTF-16 code unit 路径预算、独占 staging 目录碰撞预留及只读 recovery 验证；staging 创建后先写本程序 ownership marker，使 raw/log/manifest 部分创建失败仍可被可靠识别，同时不把普通用户 `.session.part` 当成本程序数据。View 不生成文件名也不探测磁盘。
- `SessionWriterWorker` 是第四个单写者，只拥有 raw/log/manifest 文件句柄、会话序列、流式 SHA-256 和目录发布状态，不持有 HAL 或任何硬件引用。
- `HardwareWorker` 仍先把原始 `BreathSampleBatch` 交给 `ActuationWorker`，再以 `put_nowait` 直投 writer ingress，最后发 UI signal；`ActuationWorker` 在 owner 线程直投 canonical receipt 与结构化 protocol/quality event。producer 路径不做序列化、flush、fsync、hash 或等待磁盘。
- recorder failure 先锁存 `recording_ready=False` 与 generation，再唤醒动作 owner；NORMAL/MANUAL/PRETEST/WARMUP 被拒绝，SAFETY/emergency close 继续执行。Controller 同时沿既有 `post_stop()` 安全路径收敛。
- 关闭以 Hardware/Actuation/Controller 三个 producer fence 为 barrier。writer 消费 fence 前已接收的最后 batch/event/receipt 后写 `session_closed`，按 raw/log flush→fsync→close、manifest 临时文件 replace、staging 单目录 rename 的顺序发布。
- `manifest.status=complete`、raw/log count/byte/SHA-256 全部验证通过且 JSONL 无空白行的最终目录才显示为完整会话；recovery 的 active-staging 锁只保护登记快照，流式文件验证在锁外执行并逐行响应 cancel。不完整目录只隔离和报告，不自动续写、补全或删除。

## 5. 配置来源

默认配置位于 `config/default_config.json`，作为仓库内通用默认来源提交到 Git。该文件必须能在没有真实硬件的开发电脑上启动，默认使用 Mock HAL。

本机真实硬件、端口和校准参数通过 `config/local_config.json` 覆盖默认配置。该文件不提交到 Git；仓库只提交 `config/local_config.example.json` 作为模板。运行时按“默认配置 + 本机覆盖”的顺序合并，嵌套字典递归合并，因此本机可以只覆盖 `serial_port`、`ni_devices`、`ai0_channel`、`hal_mode`、校准值等差异项。

当前 Manual runtime 的 NI 生产基线为两台 USB-6001：`Dev1` 与 `Dev2`；USB-6501 不属于 Manual 启动自检、connection readiness 或连接成功门禁。未来 Auto ingress 已确定使用 USB-6501 接收 c-pod 的 8-bit `Trig.In`，但其 NI MAX alias、port/line、DAQmx task、readiness 和 HIL 尚未实现；只有 Auto Build 完成物理登记与验证后才能启用，且不改变既有阀门映射。

硬件方案使用 versioned `HardwareProfile` 表达机外气口、内部控制阀位、NI target、显示名称、启用状态、极性和验证指纹。机外气口固定为 1–20；当前初始化映射为机外 2/4/6/8/12/14/16/18 对应内部阀位 2–9，三通选择阀单独建模。通用项目约定优先放入 `default_config.json`；只与某台电脑或某次现场校准有关的值放入本机覆盖配置。硬件配置必须经过 schema/交叉校验、同目录原子替换和显式回滚，不能存入 View 私有状态或 QSettings。

## 6. 测试策略

- 单元测试：协议解析、配置读取、HAL 行为、补偿逻辑。
- 控制器测试：安全联锁、状态转换、异常处理。
- UI 冒烟测试：应用启动、核心页面加载、按钮状态。
- 模拟模式测试：不依赖真实硬件即可运行 CI。

真实硬件验证结果应记录到 sprint artifact 或专门的测试记录中，不应替代自动化测试。

## 7. 当前执行不变量

历史 Epic/Story 决策保存在 `docs/archive/`，不作为当前状态源。以下是从已验收实现中保留的长期不变量。

### SafeStopPlan

- `Dev2/P1.0` 使用 selector 专用模型，不占用气味气口或普通阀身份。
- 全局停止、故障停止和 shutdown 共享同一 `SafeStopPlan`；A 清零 receipt 是 selector 切换的硬前置条件。
- stale/late/conflicting receipt 不推进步骤；失败、超时或不确定状态进入 `RECOVERY_REQUIRED`。
- 保留现有 owner、lease、epoch、receipt、紧急队列和 handoff，不重写 HAL/Worker 拓扑。

### 手动实验执行纵切片

- 使用 Intent → Command → Receipt → immutable Snapshot。View 只保留未提交 draft 和即时视觉反馈，不直接访问 HAL、不持有硬件状态。
- `FlowSetpoints` 以独立 A/B/C 为 authority，派生 `A+B` 只用于展示和已确认的 total-delivery ceiling；旧配置 `flow_limits_sccm.total` 保留原义，不重解释为 B MFC 上限。sample A 用户上限与未来 compensation A-controller `A+C` 上限是不同语义，后者没有证据时不得猜测。
- 手动供气和刺激阶段由 ActuationWorker/协调器持有。baseline/restore 使用直接目标 `A+C/B/C`，stimulus 使用 `A/B/0`，避免 `FlowService` 的 `rest` mode 二次补偿。刺激持续时间从全部目标成功 open receipt 的共同就绪时刻起算，由 monotonic deadline 自动关闭；UI `QTimer` 只刷新倒计时。
- 未来自动实验只能生成相同的 typed phase plan，复用 ActuationWorker、FlowWorker、HAL、lease、epoch 和 receipt，不能模拟 UI 点击。
- QFluentWidgets 手动实验 UI 复用该执行纵切片；正式 runtime 不构造 legacy View。

### 配置、清洗与验证

- HardwareProfile 只来自 `default_config.json + local_config.json`；UI 编辑 candidate，保存需断开安全态、schema/唯一性校验和原子替换。
- 映射或极性变化使相关气口重新待验证；仅修改显示名称保留验证状态。
- 清洗继续保留 `CLEANING`、maintenance lease、`maintenance-v1` bundle、owner deadline 与 recovery 资产，但必须改用 selector、SafeStopPlan 和 HardwareProfile，不再使用 21-target 终态。
- 跨 owner 交错使用 fake clock、Event/Barrier、cancellation token、fake filesystem 和 fault injection；禁止 sleep-only 竞态断言。
- 修改 selector、SafeStopPlan、ActuationWorker/FlowWorker、NI/serial、deadline、映射或 shutdown 时执行范围触发式真实 Windows/NI HIL；Mock 和 `daqmx_write_ack` 不能替代机械/出口证据。
