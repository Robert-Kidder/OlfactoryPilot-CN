# OlfactoryPilot-CN 项目上下文

## 项目定位

OlfactoryPilot-CN 是用于嗅觉刺激实验的 Windows 桌面控制软件，目标是替代原有法国软件 **ProgOlfactoTao**。项目重点不是做通用数据分析平台，而是为本地实验室提供稳定、中文、可维护、可打包交付的实验控制工具。

软件需要控制 NI USB 采集/数字输出设备、Alicat RS232 质量流量控制器、气味阀矩阵、A 路三通选择阀、呼吸信号采集、TTL 触发和实验数据记录。开发中必须始终把硬件安全放在第一位。

## 当前技术基线

- 操作系统：Windows 10/11。
- Python：3.11。开发者可使用 conda、venv 或系统 Python；文档和脚本不依赖某一台电脑的解释器绝对路径。
- GUI：PySide6 6.7.2 + PySide6-Fluent-Widgets / QFluentWidgets；正式产品窗口使用 `FluentWindow`。
- 实时图形：pyqtgraph。
- 硬件接口：nidaqmx、pyserial。
- 打包：PyInstaller。
- 测试：pytest、pytest-qt。
- 代码检查：ruff，目标版本为 `py311`。
- 依赖管理：使用 `requirements.txt` 和 `requirements-dev.txt`，不使用 Poetry 作为当前项目基线。

### 正式 UI 基线

- 使用 QFluentWidgets Dark Theme：近黑、深墨绿/石墨色，主题强调色为 `#E2AD50`。
- 当前产品范围包含“手动实验”“自动实验”两个实验工作页面和“设置”工具页；设置不是第三种实验模式。尚未实现的页面不加占位入口，预测试、协议模式、校准、清洗和呼吸实验不恢复为正式导航；呼吸触发未来属于 Auto trigger strategy。
- 产品界面优先使用 QFluentWidgets 原生 Card、Label、数字输入、Button、Badge、ToolTip 和 InfoBar；不使用旧 QWidget/QSS 控制台作为未来标准，不引入 superqt。
- selected 使用琥珀强调；真实开启使用绿色图标状态；故障使用红色且形态不同的图标状态。重要状态不得只靠颜色或 Tooltip 表达。
- “设置”位于正式导航底部；手动实验页只提供一个“气口设置”快捷入口，并切换到同一页面。普通设置固定显示 2×10 面板气口，只允许按 `20-channel` preset 选择控制通道，resolved NI target 与极性在高级区只读。
- 气口配置始终区分三层：面板气口 `external_port` → 控制通道 `internal_valve` → NI/芯片接口 `target`。当前默认八路为 02→02、04→03、06→04、08→05、12→06、14→07、16→08、18→09；它只是默认 HardwareProfile，不是永久硬编码规则。

## 架构原则

- 采用 MVC + Worker + HAL。
- `views/` 只负责界面展示和用户交互。
- `controllers/` 负责编排业务流程、安全状态和界面状态。
- `workers/` 承担硬件工作线程和低抖动执行逻辑。
- `services/` 放置硬件抽象、配置、日志、协议解析等可复用服务。
- `models/` 保存配置、会话、硬件状态和协议等结构化数据。
- 所有真实硬件访问必须经过 HAL，便于模拟、测试和安全降级。

### 执行域边界

- Protocol、Manual、Maintenance 是三个互斥执行域；每个域只接受能够由 lease、command category/identity、executor context 或 safe-transition identity 明确归属本域的证据。
- Protocol lease 是 Protocol ownership 的最强证据。仅加载文档、非零 epoch、任意 non-idle lease、普通 flow ready、active valve 或 possibly-open 状态都不能单独证明 Protocol 正在执行。
- Manual/Maintenance 的 lease、pending command、valve、possibly-open 和 readiness 不得触发 Protocol invalidation；真正 active Protocol 的 readiness 丢失仍按现有 fail-closed 路径处理。
- 单口验证使用独立 Verification ownership，只接受已保存且 revision/fingerprint 与运行时一致的 profile；模拟验证可生成 `MOCK_VERIFIED`，production stub 不得生成 `PHYSICAL_VERIFIED`。
- 配置编辑与验证是两种权限：mapping 及普通保存仅允许设备断开且 owner 全部 handoff；验证则要求设备已连接、ready、safe idle、无未保存 draft 和竞争 owner。验证 evidence 只更新匹配 revision/fingerprint 的单口状态，不触发 connected mapping hot reload。

