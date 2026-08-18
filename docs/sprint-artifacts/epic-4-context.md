# Epic 4 Context: 运行安全、新版手动实验与交付收口

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

先修复所有停止路径中已确认的气路安全顺序，再交付唯一的方案 B V3 手动实验界面、可持久化硬件方案、由硬件 owner 持有的可靠刺激计时，并在新链验收后移除旧界面。该 Epic 复用现有 Worker、HAL、lease、epoch、receipt、maintenance bundle 与恢复机制，不进行技术栈迁移，以形成安全、可验证且可维护的 Windows 实验控制基线。

## Stories

- Story 4.1: 自动清洗流程
- Story 4.2: COM 与 NI ID 配置界面
- Story 4.3: 中文界面本地化
- Story 4.4: 补偿逻辑与主阀自动化
- Story 4.5: 全局停止顺序与三通阀模型
- Story 4.6: 方案 B V3 手动实验替换

## Requirements & Constraints

- 当前新增实施范围仅为 Story 4.5 和 4.6。自动清洗资产保留但暂停；配置、本地化和补偿能力并入新版手动实验，不再作为独立开发单元。
- 全局停止、异常停止、断连和 shutdown 必须先请求并确认 MFC A 清零，再把 A 路三通 selector 切到安全路线。A 清零失败、超时、stale/late/conflicting receipt 或 selector 状态不确定时必须进入 `RECOVERY_REQUIRED`，不得宣称安全停止。
- `Dev2/P1.0` 是无独立全关态的二选一路由 selector，必须独立于气味阀 1–20 建模，不能称为主阀、第 21 只普通阀或机外气口。
- 方案 B V3 是唯一手动实验入口。界面固定显示机外气口 1–20，未接入位置禁用且不能生成硬件 intent；可用性、显示名和线路映射来自持久化 `HardwareProfile`，不得写死在 View。
- 当前初始化映射为机外气口 2/4/6/8/12/14/16/18 对应内部阀位 2–9。方案配置需支持机外气口、内部阀位、NI target、极性、启用状态、显示名称与验证状态。
- 手动实验支持多气口、T/A/B/C 流量和刺激时长；领域规则必须保证 `0 ≤ A ≤ T` 并派生只读 `B=T-A`。刺激时长从所有目标成功 open receipt 的共同就绪时刻起算，并由硬件 owner 按 monotonic deadline 自动结束。
- 当前不交付自动实验或呼吸传感器 UI，也不保留未实现入口；既有底层协议、TTL 和呼吸门控能力可保留。未来自动实验必须复用同一动作执行核心，不能模拟 UI 点击。
- USB、串口和磁盘异常必须产生明确日志、中文错误和安全降级。安全逻辑不得依赖 UI 响应能力；实时图约 20–30 Hz、关键硬件状态约 5–10 Hz，均不得阻塞控制路径。
- 新功能必须具备自动化测试。涉及 selector、停止计划、Worker、NI/serial、deadline 或映射的变更按范围执行真实 Windows/NI HIL；HIL 证据不能替代自动化测试，也不能把 `daqmx_write_ack` 当成机械动作证据。

## Technical Decisions

- 固定使用 Python 3.11、PySide6 Widgets、pyqtgraph、pytest/pytest-qt、ruff、PyInstaller 和现有依赖基线；维持 MVC + Worker + HAL 分层。
- `FlowWorker` 是 Alicat 串口单写者，`ActuationWorker` 独占 DO session 和动作 deadline，`HardwareWorker` 独占 AI task。Controller 只提交 intent，View 不访问 HAL、不判断安全，也不乐观声明硬件完成。
- 控制链采用 Intent → Command → Receipt → immutable Snapshot。receipt 必须携带并匹配有效的 lease/epoch/phase 身份；过期、迟到或冲突回执不得推进状态机。
- 全局、故障和退出停止共享 `SafeStopPlan`。强制偏序以 A 清零成功 receipt 为 selector 安全切换的硬前置，其余气味阀、B/C、DO、AI、serial 和 owner handoff 收敛由同一计划定义；DO owner 未交还时不得跨线程复用旧 task 兜底写入。
- `FlowSetpoints` 持有流量校验与 B 派生规则；`ChannelRegistry` 负责机外气口、内部阀位和 NI target 的映射；手动与未来自动模式生成相同的 typed phase plan。
- `HardwareProfile` 由 `default_config.json` 与本机 `local_config.json` 合并产生。候选保存必须在断开且安全、无活动 session/maintenance/lease、owner 已交还时进行，并经过 schema、唯一性和交叉校验、同目录原子替换及显式回滚。安全配置不得存入 View 私有字段、QSettings 或第二份 JSON。
- 映射或极性变化必须使相关气口重新进入待验证；仅修改显示名称可保留验证。并发与故障测试使用 fake clock、Event/Barrier、cancellation token、fake filesystem 和 fault injection，禁止仅靠 sleep 断言竞态。

## UX & Interaction Patterns

- 顶部始终显示设备连接状态、设置入口和“全局停止”；安全、停止收敛和恢复要求必须用文字持续可见，颜色只能辅助表达。
- 手动实验页采用实时气流区与实体控制台式操作区，包含 T/A/B/C、供气、固定 2×10 气口矩阵、刺激时长和“释放气味”。“可用、已选择、实际开启、故障”必须可区分。
- 按钮只发送 intent；文案、使能、倒计时和完成状态由 immutable Snapshot 驱动，UI 定时器只刷新显示。只有 A 路 telemetry 时不得把图表称为 A+B 总流量或展示未经证实的“稳定”。
- 配置页以机外气口和内部阀位作为普通用户术语，将 NI target、极性和原始 receipt 放在高级区域；保存和验证过程显示明确阶段，失败时保留旧配置并提供显式回滚。
- 常规和可恢复错误使用页面内状态卡，严重安全故障才使用阻断提示。所有用户可见文案使用简体中文，并说明发生了什么、系统采取了什么安全动作、用户下一步应做什么。

## Cross-Story Dependencies

- Story 4.5 必须先完成自动化测试和单独授权的真实 HIL，Story 4.6 才能接入真实硬件。
- 该门禁的 normal 动作偏序与收敛验收已于 2026-08-18 满足并归档；现场观察证明停止前 `odor` 路线的阀 2 持续气流，停止后 `compensation` 只有软件 receipt/电子 ack，不能写成机械路线确认。Story 4.6 可继续离线开发和 Mock/UI 验证，但在其 selector/safe-stop 路径接入真实硬件前，必须另行设计、授权并通过 compensation 物理出口映射 HIL。故障场景继续采用确定性离线注入。
- Story 4.1 保留现有清洗实现、maintenance lease、bundle、deadline 和恢复资产；恢复开发前必须采用 Story 4.5 的 selector 与 `SafeStopPlan` 以及 Story 4.6 的 `HardwareProfile`，不再使用 21-target 终态。
- Story 4.2、4.3 和 4.4 的有效需求由 Story 4.6 统一承接。新版链通过 Mock、UI 和适用 HIL 后，才能删除旧 `PreTestView`、View 计时、重复状态、无用弹窗及第二条真实硬件入口。
