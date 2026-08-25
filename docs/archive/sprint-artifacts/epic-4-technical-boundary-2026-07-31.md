# Epic 4 技术边界冻结 — 运行维护、清洗与本地化

Status: Frozen
Epic: 4 — 运行维护、清洗与本地化
Frozen On: 2026-07-31
Approved By: Jing（本轮明确要求“调整并冻结 Epic 4 的技术边界”）
Baseline: Epic 3 retrospective、`docs/prd.md`、`docs/epics.md`、`docs/architecture.md`、`docs/ux-design.md`

<frozen-after-approval reason="Epic 4 implementation boundary — changes require a new Correct Course proposal">

## 1. 冻结结论

Epic 4 保留 Story 4.1–4.4，不新增 Epic，不改变 PRD 的产品目标或 MVP 范围。技术实现必须建立在 Epic 3 已验证的四条链上：

1. 协议与操作身份链：immutable intent、generation/epoch、command identity、receipt identity。
2. 硬件 owner 链：AI、DO、serial、文件分别只有一个可变状态 owner。
3. 安全关闭链：invalidate → emergency close → 目标集合 receipt → owner handoff。
4. session durability 链：producer fence、事务式发布、失败隔离、显式恢复。

推荐实施顺序为 **4.2 → 4.1 → 4.4 → 4.3**。Story 编号不变；该顺序先固定配置事务，再引入维护状态机和跨 owner 补偿编排，最后对全部用户路径做中文收口。

## 2. Epic 级架构不变量

### 2.1 唯一 owner topology

- `HardwareWorker`：唯一 AI0/AI6 continuous task owner。
- `ActuationWorker`：唯一 `ProtocolExecutor`、`GatingService`、动作质量窗口和全部 DO owner。
- `FlowWorker`：唯一 Alicat serial owner。
- `SessionWriterWorker`：唯一实验 session 与 maintenance bundle 文件 owner。
- Controller 只编排 immutable intent/result；View 只发送用户意图并渲染 snapshot。
- View、Controller、`QTimer`、临时线程和 ad-hoc worker 禁止直接调用 HAL、serial 或同步文件 I/O。

### 2.2 统一安全与恢复语义

- write/ack 不确定即标记 `possibly_open`；只有目标匹配的成功 close receipt 才能清除。
- stop、LOW_FLOW、disconnect、recorder failure、owner failure、stale generation 和 shutdown failure 均 fail closed。
- SAFETY close 可绕过普通动作队列和 recorder readiness，但任何 open、流量切换或维护动作不得伪装为 SAFETY。
- safe telemetry 不自动清除 unsafe latch。恢复必须同时满足：readiness 恢复、配置目标已确认关闭、owner 已交接、用户显式执行恢复动作。
- stale/late receipt 不得推进当前状态机；若 stale successful open 造成 `possibly_open`，必须进入安全关闭收敛。

### 2.3 设备租约与记录门禁

- 协议、manual/pretest、cleaning、compensation/config-change 使用显式且互斥的 lease。
- 同一时刻只允许一个业务流程拥有可能改变阀门或流量的 lease。
- CLEANING 和普通危险动作的 recorder readiness 必须在首次硬件副作用前成立。
- queue full、I/O、fsync、close、manifest 或 publish 失败先锁存 `recording_ready=False`，再通知动作 owner；失败 bundle 不得冒充 complete，也不得原地续写。

### 2.4 不在 Epic 4 内改变的语义

- `ProtocolTrial.timing_ms` 继续只解析和展示，不参与执行。
- `daqmx_write_ack` 仍只表示软件到 DAQmx write ack，不表示机械阀物理完成。
- 当前生产 NI 基线仍是 `Dev1`、`Dev2` 两台 USB-6001；Dev3/USB-6501 仅可在实物、NI MAX 和用途确认后由本机覆盖显式加入。
- 旧系统 `.raw` byte compatibility、机械 loopback、破坏性故障测试、多语言、云同步和新硬件扩展不属于 Epic 4。

## 3. Story 4.1 — CLEANING 状态机冻结

### 3.1 业务类别和记录

- 新增显式 `CLEANING` action category；它不是 `NORMAL`、协议质量样本或 `SAFETY`。
- 清洗取得独占 maintenance lease 后，协议、manual/pretest 和 compensation open 均被拒绝。
- 清洗使用独立 maintenance bundle，不写入实验 session：
  - 目录使用事务式 staging/publish；
  - 包含结构化 `.log` 和 `manifest.json`，不生成实验 `.raw`；
  - `manifest` 固定记录 `schema=maintenance-v1`、operation identity、配置快照、步骤计数、receipt 计数、hash、最终状态和失败原因；
  - 文件仍由 `SessionWriterWorker` 单写，ownership marker、失败隔离和 recovery 语义沿用 Epic 3。