## 目标用户

- 心理学、神经科学或嗅觉实验研究人员：需要加载协议、执行实验、记录数据，界面必须中文且容易理解。
- 实验室技术人员：需要连接设备、校准呼吸阈值、测试阀门、清洗管路和排查硬件状态。
- 后续开发者：需要清楚的文档、测试、代码结构和中文提交历史，以便持续维护。

## 功能范围

范围内：

- 启动自检、连接状态显示、硬件重连和安全复位。
- 气流阈值安全联锁，低气流时禁止阀门和加热器危险动作。
- 呼吸信号实时显示、吸气/呼气阈值调节、LED 状态反馈。
- 10/20 通道气味阀矩阵手动测试。
- Alicat A/B/C 三路流量设置和补偿逻辑。
- 协议文件解析、手动触发、TTL 触发、呼吸门控刺激。
- `.raw` 信号文件和 `.log` 事件日志输出。
- 清洗流程、配置持久化、中文界面。
- Mock HAL 硬件模拟模式。

范围外：

- 云端同步、多人协作、移动端或网页端。
- 实验结果统计分析和高级可视化。
- 多语言界面。当前项目以简体中文为唯一交付语言。

## 关键硬件映射

Manual 长期领域规则：可编辑 setpoint 只有独立 A/B/C；`total_delivery=A+B` 仅为派生值，B 在正常供气、selector 切换、刺激和恢复期间保持用户设定不变。baseline/restore controller targets=`A+C/B/C`，stimulus=`A/B/0`。

- NI USB-6001 `Dev1`
  - `AI0`：呼吸传感器模拟输入。
  - `AI6`：外部 TTL 触发输入。
  - `P0.0-P0.7`、`P1.0-P1.3`：气味通道 1-12。
- NI USB-6001 `Dev2`
  - `P1.0`：A 路三通选择阀，配置中的历史名称为 `master_valve=Dev2/P1.0`，运行时转换为 NI-DAQmx 线路 `Dev2/port1/line0`。现场 HIL 已确认低电平选择 `A → 补偿出口`，高电平选择 `A → 气味阀1–20总入口`；该阀没有独立全关态，不得建模成第21只普通两通阀。
  - `P0.0-P0.7`：气味通道 13-20，以 `config/default_config.json` 的 `valve_mapping` 为准。
- 当前 Manual runtime 不要求 NI USB-6501；当前呼吸/TTL 采集、selector 和 20 通道气味阀仍全部由 `Dev1`、`Dev2` 承担，USB-6501 不加入 Manual required devices、startup self-check 或 connection readiness。
  - 未来 Auto ingress 已确认使用 USB-6501 接收 SuperLab → c-pod 的 8-bit `Trig.In`；这不等于 reader/readiness 已实现。
  - Auto Build 必须先用设备铭牌与 NI MAX 确认 alias 和物理 port/line，再通过本机配置登记并完成 DAQmx task 与 HIL；不得把它加入气味阀映射或据此改变当前 Manual 门禁。
- Alicat RS232
  - MFC A：气味/补偿气路。
  - MFC B：载气气路。
  - MFC C：排空气路。

## 项目进度来源

Epic/Story 的当前状态只维护在 `docs/sprint-artifacts/sprint-status.yaml`。本文档只说明项目背景、技术基线和长期规则，不重复写动态进度，避免与 sprint 状态文件不同步。

## 文档层级

- 当前权威：`project-context.md`、`prd.md`、`architecture.md`、`ux-design.md`、`project-structure.md`。
- 动态状态：`sprint-artifacts/sprint-status.yaml`。
- 活动执行工件：`sprint-artifacts/spec-*.md` 及尚未终结的 Story。
- 审计证据：`sprint-artifacts/evidence/`；不得因整理文档而删除安全、HIL、极性或发布证据。
- 历史资料：`archive/`；用于追溯，不覆盖当前权威。

## 文档语言规范

项目文档、story、commit message、软件界面文字、脚本提示和开发沟通默认使用简体中文。必要的技术名词、库名、命令名和文件名保留英文原文。
