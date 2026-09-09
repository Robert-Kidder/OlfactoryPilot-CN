# OlfactoryPilot-CN 产品需求文档

## 1. 背景与目标

OlfactoryPilot-CN 用于替代原有法国软件 **ProgOlfactoTao**，服务本地嗅觉刺激实验。旧软件依赖 LabView 生态和法文说明，维护、培训和本地化成本较高。新软件需要用 Python 3.11 和 PySide6 实现中文桌面应用，保留旧系统的核心实验能力，并提高安全性、可测试性和可维护性。

产品目标是让实验人员在 Windows 电脑上安全完成设备连接、硬件方案配置、手动实验、未来自动实验和数据记录。当前正式 runtime 只包含“手动实验”和作为工具页的“设置”；设置不是实验模式。未来 Auto 验收后才成为第二个实验工作页。预测试、协议模式、校准、清洗和呼吸实验不作为当前正式导航页面，呼吸触发未来属于自动实验的一种 trigger strategy。

## 2. 用户与场景

- 研究人员：在新版手动实验界面设置流量、选择一个或多个机外气口、按指定时长释放气味，并保存事件和会话记录。
- 实验室技术人员：配置 COM/NI 设备和气口映射、验证气口、清洗管路并处理硬件异常。
- 开发维护人员：通过清晰架构、测试和中文文档持续扩展软件。

## 3. 功能需求

### FR1：安全硬件基础

- FR1.1：启动或点击连接时，自检配置中声明的 NI 设备和 RS232 端口。当前 Manual runtime 基线仅为 `Dev1`、`Dev2` 两台 NI USB-6001，USB-6501 不属于 Manual required devices、startup self-check、connection readiness 或连接成功门禁。
- FR1.2：气流必须满足配置的安全条件，才允许气味阀、A 路三通选择阀和其他危险动作。
- FR1.3：退出、急停、异常断连或停止操作时，必须先请求并确认 MFC A 清零，再将 A 路三通选择阀切换到定义的安全路线；气味阀、B/C、owner handoff 和资源释放按架构规定收敛。任何关键回执失败或状态不确定时进入 `RECOVERY_REQUIRED`，不得报告“已安全停止”。
- FR1.4：全局工具栏提供连接、重置、停止和帮助入口。

### FR2：文件与会话

- FR2.1：根据时间、受试者和条件自动生成 `{Timestamp}_{Subject}_{Condition}.raw` 文件名。
- FR2.2：解析旧系统兼容的 `.txt` 和 `.csv` 实验协议文件。
- FR2.3：每次实验会话保存 `.raw` 信号文件和 `.log` 事件日志。

### FR3：呼吸与 TTL 既有能力（当前 UI 延期）

- FR3.1：既有呼吸采集、门控和 TTL 底层能力可以保留，但当前产品不提供呼吸传感器操作页面或占位入口。
- FR3.2：只有在呼吸硬件与实验需求明确后，才重新定义校准、阈值、波形和门控 UX，并另行评估验收与 HIL。

### FR4：手动实验与硬件方案

- FR4.1：手动实验固定显示机外气口 1–20；未接入位置变灰，可用性不得写死，必须来自当前持久化 HardwareProfile。
- FR4.2：HardwareProfile 可视化配置“机外气口 → 内部控制阀位 → NI 线路”，支持显示名称、气口验证、自然验证状态和跨启动保存。当前初始化映射为机外 2/4/6/8/12/14/16/18 对应内部阀位 2–9。
- FR4.3：`Dev2/P1.0` 作为 A 路三通选择阀独立建模，不占用机外气口编号，也不作为第 21 只普通阀。
- FR4.4：手动实验支持一个或多个 available 气口、独立 A/B/C 流量和指定刺激时长；A+B 只作为派生总送风显示与已确认的 `max_total_sccm` 联合上限，B 始终等于用户输入。
- FR4.5：手动刺激从全部目标成功 open receipt 的共同就绪时刻起算，由 `ActuationWorker` 使用 monotonic deadline 自动结束；UI 定时器只刷新显示。
- FR4.6：Manual baseline/restore 的 MFC 目标为 `A+C/B/C`，stimulus 为 `A/B/0`。selector 与气味阀 1–20 独立，正常结束保持 close receipts → A=0 → compensation → restore → COMPLETED → 精确释放 lease。

### FR5：实验执行边界

- FR5.1：Protocol、Manual 和 Maintenance 各自持有明确 ownership；一个域的 lease、command、valve 或 readiness 不得触发另一个域的失效或安全停止。
- FR5.2：未来自动实验必须复用与手动实验相同的 Worker/HAL、lease、epoch、receipt 和动作计划执行核心，不得模拟按钮点击或复制硬件控制链。
- FR5.3：阀门动作继续记录 expected、actual 和 jitter；真实硬件性能结论必须来自适用 HIL。

### FR8：未来自动实验协议与触发（需求基线，尚未实施）