- maintenance bundle 绑定、初始全关确认和 recorder readiness 未完成前，不得执行清洗 open。

### 3.2 状态和身份

状态固定为：

`IDLE → PREPARING → RUNNING → STOPPING → COMPLETED`

任意非终态可进入：

`FAILED → RECOVERY_REQUIRED`

每个 step/command/receipt 至少携带：

`operation_id + generation + step_id + command_id + target + action_kind`

只有 generation、step、command 和 target 全部匹配的成功 receipt 才能推进步骤。

### 3.3 清洗 I/O 与故障矩阵

| 场景 | 必须行为 | 终态/证据 |
|---|---|---|
| 正常启动 | 获取 maintenance lease；绑定 bundle；确认 21 个配置目标全关；再进入步骤 1 | `PREPARING → RUNNING`，记录配置快照和初始 close receipt 集合 |
| 正常步骤 | owner 状态机按 monotonic deadline 提交；前一步终态明确后才进入下一步 | 记录 step start/end、command、receipt、elapsed |
| 用户中止 | invalidate generation；停止新步骤；抢占式关闭 21 个配置目标 | 全部 close receipt 成功才可 `COMPLETED/aborted` |
| LOW_FLOW / disconnect | 锁存 unsafe；停止普通步骤；执行 emergency close | `FAILED`；记录原因、安全动作和用户下一步 |
| recorder failure | 首先使 `recording_ready=False`；禁止后续 open；SAFETY close 保持可用 | bundle 隔离，进入 `RECOVERY_REQUIRED` |
| stale/late receipt | 不推进新步骤；late successful open 标记 `possibly_open` 并触发关闭 | 记录 stale identity 与收敛 receipt |
| close timeout/uncertain | 保留 `possibly_open`，禁止释放为可运行状态 | `RECOVERY_REQUIRED`，需要显式恢复 |
| shutdown | 与 stop 相同先 invalidate 和全关，再按固定 owner 顺序释放 | 21-target receipt + owner handoff 证据 |

### 3.4 禁止实现

- 禁止用 UI `QTimer`、`sleep` 或临时线程编排清洗步骤。
- 禁止把 CLEANING open 标为 SAFETY 以绕过 interlock、lease 或记录。
- 禁止以 UI 按钮禁用状态代替 owner/service 守卫。

## 4. Story 4.2 — 断开态配置事务冻结

### 4.1 配置真源和前置状态

- 唯一配置真源保持 `default_config.json + local_config.json` 递归覆盖，不新增数据库、第二份 JSON 或 UI 私有配置。
- 只允许在以下条件全部成立时应用：
  - disconnected；
  - 无 active session/maintenance operation；
  - 无 protocol/maintenance/compensation lease；
  - 无 active/possibly-open target；
  - 21 个配置目标已由 receipt 确认关闭；
  - AI、DO、serial owner 已安全停止并完成 handoff。

### 4.2 原子事务与 commit point

1. 获取 `CONFIG_CHANGE` 独占 lease，并冻结连接/运行入口。
2. 在临时 candidate 中递归合并和校验，不修改 active snapshot。
3. 校验设备别名、产品型号、AI/DO 通道、阀门映射唯一性、主阀 target、串口参数和 10/20 通道变体；Dev3 不得作为默认必需设备。
4. 使用只读 inventory/probe 验证候选设备；probe 不得占用活动 owner 资源。
5. 将 candidate 写入同目录临时文件，flush/fsync 后 atomic replace `local_config.json`。
6. 发布新的 immutable config snapshot；系统保持 disconnected，下一次连接只使用该 snapshot。

**Commit point**：atomic replace 成功且新 snapshot 发布完成。此前任何失败均无运行时副作用；此后若下一次连接自检失败，保留已保存配置但保持 disconnected/fail-closed，并允许用户显式回滚到事务前快照。

### 4.3 失败与回滚

- 校验、probe、写临时文件、fsync 或 replace 任一步失败：旧文件和旧 active snapshot 保持不变。
- owner 未 handoff、存在 lease/session/possibly-open：拒绝事务，不尝试“边运行边重连”。
- 回滚也是同样的断开态原子事务，不允许直接覆盖内存配置。
- 配置错误提示必须说明：错误字段/设备、系统保持的安全状态、修正或回滚步骤。

