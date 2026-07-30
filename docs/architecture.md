# OlfactoryPilot-CN 架构文档

## 1. 架构目标

本项目是 Windows 桌面硬件控制软件，核心目标是安全、稳定、可测试地替代 ProgOlfactoTao 的实验控制能力。架构必须让 UI、业务流程、硬件访问和数据记录彼此解耦，避免所有逻辑堆在界面代码中。

## 2. 技术栈

- Python：3.11。
- GUI：PySide6。
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

### 低抖动动作与资源所有权

- `HardwareWorker` 独占唯一 AI0/AI6 continuous task，并把带 AI epoch、sample sequence 与采样点 `monotonic_ns` 的 frozen batch 直接提交给 `ActuationWorker`；UI signal 不参与协议 deadline。
- `ActuationWorker` 独占 `ProtocolExecutor`、`GatingService`、动作质量窗口和全部 DO session。协议、手动、预检与安全动作统一进入其 deadline/紧急队列，HAL 成功回执点明确为 `daqmx_write_ack`，不代表机械阀物理完成。
- `FlowWorker` 是 Alicat 串口单写者。Controller 只提交 flow intent；`ActuationWorker` 先检查协议设备租约与 interlock，再把获准命令交给 `FlowWorker`。
- `ActuationInterlockIngress` 是 producer-safe 的 immutable readiness store。AI/telemetry/serial producer 先更新 generation 和 unsafe latch，再发 UI 消息；只有动作 owner 在 readiness 恢复且阀门已确认关闭后才能清除 latch。
- shutdown 固定为：停止新提交与失效 normal epoch → `emergency_close_all` 有界确认 → ActuationWorker 停止并释放 DO → HardwareWorker 在线程内释放 AI → FlowWorker 最后释放 serial。DO owner 未交还时禁止跨线程复用旧 task 做兜底关闭。
- RealHAL 按 device/port 建立持久 DO task，deadline 路径只更新端口状态向量并调用 on-demand `Task.write(auto_start=False)`；最终资源分组及 `<20ms` 性能仍必须由真实 Windows/NI HIL 证据确认。

### HAL 硬件抽象

所有硬件访问必须通过 HAL：

- Real HAL：连接真实 NI 设备和 Alicat 串口设备。
- Mock HAL：模拟呼吸信号、阀门状态和流量反馈，用于开发、演示和测试。

新增硬件功能时，应优先扩展 HAL 接口和测试，而不是在界面代码里直接调用驱动。

### 安全策略

- 气流低于阈值时，阻止阀门、主阀和加热器动作。
- 退出、停止、异常断连时关闭所有阀门。
- 安全状态必须由 Worker/HAL 层保证，不能只依赖按钮是否可点击。
- 关键安全事件写入日志。

### 协议与数据

协议文件解析与执行能力已由 Epic 3 建立；后续变更仍以 `docs/sprint-artifacts/sprint-status.yaml` 为状态依据。相关模块包括：

- 协议模型：保存 trial、timing、valve、trigger、metadata。
- 协议解析服务：负责 `.txt`、`.csv` 解析和错误定位。
- 会话记录服务：负责 `.raw` 信号和 `.log` 事件输出。
- 执行控制器：处理手动触发、TTL 触发、呼吸门控和暂停/停止。

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

当前实验台的 NI 生产基线为两台 USB-6001：`Dev1` 与 `Dev2`。现场未安装 USB-6501，`Dev3` 不属于当前启动自检或 Story 3.5 HIL 的必需设备。硬件清单以设备铭牌与 NI MAX/NI-DAQmx 在线枚举共同确认；若日后新增扩展设备，只在对应电脑的本机覆盖配置中显式登记，不据此改变既有阀门映射。

硬件通道映射、主阀线路、阈值、默认流量和界面文字应保持清晰可追踪。通用项目约定优先放入 `default_config.json`；只与某台电脑或某次现场校准有关的值必须放入本机覆盖配置，避免 Git 同步互相覆盖。

## 6. 测试策略

- 单元测试：协议解析、配置读取、HAL 行为、补偿逻辑。
- 控制器测试：安全联锁、状态转换、异常处理。
- UI 冒烟测试：应用启动、核心页面加载、按钮状态。
- 模拟模式测试：不依赖真实硬件即可运行 CI。

真实硬件验证结果应记录到 sprint artifact 或专门的测试记录中，不应替代自动化测试。