- FR8.1：Auto 使用多行 TXT 参数文件。每行依次包含：`duration_ms`；20 个独立二值列 `channel_01`…`channel_20`；独立 `sample_flow_a`、`main_flow_b`、`vacuum_flow_c`（ml/min）；以及可能固定存在的尾部 `NULL`。20 个 channel cell 分别只能为 `0` 或 `1`，不得默认打包为一个 bitmask/integer；尾部 `NULL` 允许存在、由未来 parser 忽略且不作为控制参数。
- FR8.2：无外部新指令时，Auto 必须支持加载 TXT、从指定 1-based 起始行开始逐行顺序执行；每行使用自身 duration、20 个 channel states 和 A/B/C。当前行结束且无新外部触发时，恢复与 Manual 同义的普通无刺激阶段：阀全关、selector=compensation、MFC=`A+C/B/C`。相邻 trial 之间采用哪一行的 baseline 留待 Auto execution design 根据文件语义确认。
- FR8.3：外部链路确认为 SuperLab → Cedrus c-pod 数字输出 → NI USB-6501 → OlfactoryPilot。相同实验标记另一路进入现场采集设备仅作拓扑背景，不由本软件控制。USB-6501 输入是 8-bit，包含 `Trig.In` 与 TXT 行选择；`10000001` 按已确认接口约定选择 1-based 第 128 行，不得按通用二进制直觉改成 129。
- FR8.4：`Trig.In` 有效且得到行号 N 时，未来实现必须验证文件至少有第 N 行、解析该行、转换为 canonical trial command，并复用 Manual 的 Worker/HAL/lease/receipt 执行核心；不得模拟 UI 点击。N 越界或目标行无效时明确拒绝，不能 clamp、wrap 或执行其他行。
- FR8.5：USB-6501 是 Auto ingress 的确认硬件，但 reader 与真实 HIL 尚未实施。未来 HIL 需锁定 NI MAX alias、8-line port/terminals、DAQmx task、polling/change detection/edge、pulse width/debounce/sampling、startup/reconnect、latency 和可靠性；这些待办不否定设备与上层编码需求，也不得加入当前 Manual 启动、连接或 readiness 门禁。
- FR8.6：未来 Auto/Settings 提供两种外部触发冲突策略：“排队等待”按到达顺序在当前 trial 完成后执行；“优先最新触发”先安全结束/收敛当前 trial，再执行最新行，绝不能直接打开新气味阀。旧等待 trigger 的丢弃/合并细节留待 Auto state machine 设计。
- FR8.7：未来 breath gating 必须支持 minimum inter-stimulus interval；refractory interval 内的呼吸事件不能立即启动下一刺激，间隔结束后等待下一次满足门控条件的呼吸事件。interval 从刺激开始还是结束计时、期间外部 trigger 排队/替换/丢弃、最终 breath phase、校准和 threshold UX 尚待实验方案确认。

### FR6：清洗

- FR6.1：保留自动清洗目标和现有实现资产；在全局停止顺序修复后，按三通选择阀和 HardwareProfile 新语义修正并完成验收。

### FR7：配置与本地化

- FR7.1：通过界面配置 COM 端口、NI 设备 ID、HardwareProfile、气口显示名称、内部阀位、NI 线路和验证状态。
- FR7.2：界面、错误、提示、日志摘要和帮助入口使用简体中文。
- FR7.3：最终产品只保留新版界面，不提供新旧界面切换；旧 `PreTestView`、旧 View 计时、重复状态和确认无用的弹窗/代码在新链验收后删除。
- FR7.4：Settings 后续允许为气口 1–20 配置 enable/disable、alias、external port、internal valve/control channel、NI target、polarity 和 verification state，并继续使用 HardwareProfile/ChannelRegistry/HardwareProfileStore 的 revision、atomic save、last-known-good、rollback 与 verification fingerprint；保存结果必须跨重启生效。
- FR7.5：“验证此气口”一次只验证一个气口，使用专用 Maintenance/Verification ownership 与 Worker monotonic deadline，始终允许立即安全停止。默认约 20 秒、1500 ml/min，但必须服从实际 MFC 上限；只有用户在实体设备端明确确认后才记录 `PHYSICAL_VERIFIED`，mapping 改变后自动失效。当前代码已实现基于 fake-real HAL 的完整安全链，production 资格仍必须等待 C.3b 实体 HIL evidence。

## 4. 非功能需求

- NFR1 安全：硬件安全逻辑必须独立于 UI 响应能力，UI 卡顿不能绕过安全联锁。
- NFR2 性能：实时气流图以约 20–30Hz 刷新且不得阻塞控制路径；关键硬件状态推送保持 5–10Hz。
- NFR3 可靠性：USB、串口、磁盘写入异常必须有明确错误、日志和安全降级。
- NFR4 可维护性：保留 MVC + Worker + HAL 结构，新增功能必须有相应测试。
- NFR5 分发：使用 PyInstaller 生成 Windows 可执行产物。
- NFR6 技术基线：固定当前 Python 3.11、PySide6、pyqtgraph 和现有依赖版本，不在本轮安排版本或技术栈迁移。

## 5. 状态边界

本文只维护产品需求，不记录 Epic/Story 当前进度、推荐实施顺序或下一页面。动态状态统一以 `docs/sprint-artifacts/sprint-status.yaml` 为准，历史决策与证据分别进入 `docs/archive/` 和 `docs/sprint-artifacts/evidence/`。