## 5. Story 4.3 — 中文与乱码契约冻结

- 用户可见按钮、标签、状态、错误、恢复、清洗、配置和帮助文本统一为简体中文 UTF-8。
- 错误文本必须同时包含：发生了什么、系统采取了什么安全动作、用户下一步做什么。
- 颜色只作辅助；安全、警告、失败和进行中状态必须有可读文字。
- 自动化验收必须同时包含：
  - 静态用户可见字符串清单，禁止遗留旧英文提示；
  - UTF-8 严格解码、Unicode replacement character 和常见 mojibake 扫描；
  - pytest-qt 关键页面/对话框遍历；
  - protocol、session、recovery、config、cleaning、LOW_FLOW、DATA_STALE、disk failure 和 shutdown 路径覆盖。
- 技术标识、协议字段、设备 ID、文件扩展名和结构化日志 machine key 可保留英文；不得把这些允许项扩大为整段英文用户提示。

## 6. Story 4.4 — flow → master → odor 状态机冻结

### 6.1 Owner 与身份

- MFC setpoint 只由 `FlowWorker` 写入；主阀和气味阀只由 `ActuationWorker` 写入。
- compensation 流程必须取得与协议/cleaning/manual 互斥的 lease，或作为当前 protocol lease 内的明确子状态执行。
- flow intent/receipt 和 valve command/receipt 使用同一 phase identity：

`session_id + protocol_epoch + phase_generation + phase_id + command_id + target`

- 只有完整 identity 匹配的成功 receipt 才能推进；duplicate/conflicting/stale receipt 记录后拒绝。

### 6.2 状态顺序

刺激准备：

1. `PREPARE_STIM_FLOW`：提交 MFC A=`A_target`、MFC C=`0`，等待匹配的成功 flow receipt。
2. `PREPARE_MASTER`：提交主阀 open，等待成功 DO receipt。
3. `READY_FOR_ODOR`：此时才允许协议进入实际 trigger/gating。
4. `ODOR_OPEN`：按 Epic 3 的 odor deadline 提交目标气味阀；MFC serial write、主阀 cold start 和同步日志不得进入该 deadline 路径。

返回静息：

1. `CLOSE_ODOR`：按刺激 duration 关闭气味阀并确认 receipt。
2. `PREPARE_REST_FLOW`：提交 MFC A=`A_target + C_target` 和 MFC C=`C_target`，等待成功 flow receipt。
3. `CLOSE_MASTER`：关闭主阀并确认 receipt。
4. `REST_READY`：确认气味阀和主阀关闭、flow receipt 匹配，记录阶段终态。

### 6.3 失败语义

- flow failure/timeout、serial owner failure、epoch drift 或 receipt identity 不匹配：不得提交后续 open；进入 fail-closed。
- 主阀 open 不确定：标记 `possibly_open`，不得提交 odor open，执行目标集合关闭。
- odor open/close 延续 Epic 3 jitter 口径；flow 和 master 准备 receipt 不进入 odor 正常质量窗口。
- stop、LOW_FLOW、disconnect、recorder failure 可抢占任何 phase，invalidate phase generation，并收敛 21 个配置目标。
- phase start、flow intent/receipt、master/odor receipt、失败和恢复事件写入当前实验 session 的结构化日志；不得重置 session identity 或 jitter window。

## 7. 确定性并发测试清单

所有跨 owner 交错必须使用 fake clock、Event/Barrier、cancellation token、fake filesystem 或 fault injection 固定排列；毫秒级 `sleep` 或重复运行不得作为唯一证明。

| ID | 交错 | 必须断言 |
|---|---|---|
| CC-01 | cleaning step vs stop | stop 抢占；旧 step receipt 不推进；21-target 全关 |
| CC-02 | cleaning open ack vs LOW_FLOW | late successful open 进入 `possibly_open` 并被关闭 |
| CC-03 | maintenance recorder queue full vs open | `recording_ready=False` 先于后续 open；SAFETY close 可执行 |
| CC-04 | maintenance finalize vs shutdown | producer fence 顺序不丢事件；失败不发布 complete |
| CC-05 | config apply vs owner handoff | handoff 未完成时无文件/内存副作用 |
| CC-06 | config atomic replace failure vs rollback | 旧 snapshot 与旧文件一致可用 |
| CC-07 | flow ack vs phase invalidation | stale flow ack 不提交 master/odor |
| CC-08 | master ack vs recorder failure | 不提交 odor open；执行全关 |
| CC-09 | odor close timeout vs rest transition | 不进入 `REST_READY`；保留 `possibly_open` |
| CC-10 | duplicate/conflicting receipt | 终态只提交一次；冲突被记录并 fail closed |
| CC-11 | session finalizer vs late phase event | fence 前事件必达，fence 后旧 generation 被拒绝 |
| CC-12 | localization scan vs allowed machine key | 用户文本零漏报，协议字段/设备 ID 允许项有显式白名单 |

## 8. 真实 Windows/NI HIL 触发矩阵

| 变更范围 | 必须复验 |
|---|---|
| 仅 View 布局、纯中文文案且不改 signal/slot、owner 或 gate | Pytest/pytest-qt、乱码扫描、打包后 Mock 启动；无需 NI HIL |
| 配置 merge/事务、设备 inventory、NI/COM ID 或通道映射 | 断开态保存/拒绝/回滚、Dev1/Dev2 枚举、COM probe、连接失败保持 fail-closed；若 DO 映射变化则追加 21-target 全关 |
| CLEANING、FlowWorker、主阀/MFC 编排、lease/interlock | 惰性气体、无气味、无受试者；验证实际步骤/receipt 顺序、stop/LOW_FLOW/disconnect、stale receipt 与最终 21-target 全关 |
| ActuationWorker、HardwareWorker、DO/AI task、queue priority、协议 deadline | 重跑 200 open + 200 close；aggregate/rolling/final-last-100 p95；stop、LOW_FLOW、severe、shutdown 四类安全场景 |
| SessionWriter、recorder readiness、maintenance bundle 或 fence/finalize | 真实记录负载下 bundle hash/count/sequence/fence Gate；queue/disk 故障用可控注入，不做破坏性填盘 |
| shutdown 顺序或 owner handoff | 初始、stop、失败和最终 shutdown 的 21-target receipt 集合及 owner 释放顺序 |

HIL 证据必须记录 candidate commit、worktree 状态、Windows/Python/NI/COM 环境、硬件身份、首样本、阈值、完整失败 run 和声明边界。Mock、软件注入和 `daqmx_write_ack` 不得扩称为外部传感器、机械阀或破坏性故障证据。

## 9. Story Gate 与发布 Gate

### 9.1 每个 Story 的 Definition of Done

- AC、实现和测试均符合本冻结文档。
- `pytest`、`ruff`、`git diff --check` 通过。
- 所有适用的 CC 清单项有确定性测试。
- 命中 HIL 触发矩阵时，对应真实 Windows/NI 证据已通过并归档。
- 新增结构化 event/receipt 有 schema、identity 和终态测试。

### 9.2 Epic 4 完成与实验室发布

Epic 4 的四个 Story 完成不自动等于实验室发布完成。发布候选还必须：

- 复核 PyInstaller 构建、资源清单、产物 hash 和打包后 Mock 启动；
- 完成协议、session、TTL、LOW_FLOW、DATA_STALE、disk failure、recovery、cleaning、config rollback 和 shutdown 中文操作指引；
- 记录未连接受试者、未证明机械阀完成、未执行破坏性故障测试等声明边界。

## 10. 变更控制

### Always

- 保持唯一 owner、immutable identity、receipt 驱动终态、fail-closed、显式恢复和事务式记录。
- 使用 `docs/sprint-artifacts/sprint-status.yaml` 作为唯一实施状态源。

### Ask First

- 改变 maintenance bundle schema、owner topology、flow/master/odor 顺序、lease 互斥、HIL 阈值或设备生产基线。
- 让 `timing_ms` 参与执行、增加新硬件、宣称机械时序或替换真实 HIL。

### Never

- 从 UI/Controller 直接访问 HAL、serial 或文件。
- 用 SAFETY 标记普通 open，或用 UI 禁用代替 service/owner gate。
- 用 stale receipt 推进状态机、自动清除 unsafe latch、自动续写失败 bundle。
- 用 Mock/平均值/重复运行代替确定性竞态测试或范围触发的 HIL。

任何 Ask First 或 Never 边界的改变，都必须新建 Sprint Change Proposal，更新 PRD/Epics/Architecture/UX 中受影响部分，并在批准前把受影响 Story 保持为 `backlog`。

</frozen-after-approval>
