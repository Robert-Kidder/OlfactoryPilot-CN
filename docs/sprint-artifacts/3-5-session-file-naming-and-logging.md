---
baseline_commit: e401a319d1da93302bcc8908fc9ed7d161b3da08
---

# Story 3.5: 会话文件命名与日志

Status: done

<!-- Note: 本 Story 已按 create-story checklist 完成上下文审查；实现阶段仍须逐项勾选并保留测试/HIL 证据。 -->

## Story

作为研究人员，
我需要在每次实验会话中自动生成成对的信号文件和结构化事件日志，
以便能够按受试者、条件、trial、触发来源和硬件动作回执追踪实验数据，并在磁盘异常后明确识别不完整数据。

## Acceptance Criteria

1. **会话开始是成对文件事务，也是协议运行前置条件**
   - Given 已加载一份有效协议，用户已填写受试者、条件并选择本地输出目录；
   - When 用户点击“开始会话”；
   - Then 系统生成一次不可变 `session_id`、一个规范化 stem，以及同 stem 的 `{stem}.raw`、`{stem}.log`；
   - And 两个文件与 `manifest.json` 必须全部在隐藏工作目录 `<output>/.<stem>.session.part/` 内成功独占创建后，会话才进入 `recording`；
   - And 任一创建失败时会话保持非活动，协议“开始”在 Controller/Service 边界被拒绝，不得只依赖按钮禁用；
   - And 最终发布路径固定为 `<output>/<stem>/<stem>.raw` 与 `<output>/<stem>/<stem>.log`。活动/失败会话只能位于名称明确含 `.session.part` 或 `recovery` 的目录，不得以最终会话目录冒充完成。

2. **Windows 文件名清洗与确定性碰撞处理**
   - Given FR2.1 的标准 stem 为 `{Timestamp}_{Subject}_{Condition}`；
   - Then `Timestamp` 在会话开始时只取一次本地墙钟，格式固定为 `yyyyMMdd-HHmmss-fff`；日志另存带 UTC offset 的 ISO-8601 时间；
   - And 用户需求中的 `*` 仅表示字段之间的占位关系，不是字面字符；Windows 禁止 `*`，实际分隔符沿用 FR2.1 的 `_`；
   - And `Subject`、`Condition` 的清洗依次执行：Unicode NFC、去首尾空白、把控制字符、Windows 非法字符 `< > : " / \ | ? *` 和连续空白替换为 `-`、折叠连续替换符、去尾部空格/句点；
   - And 对大小写不敏感的 `CON/PRN/AUX/NUL/COM1…COM9/LPT1…LPT9` 及 `COM¹/²/³/LPT¹/²/³`（含带扩展等价形式）加前缀 `_`；清洗后为空则拒绝开始并显示中文原因，不得默认为 `unknown`；
   - And 每个清洗后组件最多 64 个 Unicode 字符，完整绝对路径按 240 个 UTF-16 code unit 预算；超限时使用“可读前缀 + `-` + 原值 SHA-256 前 8 位”确定性截断，不得静默截成相同名称；
   - And 用户输入原值、清洗后值和最终路径必须在开始前预览，文件名不是自由编辑字段；受试者与条件的权威来源是文件页输入，不从任意 protocol/trial metadata 猜测；
   - And 碰撞不得覆盖旧数据，也不得使用易竞态的“先 `exists()` 再创建”。以独占创建 `<output>/.<candidate>.session.part/` 作为成对预留；若 candidate 或最终目录已存在，整对依次使用 `__001`、`__002`…，两文件始终共享同一后缀；达到 `__999` 后中文拒绝。

3. **`.raw` 固定为可追踪的 100 Hz 呼吸原始样本流**
   - Given Story 3.3/3.4 的 `HardwareWorker` 已从唯一 AI0/AI6 continuous task 产生不可变 `BreathSampleBatch`；
   - When 会话为 `recording`；
   - Then `.raw` 记录现有 100 Hz、校准前 AI0 呼吸 batch，不另开 NI task、不重新读取 HAL，也不把磁盘 I/O 放进 `HardwareWorker`；
   - And 文件编码为 UTF-8、换行固定为 `\n`，首行是以 `# ` 开头的 JSON metadata（至少 `schema=olfactorypilot.raw`、`schema_version=1`、`session_id`、列定义、名义采样率），随后是 CSV header 与数据行；
   - And 每个样本至少包含 `record_sequence,timestamp,monotonic_ns,ai_epoch,sample_sequence,ai0_raw`；`timestamp` 保留采集墙钟，`monotonic_ns + ai_epoch + sample_sequence` 保留 3.4 的采样身份；
   - And 不在本 Story 伪造 AI6 连续波形、NI 原生逐样本时戳或 `origin_uncertainty_ns`。TTL pulse 使用 3.3 既有采集时间与 identity 写入 `.log`；未来若需要 1 kHz AI6/旧系统二进制兼容，必须另行定义迁移/导出需求。

4. **`.log` 使用稳定、可版本化的 JSON Lines 契约**
   - Given 会话成功进入 `recording`；
   - Then `.log` 使用 UTF-8 JSONL，一行一个对象，`ensure_ascii=False`，每行以 `\n` 结束；
   - And 每条记录都含 `schema=olfactorypilot.event`、`schema_version=1`、`session_id`、严格递增 `session_sequence`、`event`、带 offset 的 `timestamp`、可用时的 `monotonic_ns`、`source`、`result`、中文 `message`；
   - And 第一条 `session_started` 快照至少包含：受试者/条件原值与清洗值、最终 stem、协议 source/metadata、当前 declared/current trigger mode、吸气/呼气/低流量阈值、硬件变体、simulation/real、AI epoch 可用状态、动作质量配置（target/single limit/window/min samples）；
   - And 会话中的模式或阈值变化必须写独立 change event，不能改写首条快照；最终 `session_closed` 写结束原因、开始/结束时间、样本/事件/receipt 数、队列高水位、丢失计数（成功会话必须为 0）及最终动作质量汇总。

5. **复用 Story 3.2–3.4 的结构化事件、receipt 与质量指标**
   - Given `ProtocolGateEvent.as_dict()`、frozen `ActuationReceipt` 与 `ActuationQualitySnapshot` 已存在；
   - When 门控、触发、trial、动作或质量事件产生；
   - Then recorder 直接适配结构化对象，不解析当前 Python logger 的格式化字符串，也不复制一套 ProtocolExecutor/metrics 状态机；
   - And 至少记录：会话开始时按当时时间合成的 `protocol_bound`、协议开始/暂停/恢复/停止/完成、manual/TTL accepted/ignored/rejected、`arm_epoch/pulse_sequence`、等待呼气、阈值越过、timeout/retry/skip、每个 trial 状态、readiness/safety block、rearm、shutdown；不得把会话建立前发生的加载动作伪造成历史 `protocol_loaded` 事件；
   - And 每个动作 receipt 是一等记录，完整保留 `command_id,execution_epoch,arm_epoch,sequence,trial_id,trial_index,valve,action,category,expected_ns,started_ns,actual_ns,offset_ms,jitter_ms,result,measurement_point,stale,actual_duration_ms,target_device,target_line,message`；
   - And `actual_ns` 的语义明确为 `daqmx_write_ack`，不得描述成机械阀物理完成；
   - And 质量记录至少包含 last jitter、open/close/combined p95、各自 sample count、warning/recovery transition、severe latch/ack 与失败/uncertain/cancelled receipt；
   - And receipt 的 canonical identity 为 `(session_id, execution_epoch, command_id)`。同一结构化 receipt 可能同时出现在 executor result 与 `receipt_ready` 时，只从 owner 线程的 canonical receipt ingress 持久化完整 receipt；
   - And 日志 schema 区分 `record_type=receipt/protocol_event/quality_event/session_event`：`receipt` 保存完整 timing 原事实；`protocol_event/quality_event` 只保存状态转换、质量摘要与 `command_id` 引用，必须剔除重复的 receipt timing 字段；
   - And 非 receipt 记录使用 owner 在首次发布时分配的 immutable envelope identity（至少 `session_id,session_generation,producer,producer_sequence,event_id`）。`event_id` 不得由 writer 临时 UUID 或 message 猜测生成；缺失/旧 generation 的 envelope 必须拒绝并报告，不能误归入下一会话。

6. **磁盘路径与低抖动/硬件所有权彻底隔离**
   - Given Story 3.4 closure 已固定 AI、DO、serial 和 executor 所有权；
   - Then 新增独立的单写者 `SessionWriterWorker`（或等价专用 I/O worker）独占 `.raw/.log/manifest` 句柄、序列号、hash 和发布状态；
   - And `HardwareWorker`/`ActuationWorker` 在各自 owner 线程直接调用线程安全、O(1)、非阻塞的 recorder ingress，把已绑定 `session_id/session_generation/producer_sequence` 的不可变 batch/event/receipt 放入同一有界 writer queue；跨线程 Qt signals 只用于 UI，不得作为持久化的必经队列；
   - And producer-facing `post_*` 只做 `put_nowait` 与最小身份校验，不做 JSON/CSV 序列化、flush、fsync、hash 计算、目录扫描或等待 writer lock/ack；
   - And 不得在 UI、`HardwareWorker`、`ActuationWorker`、`FlowWorker` 或 HAL 方法中同步写磁盘；
   - And `HardwareWorker` 继续独占唯一 AI task，`ActuationWorker` 继续独占 `ProtocolExecutor/GatingService/metrics` 与所有 DO session，`FlowWorker` 继续作为 serial/MFC 单写者；recorder 只观察不可变输出，不获得任何硬件引用，不向 owner 回写状态；
   - And 3.4 的 trigger/有效 AI batch 优先级、无损 batch 合并、deadline/紧急队列与 `daqmx_write_ack` 测量点不得改变。

7. **目录不可写、队列满与中途写入失败必须 fail closed**
   - Given 输出目录不存在、不是目录、为不支持的 UNC/网络路径、不可创建工作目录、raw/log/manifest 任一独占创建失败，或 write/encode/flush/fsync/close/hash/rename 任一步抛出错误；
   - Then 第一次失败立即将 recorder 锁存为 `failed`，停止接受新的普通记录，发出一次结构化内存错误信号，并在状态栏显示“发生了什么 + 下一步”的中文错误，例如“会话写入失败，请检查磁盘空间或目录权限；实验已停止记录，请执行安全停止后新建会话。”；
   - And 队列必须有配置化上限并记录高水位；队列满视为数据完整性失败，禁止静默丢 batch、event 或 receipt 后继续显示“记录正常”；
   - And recorder failure 必须先写入 producer-safe `recording_ready=False`/generation latch，再唤醒 `ActuationWorker`；worker/service 层统一拒绝 NORMAL/MANUAL/PRETEST/WARMUP，只允许 SAFETY/emergency close。Controller 同时通过既有 `ActuationWorker.post_stop()` 编排安全处理可能打开的阀，不能只靠 UI 状态或一次 stop message；
   - And failure latch 只有在旧会话已安全收敛、确认无 active/possibly-open valve、用户确认错误且新会话成功建立后才可由动作 owner 清除；recorder 不得直接操作 DO/serial/AI；
   - And emergency close、shutdown receipt 和用户可见错误不能因 recorder 失败而被阻断；同盘 `.log` 已不可用时，UI、内存状态和现有进程 logger 仅作 best effort 诊断，不得谎称失败事件已经持久化；
   - And 失败会话不可原地 resume/append；安全收敛后必须显式开始一个新会话。任何不完整数据只保留在 `.session.part`/`recovery`，最终 `<output>/<stem>/` 不得出现。

8. **关闭、同步与最终发布语义明确且幂等**
   - Given 用户显式结束、协议 `completed/stopped`、安全中止、reset、全局 stop 或应用退出；
   - When 关闭会话；
   - Then 先停止新普通提交，等待既有协议/安全关闭产生最终事件与 receipts；既有硬件关闭顺序保持“失效 normal epoch → 有界 emergency close → ActuationWorker/DO → HardwareWorker/AI → FlowWorker/serial”，session writer 在最终 shutdown event 后独立收尾；
   - And 每个 active producer 必须在停止为该 generation 提交后，向同一 ingress queue 写入带最后 `producer_sequence` 的 `producer_fence`；ActuationWorker 的 fence 位于该 execution 最后 event/receipt 之后，HardwareWorker 的 fence 位于最后 raw batch 之后，Controller 的 fence 位于最终 shutdown/session event 之后；
   - And writer 只有按序消费全部预期 fence 并返回 `finalization_ack` 后才能写 `session_closed`；禁止依据 UI snapshot、Qt signal 已返回或“队列暂时为空”推断最后 receipt 已到达；
   - And writer 排空 fence 前已接受的记录，写 `session_closed` 与最终指标，对 raw/log 依次执行 Python `flush()` → `os.fsync()` → close；随后把 count 与流式 SHA-256 写入同目录临时 manifest，执行 `flush()` → `os.fsync()` → close，再以 `os.replace()` 替换 recording manifest 为 `manifest.status=complete`；
   - And manifest 的 SHA-256 只覆盖最终 `.raw/.log` encoded bytes，不自包含 manifest hash；`raw_record_count` 只统计 CSV 数据行（不含 metadata/header），`log_event_count` 包含 `session_started` 到 `session_closed` 的全部 JSONL 记录，byte count 是各最终文件的实际字节数；
   - And 只有上述步骤全部成功，才把同一父目录中的 `.<stem>.session.part` 一次重命名为 `<stem>` 作为 pair commit point；不得逐个把 sibling `.part` 改成看似有效的最终文件；
   - And publish 前后不得覆盖现有最终目录；重命名失败保持 `.session.part` 并进入 recovery-required；
   - And `close()` 幂等：并发/重复调用返回同一结果，不重复写 `session_closed`、不重复发布、不改变已完成文件；关闭超时不阻塞硬件安全释放，但会话不得标记完成。

9. **崩溃恢复只隔离与报告，不自动冒充完成或续写**
   - Given 应用启动或用户选择输出目录时发现 `.*.session.part`、缺失/非 complete manifest、hash/count 不一致的会话目录；
   - Then recovery scanner 只读验证，记录原因，并尝试原子移动到 `<output>/recovery/<stem>__incomplete__<timestamp>/`；移动失败则原地保留 `.session.part`；
   - And UI 用中文列出发现位置、失败阶段/最后成功序号（若可得）和“打开恢复目录/另存分析”的下一步，不自动追加旧文件、不自动改为完整、不静默删除；
   - And recovery/quarantine 操作幂等；同一目录重复扫描不得生成无限副本；
   - And 只有 manifest 为 complete、两文件存在且 count/hash 验证通过的最终目录才可在应用内显示为“完整会话”。不完整数据可供人工只读取证，但不属于成功 FR2.3 输出。

10. **中文文件页与状态反馈**
    - Given 主窗口当前尚无文件页；
    - Then 新增“文件”页，包含受试者、条件、本地输出目录、规范化文件名/最终路径预览、会话状态、开始/结束会话与恢复提示；
    - And 输入变更实时刷新清洗后预览，但活动会话的 `session_id/stem/路径/协议绑定` 不可变；
    - And 按钮 capability 同时消费 session state 与 protocol state：无有效协议或 recording 未建立时不能开始协议；活动动作未安全收敛时不能直接发布/切换会话；
    - And 所有错误、警告和恢复说明为简体中文，复用现有状态栏；颜色仅辅助，不以颜色代替状态文字。

11. **会话边界继承 3.2–3.4，不重写执行语义**
    - Given 一次会话绑定开始时的唯一 `ProtocolDocument`；
    - Then 活动会话内禁止替换协议；用户必须先按既有安全路径停止/结束会话，才能加载另一协议。协议替换清理失败时保留旧 document/trial/mode/active valve 与当前失败会话，不能显示候选协议已生效；
    - And pause/resume/rearm 保持同一 session、当前 trial、execution identity 与质量窗口；不会关闭、重建或碰撞生成新文件；
    - And completed/stop 会终结当前 session；restart 必须先成功创建新 session，且 3.4 metrics 只在新 protocol start 成功后按既有规则 reset；
    - And `BLOCKED` 可在同一会话内显式 rearm；磁盘失败例外，记录不可恢复，必须安全 stop 并新建会话；
    - And frozen `ProtocolDocument/ProtocolTrial/ProtocolGateEvent/ActuationReceipt/BreathSampleBatch` 不得为记录方便改成可变对象。

12. **自动化、Windows 故障注入与低抖动证据**
    - Given 不连接真实硬件的 CI；
    - Then 使用 `tmp_path`、fake clock、可注入 filesystem/writer adapter、barrier/event 覆盖命名、清洗、碰撞竞争、schema、顺序、去重、队列背压、close 幂等、恢复与所有失败阶段；Windows 不可写测试不能只依赖 `chmod`；
    - And 覆盖 raw 成功/log 失败、log 成功/raw 失败、header/write/flush/fsync/close/manifest/rename 失败，任何场景都不得出现成功最终目录；
    - And 覆盖 session start 与协议 start gate、pause/rearm、stop/completed、协议替换清理失败、global stop/reset/exit、shutdown receipt 在 recorder failure 后仍可处理；
    - And 3.2–3.4 的 protocol/TTL/gating/actuation/flow/shutdown 测试保持通过，ruff 与 `git diff --check` 通过；
    - And 用慢盘/队列压力自动化证明 recorder 不阻塞 producer，`session_sequence` 完整或明确 fail closed，不用毫秒级真实 sleep 作为唯一并发断言；
    - And 在真实 Windows/NI 环境、实际 session recording 与 UI/logging 并发负载下重新运行 3.4 的 200 open + 200 close HIL 门禁：400/400 receipt success、open/close/combined p95 及 rolling/final-last-100 均严格 `<20ms`，stop/LOW_FLOW/severe/shutdown 均完成全部配置目标关闭。该 HIL 需要用户明确授权；证据未完成前 Story 不得置 `done`。

## Tasks / Subtasks

- [x] Task 1：建立会话模型、状态机与命名服务（AC: 1, 2, 8, 9, 11）
  - [x] 新增 `app/models/session.py`：frozen `SessionDescriptor/SessionPaths/SessionRecordEnvelope/ProducerFence` 与受控 `SessionState`；envelope 固定 session generation、producer sequence 与 event identity，运行态不得写回 `ProtocolDocument`。
  - [x] 新增 `app/services/session_file_service.py`：NFC 清洗、Windows 保留名/路径预算、hash 截断、原子 staging 目录预留、`__001…__999` 碰撞、manifest 与 recovery scanner。
  - [x] 固定一次会话绑定一份协议；定义 start/close/fail/recover 幂等转换及不可变路径。
  - [x] 使用同父目录 staging bundle → final bundle 的单目录 publish，不实现逐文件“伪原子”提交。

- [x] Task 2：实现专用 session writer 与稳定文件 schema（AC: 3–9）
  - [x] 新增 `app/workers/session_writer.py`，作为唯一文件句柄 owner；有界 queue、单调 sequence、producer fence/finalization ack、流式 SHA-256、周期 flush、close fsync、一次性 failure latch。
  - [x] `.raw` 写现有 100 Hz `BreathSampleBatch` 的校准前 AI0 与采样 identity；`.log` 写版本化 JSONL。
  - [x] 为 `ProtocolGateEvent`、`ActuationReceipt`、quality snapshot、阈值/模式/安全/shutdown 事件写显式 adapter；receipt/event/quality schema 分离，不反解析 logger 字符串。
  - [x] 以 ActuationWorker owner 线程 direct ingress 为 canonical receipt 来源，按 `session_generation + execution_epoch + command_id` 关联/去重；Qt signal 只服务 UI。
  - [x] 为队列满与每个 filesystem 阶段提供可注入失败点和中文结构化结果。

- [x] Task 3：接入 Controller、owner direct ingress 与会话边界（AC: 1, 5–8, 11）
  - [x] 更新 `app/controllers/main_controller.py`：创建/启动/结束 recorder，协议 start gate，活动会话禁止换协议，磁盘失败转既有安全 stop，最终 shutdown event 后收尾。
  - [x] 在 HardwareWorker/ActuationWorker owner 线程用 `put_nowait` 直投 immutable envelope；保持 AI batch 先交 ActuationWorker，再投 recorder，Qt signals 仅供 UI，不把 recorder 插入 deadline/DO/AI/serial ownership。
  - [x] 将 recorder readiness/generation 纳入 producer-safe interlock latch，覆盖 NORMAL/MANUAL/PRETEST/WARMUP 并放行 SAFETY；实现保守清除门禁。
  - [x] 为 stop/completed/reset/exit 实现多 producer fence 与 writer `finalization_ack`，避免在最后 event/receipt/raw batch 入队前关闭日志。
  - [x] 保持现有 Python runtime logger 供诊断，但 session `.log` 只消费结构化对象。
  - [x] `HardwareWorker.flush_logs()`/HAL no-op 不能被当作 session durability；session close 由独立 writer owner 负责。

- [x] Task 4：增加中文文件页与状态渲染（AC: 1, 2, 7, 9, 10）
  - [x] 新增 `app/views/session_view.py`，只发出 subject/condition/output/start/end/recovery 意图并渲染 snapshot。
  - [x] 更新 `app/views/main_window.py` 增加“文件”页，连接 Controller，复用状态栏。
  - [x] 路径预览显示 staging/最终语义，错误说明发生事项与可执行下一步；颜色不作为唯一信息。
  - [x] 更新 `AppState` 或独立 session snapshot；不得让 View 自行生成 stem、探测磁盘或推进 session。

- [x] Task 5：配置、生命周期与文档（AC: 6–12）
  - [x] 更新 `config/default_config.json`：经验证的 writer queue/flush/close timeout 默认值；配置无效时采用安全默认并记录中文 warning。
  - [x] 更新 `app/main.py`/Controller 生命周期；优先复用 `ShutdownService.shutdown()` 已返回的最终结构化事件，不修改其硬件关闭链。只有现有返回契约无法满足 finalize barrier 时才做最小扩展，并证明 owner 固定关闭顺序不变。
  - [x] 更新 `app/models/__init__.py`、`app/services/__init__.py`、`app/workers/__init__.py`、`app/views/__init__.py` 的必要导出。
  - [x] 实现后同步 `docs/architecture.md`、`docs/project-structure.md`，记录 session bundle、writer owner、数据流与恢复语义。
  - [x] 不新增第三方依赖；使用 Python 3.11 标准库 `pathlib/os/json/csv/hashlib/unicodedata/uuid`。

- [x] Task 6：先写失败测试，再完成回归与 HIL（AC: 1–12）
  - [x] 新增 `tests/test_session_file_service.py`、`tests/test_session_writer.py`、`tests/test_session_view.py`。
  - [x] 扩展 `tests/test_app.py`、`tests/test_protocol_trigger_integration.py`、`tests/test_actuation_worker.py`、`tests/test_shutdown_actuation.py`，覆盖会话 gate、旧 generation 拒绝、failure latch、producer fence/最后 receipt、关闭顺序和低抖动隔离。
  - [x] 添加多线程碰撞、慢盘、队列满和全阶段 filesystem fault injection；不用真实磁盘填满或破坏用户目录。
  - [x] 运行定向测试、全量 pytest、`python -m ruff check .`、`git diff --check`。
  - [x] HIL 条件门禁已遵守：仅在获得用户明确授权后才在真实 Windows/NI 环境运行；本轮未获授权，按用户要求未运行，也不声明 3.5 session writer 负载下的 HIL 证据。

- [x] Task 7：修复 2026-07-27 code review findings 并完成确定性 teardown（AC: 1–12）
  - [x] [AI-Review][High] recovery scanner 只隔离具有 Story 3.5 bundle 身份的目录，不移动仅含任意 `manifest.json` 的无关目录。
  - [x] [AI-Review][High] writer finalize 与 close timeout 使用单一终态仲裁；timeout 后禁止继续发布或覆盖失败终态。
  - [x] [AI-Review][High] queue-full failure 通知移出 ingress/Actuation 锁，消除同步 callback 锁反转。
  - [x] [AI-Review][High] session 进入 `CLOSING` 后在 owner/service 边界拒绝 MANUAL/PRETEST/WARMUP。
  - [x] [AI-Review][High] 异步 master-prepare 完成后重新校验 session identity/generation、document 与 recording readiness。
  - [x] [AI-Review][High] controller fence 成功提交后，stop/reset/exit 不再追加 shutdown/session event。
  - [x] [AI-Review][High] unsafe shutdown、possibly-open 或 owner-handoff 失败不得发布 complete bundle。
  - [x] [AI-Review][High] failed writer 完全终止后才允许新 generation；Actuation recorder bind/unbind 在 owner 线程串行化。
  - [x] [AI-Review][High] shutdown payload 不得覆盖 canonical timestamp/schema/session identity 等保留字段。
  - [x] [AI-Review][High] preview 与 reserve 复用同一时间样本，实际创建路径与用户预览一致。
  - [x] [AI-Review][Medium] manifest 验证 schema/version/identity、basename containment，并隔离非法 UTF-8/JSON/路径异常。
  - [x] [AI-Review][Medium] CLOSED/FAILED 后的新 writer 初始化失败写入新的 session failure state。
  - [x] [AI-Review][Medium] `SessionRecordEnvelope` 对嵌套 payload 做深层冻结。
  - [x] [AI-Review][Medium] `session_closed` 在 rename 前不声称已经发布。
  - [x] [AI-Review][Medium] Controller 不越过 Actuation owner 线程读取 `metrics.snapshot()`。
  - [x] [AI-Review][Medium] CLOSED/FAILED UI 不回退旧 preview，协议加载等状态变化主动刷新 session capability。
  - [x] [AI-Review][Medium] recovery scan 异步执行、缓存可失效，并在 UI 显示 `last_sequence`。
  - [x] [AI-Review][Medium] 增加 slow-finalize timeout、真实 writer failure、无关 manifest、非法 UTF-8、活动 writer/scanner 并发及确定性 teardown 覆盖。

- [x] Task 8：处理 2026-07-29 独立 code review findings（AC: 1–12）
  - [x] [AI-Review][High] 会话开始、结束和 recorder bind 必须等待 pending protocol start、master-prepare、MANUAL/PRETEST plan 及 `ValveService` master/manual valve 状态安全收敛；晚到的成功 master-prepare 必须补偿安全关闭，不能只拒绝协议启动。 [`app/controllers/main_controller.py:1014`, `app/controllers/main_controller.py:1287`, `app/controllers/main_controller.py:2310`]
  - [x] [AI-Review][High] Actuation `recorder_fence` 不得越过更早排队的 `ai_batch`、`ttl_pulse`、普通消息、动作或 receipt；fence 必须确定性位于该 generation 最后 event/receipt 之后。 [`app/workers/actuation_worker.py:1132`, `app/controllers/main_controller.py:1354`]
  - [x] [AI-Review][High] finalization 已开始后发生 reset/stop/app-exit/global shutdown 时，unsafe shutdown、possibly-open 或 owner-handoff 失败仍必须取消 complete 发布。 [`app/controllers/main_controller.py:411`]
  - [x] [AI-Review][High] `SessionWriterWorker.close(timeout)` 必须严格有界；不得持 `_state_lock` 执行可能阻塞的 `exists()`/rename，也不得在 timeout 已发生后发布成功。 [`app/workers/session_writer.py:607`, `app/workers/session_writer.py:983`]
  - [x] [AI-Review][High] 正常 session finalizer 必须被正确跟踪，预检线程不得写入 `_session_finalize_thread`；finalizer 对 session state、interlock、result 和 event 的更新必须校验 `session_id/generation`，不能覆盖新会话。 [`app/controllers/main_controller.py:685`, `app/controllers/main_controller.py:1377`, `app/controllers/main_controller.py:1391`]
  - [x] [AI-Review][High] 生产窗口退出必须确定性停止 QTimer、Recovery QThread、SessionWriter、finalizer 及 Actuation/Hardware/Flow owner；RecoveryScanWorker 必须可取消，timeout 后不得丢弃仍运行线程的最后引用。 [`app/controllers/main_controller.py:80`, `app/controllers/main_controller.py:382`, `app/main.py:174`]
  - [x] [AI-Review][High] `SessionFileService.reserve()` 与 recovery scan 的 active-staging 登记必须原子协调；writer 初始化 timeout 后只有在线程确实终止时才能 `mark_inactive()`。 [`app/services/session_file_service.py:195`, `app/services/session_file_service.py:387`, `app/controllers/main_controller.py:1093`]
  - [x] [AI-Review][High] recovery scanner 只能移动具有完整 OlfactoryPilot session bundle 身份的目录；不得因为目录中恰有同名 `.raw/.log` 或普通 `.session.part` 名称就移动无关用户目录。 [`app/services/session_file_service.py:399`]
  - [x] [AI-Review][Medium] Timestamp 必须来自实际开始会话时刻且只采样一次；碰撞后的 `__001…__999` 实际最终路径必须与用户确认的预览一致。若“实际开始时刻”与“开始前最终路径预览”需要产品决策，须先 HALT 询问，不得自行弱化 AC。 [`app/services/session_file_service.py:123`, `app/controllers/main_controller.py:1024`]
  - [x] [AI-Review][Medium] CLOSED/FAILED 后用户修改输入时必须显示新 preview；按钮使用的 preview、屏幕显示和实际 reserve 路径必须一致。 [`app/controllers/main_controller.py:1447`]
  - [x] [AI-Review][Medium] 本地路径验证必须拒绝映射网络盘或解析到网络位置的路径，不能只识别字面 UNC。 [`app/services/session_file_service.py:552`]
  - [x] [AI-Review][Medium] `RecorderReadinessLatch` 的 fail/close/ready 更新必须带 `session_id/generation` 条件；迟到旧 ingress 不得击穿新 generation。 [`app/workers/session_writer.py:134`, `app/workers/session_writer.py:408`]
  - [x] [AI-Review][Medium] complete bundle 验证应流式计算 hash/count/JSONL identity，避免多次整文件读取和构造全部记录列表。 [`app/services/session_file_service.py:334`, `app/services/session_file_service.py:367`]
  - [x] [AI-Review][Gate] 为上述交错逐项先增加稳定失败测试，再实施修复；使用 Event/Barrier/fake filesystem，不以毫秒级 sleep 作为唯一并发断言。完成后运行 Story 定向测试、全量 `python -m pytest`、`python -m ruff check .` 及包含未跟踪文件的 `git diff --check`；仅使用 MockHAL/fake/fault injection，不运行或声称运行真实 NI HIL。

- [x] Task 9：处理 Task 8 完成后的独立复审 findings（AC: 1–12）
  - [x] [AI-Review][High] MainController 的 session boundary 必须等待 `_pending_protocol_load`；迟到的成功 document load 回执不得在 PREPARED/RECORDING 后替换活动会话协议。
  - [x] [AI-Review][High] `ActuationWorker.bind_session_recorder()` 超时后必须取消或失效化已排队的 `recorder_bind`，迟到命令不得绑定失败 ingress 或阻塞下一 generation。
  - [x] [AI-Review][High] recovery scan completion 必须以 worker identity 绑定；旧 scan 的回调不得对新 `_recovery_scan_worker` 执行 wait/deleteLater 或清空其引用。
  - [x] [AI-Review][High] `scan_recovery()` 不得在 `_active_lock` 内执行完整文件验证；raw/log/manifest 的真实验证循环必须响应 cancel，确保 reserve 与 teardown 有界。
  - [x] [AI-Review][Medium] create_log/create_manifest/manifest 部分写入失败后，即使 manifest 无效，本程序创建的 `.session.part` 也必须被 recovery scanner 识别、报告并隔离，同时继续拒绝无关用户目录。
  - [x] [AI-Review][Medium] complete bundle 验证必须拒绝空白 JSONL 行，严格执行“一行一个对象”契约。
- [x] [AI-Review][Decision] 采用方案 A：强制应用单实例，以操作系统级、崩溃后可释放的 ownership 防止第二个实例隔离首个实例的 PREPARED staging；第二个实例显示中文提示并安全退出。
- [x] [AI-Review][Gate] 对每个交错先写稳定失败测试再修改实现，使用 Event/Barrier/fake filesystem/clock 与 fault injection，不以毫秒级 sleep 作为唯一断言；覆盖 Controller、QTimer、QThread、SessionWriter、finalizer、窗口 teardown 及 generation/session 隔离。完成后运行 Story 定向测试、全量 `python -m pytest`、`python -m ruff check .`，以及相对 baseline、包含全部未跟踪文件的 `git diff --check`；仅使用 MockHAL/fake，不运行或声称运行真实 NI HIL，Story 与 sprint-status 保持 `review`。

- [x] Task 10：处理 2026-07-30 真实 Windows/NI HIL 失败后的 review continuation（AC: 5, 6, 8, 9, 12）
  - [x] [HIL][Evidence] 登记失败 candidate `c9baff6e6910266621fb2a36c6b62f880f42a27e` 与只读运行目录 `logs/benchmarks/story-3-5-20260730-154234-live`；首个正式动作 `protocol-9-open-1` jitter 为 `33.0973 ms`，超过 `30 ms` severe limit，随后 severe safety close 将 21 个配置目标全部关闭并按要求中止。仅取得正常 open `1/200`、close `0/200`，因此 `400/400` receipt success 与 open/close/combined rolling/final-last-100 p95 Gate 均未完成。
  - [x] [HIL][High] 在不得排除首个正式样本、增加未声明 warm-up、降低阈值、修改统计口径或挑选成功 run 的前提下，定位并修复首个正式 deadline 超限的真实原因；重点审计 session recorder bind、protocol load/cleanup、AI batch backlog、UI/logging 并发及首个正式动作调度顺序，并以现有 latency trace 与确定性 owner/queue 测试建立 RED→GREEN 证据。
  - [x] [HIL][High] 统一 writer、receipt schema 与 complete-bundle validator 的主阀契约：logical `valve=0` 仅对配置的主阀动作合法，不得无条件放宽其他非法 valve；增加真实 writer round-trip、complete-bundle validator 回归及本次中止 bundle 的只读验证测试。当前证据的 raw/log SHA-256、byte、count、session sequence、`dropped_count=0` 与 producer fences 均匹配 manifest，但 validator 返回“receipt valve 无效。”。
  - [x] [HIL Tooling][Medium] 审计并补齐 `scripts/hil_actuation_benchmark.py` 的 Story 3.5 recording 模式，使正式入口可原生建立 `SessionWriter`、绑定 hardware/actuation producer、显示 UI、收集完整 fences、验证最终 bundle；保留明确授权参数、真实/模拟标识、初始全关和中止后全关，且任何中止 run 不得继续后续安全场景，不依赖一次性 inline harness。
  - [x] [HIL][Automated Gate] 先写稳定失败测试，再实施最小修复；增加首个正式动作调度/积压的确定性测试且不以真实 sleep 为唯一断言。完成 Story 定向测试、全量 `python -m pytest -q`、`python -m ruff check .` 与 `git diff --check`。本轮只允许 MockHAL/fake/只读证据验证，不连接或操作真实硬件、不运行真实 NI HIL。
  - [x] [AI-Review][High] canonical identity 相同的重复 receipt 只有在完整 canonical 内容一致时才允许去重；valve/action/category/target/result 等任一冲突必须使 writer fail closed，validator 不得发布 complete。
  - [x] [AI-Review][High] Story 3.5 runner 的 SessionWriter 首次 failure 必须按 MainController 契约先失效 recording interlock 并直接唤醒 ActuationWorker，立即阻断 NORMAL/MANUAL/PRETEST/WARMUP，只允许安全关闭。
  - [x] [AI-Review][High] 正式 benchmark 必须在进入四个安全场景前判定 aggregate、全部 rolling、final-window p95 与样本完整性 Gate；任一失败立即中止并安全全关，不得继续后续场景。
  - [x] [AI-Review][High] live Story 3.5 `--candidate-commit` 必须解析为存在的 Git object、精确等于当前 HEAD 且 worktree/index clean；simulation smoke 保留明确 simulation 语义，不得冒充正式 candidate evidence。
  - [x] [AI-Review][Medium] 统一生产计划与 writer/validator 的 master_prepare 契约：合法 MANUAL/PRETEST 对配置主阀 target 的 `valve=0` receipt 可记录；错误 target、非法 action/category 与其他 `valve=0` 继续 fail closed。
  - [x] [AI-Review][Medium] Story 3.5 runner 必须保持单 session 单绑定协议，安全场景不得在 benchmark session 内加载不同 ProtocolDocument；session_closed 最终质量必须固定为正式 benchmark 质量，不得被最后一个安全场景的 metrics reset 覆盖，同时保留 canonical receipts、producer fences 与 validator。
  - [x] [HIL][Evidence] 登记失败 candidate `7c4d971aa370056b8a70cee3592344bb54dd7ad7` 与只读正式运行目录 `logs/benchmarks/story-3-5-20260730-183616-live`：正常 open/close 各完成 `68/200`，已有 136 条正常 receipt 均为 success；`protocol-9-close-1000283`（trial `bench-0068-v9`、valve 9 close）jitter `30.3166 ms` 超过 severe limit 后中止，未继续剩余正常动作及四个安全场景。中止 bundle writer/validator 均 complete、`dropped_count=0`、queue high-water=5、sequence `1..750`、三 producer fences 与 raw/log hash/byte/count 均一致。初始 finding 报告 severe 后缺少完整 emergency-close；后续只读逐序复核确认 sequence `728..748` 实际保存了同 run 的 21 条 `shutdown-close` success，且早于 actuation fence/controller event/`session_closed`，但 close-severe owner 分支自身没有生成 `severe-close`，安全收尾依赖 finally 通用 shutdown 补救。事后独立 close 目录 `logs/benchmarks/story-3-4-20260730-183741-live` 仅作额外安全佐证，不替代本 run 验收。
  - [x] [HIL][High] 使用现有 latency trace 定位并修复 `protocol-9-close-1000283` 的 `30.3166 ms` 超限根因，重点审计 UI、SessionWriter、AI batch、owner queue 与 DAQ write 调度；不得删除样本、改变统计口径、降低 severe limit、增加未声明 warm-up 或挑选成功 run。
  - [x] [HIL][High] 修复 severe abort → emergency close → receipt drain → producer fence → bundle finalize 顺序；正式 run 必须有界等待并在自身 bundle 持久化全部 21 个配置目标关闭回执，任何中止均不得继续后续安全场景，也不得依赖事后独立 close。
  - [x] [HIL][Automated Gate] 为延迟超限和中止全关顺序先增加确定性 RED 测试，再做最小修复；完成 Story 定向测试、全量 `python -m pytest -q`、`python -m ruff check .` 与 `git diff --check`，仅使用 MockHAL/fake/只读证据，不连接或操作真实硬件、不运行真实 NI HIL。
  - [x] [HIL][Acceptance] 以新的 40 位 candidate commit 在真实 Windows/NI 上从正式 `scripts/hil_actuation_benchmark.py --story-3-5-recording` 入口完成单次预声明验收：正常 open/close 各 `200/200`、全部 rolling/final-last-100 p95 Gate、四个安全场景、完整 producer fences 与最终 bundle validator 均通过；失败或中止不得继续后续安全场景。通过前 Story、Task 10 与 sprint-status 保持 `review`。

### Review Findings — candidate `e37578d15fc4eeb3679d08909d861faf9deac67f` 独立复审

- [x] [Review][Patch][High] 到期 NORMAL OPEN 不得越过已排队的 pause/mode/load 安全转换；deadline reservation 只应赋予 CLOSE 越过非安全消息的优先权。 [`app/workers/actuation_worker.py:1227`]
- [x] [Review][Patch][High] HIL teardown 必须检查 Actuation/Hardware/Flow owner 停止与资源释放结果；任一失败须先锁存 writer failure，禁止 fence 齐全后发布 complete bundle。 [`scripts/hil_actuation_benchmark.py:1340`]
- [x] [Review][Patch][Medium] close-severe 全目标关闭命令必须保留触发 severe 的原 trial identity，不得在 executor 推进后错误归属到下一 trial。 [`app/workers/actuation_worker.py:2276`]
- [x] [Review][Patch][Medium] latency trace 的 command→trial 登记、归属选择与 terminal 清理必须覆盖并发 submit、跨 trial 执行及 CANCELLED/rejected receipt，避免错误归因、漏 trace 和残留映射。 [`scripts/hil_actuation_benchmark.py:220`]

### Review Findings

- [x] [Review][Patch][High] Actuation owner 的协议启动与 NORMAL 动作门禁必须直接检查 `recording_ready`，不能只依赖 Controller、`recorder_failed` 或 `session_closing`，否则内部/迟到 intent 可绕过会话前置条件。 [`app/workers/actuation_worker.py:70`]
- [x] [Review][Patch][High] recovery 对 manifest/ownership marker 仍使用不可取消的整文件 `read_bytes()`，raw/log 的超大无换行记录也会在检查 cancel 前整行读入；必须采用有大小上限、可逐块取消的读取，保证 teardown 有界。 [`app/services/session_file_service.py:309`]
- [x] [Review][Patch][High] Qt event loop 返回后会无条件释放单实例 mutex，但 teardown 可在 writer/finalizer/owner 仍存活时超时返回；mutex ownership 必须覆盖旧进程实际持有文件/硬件 owner 的完整生命周期。 [`app/main.py:294`]
- [x] [Review][Patch][High] 最终目录只按 manifest 识别，随 bundle 保留的严格 ownership marker 在非 staging 目录被忽略；本程序最终目录缺失/损坏 manifest 时必须被 recovery 报告和隔离。 [`app/services/session_file_service.py:511`]
- [x] [Review][Patch][Medium] complete bundle 验证过浅：应验证固定 raw CSV header/行结构、JSONL 稳定字段与首尾生命周期事件、成功会话 `dropped_count=0`，并严格拒绝 bool/float 冒充整数 schema/count/sequence。 [`app/services/session_file_service.py:319`]
- [x] [Review][Patch][Medium] `session_started.timestamp` 与 `session_closed.started_at` 仍使用第一次锁定路径的时间，而不是二次确认时已有的 `recording_started_at`；会话持续时间应以实际开始记录时刻为准。 [`app/workers/session_writer.py:738`]
- [x] [Review][Patch][Medium] TTL pulse 已有的 `monotonic_ns` 未进入持久化 envelope，导致 TTL 事件无法与 raw/receipt 的单调时间轴关联。 [`app/workers/actuation_worker.py:1334`]
- [x] [Review][Patch][Medium] 质量 warning/recovery transition 只保留在中文 message 中，且 quality event 的 `result` 只看 combined warning；应持久化 stream、进入/恢复方向，并综合 open/close/combined/severe 状态。 [`app/workers/session_writer.py:885`]
- [x] [Review][Patch][Medium] 两步确认进入 PREPARED 后输入与“结束会话”都被禁用，用户发现路径/身份错误时没有取消路径；需提供不会冒充完成的显式取消/恢复语义。 [`app/views/session_view.py:124`]
- [x] [Review][Patch][Medium] recovery quarantine 移动失败时 UI 仍只尝试打开 `<output>/recovery`，无法打开实际保留的原 `.session.part`；恢复动作应携带并打开真实位置。 [`app/controllers/main_controller.py:1377`]
- [x] [Review][Patch][Medium] Windows 组件清洗只替换 U+0000–U+001F，仍保留 DEL 与 C1 control characters；应按 Unicode 控制字符语义完成替换。 [`app/services/session_file_service.py:21`]
- [x] [Review][Patch][Medium] collision 分支中的 `staging.rmdir()` 异常未转换为 `SessionFileError`，会从 Qt slot 泄漏原始 `OSError` 并留下无 marker、recovery 无法识别的 orphan staging。 [`app/services/session_file_service.py:228`]
- [x] [Review][Patch][Medium] recovery worker 已结束但 completion 尚未派发时，新扫描会覆盖 worker 引用却保留更早的 pending output；新扫描完成后可能反向启动旧目录扫描并覆盖最新 UI 结果。 [`app/controllers/main_controller.py:1333`]
- [x] [Review][Patch][High] 会话开始缺少 producer cutover barrier：`recorder_bind` 作为 priority message 会越过更早排队的 `ai_batch`/`ttl_pulse`，Hardware 也可在一次 AI 读取已开始、`_record_raw_batch()` 尚未执行时被 Controller 直接切换 recorder；会话建立前捕获的 raw/TTL 因而可能被归入新 generation。Actuation 与 Hardware 都必须在 owner 线程完成有 ack 的顺序切换后才能进入 `recording`。 [`app/workers/actuation_worker.py:1158`]
- [x] [Review][Patch][High] Actuation owner 的 `recording_ready` 动作门禁只覆盖 `NORMAL`，`WARMUP` 开阀在未建立 recording 会话且 recorder 未 failed/closing 时仍会真实执行；协议主阀预备类别不得只依赖 Controller gate，owner/service 边界也必须拒绝。 [`app/workers/actuation_worker.py:70`]
- [x] [Review][Patch][High] publish rename 后若 timeout/failure 抢先，final→staging 回滚 rename 再次失败时，writer 返回 recovery-required，但带 complete manifest 的最终目录仍保留；validator 会认定 complete，recovery scanner 也不会隔离。回滚失败必须留下可识别的不完整语义，绝不能让失败会话占用成功最终路径。 [`app/workers/session_writer.py:1059`]
- [x] [Review][Patch][High] 单实例 mutex 仅在 Qt event loop 正常返回后检查 `lifecycle_stopped()`；`window.show()`、`qt_app.exec()` 或返回后的检查抛异常时，`finally` 仍按默认值释放 guard，即使 Controller 的 writer/QThread/硬件 owner 尚存活。异常路径也必须执行同一 owner-liveness 仲裁并保留 mutex。 [`app/main.py:295`]
- [x] [Review][Patch][High] PREPARED 独占创建的 raw/log 在二次确认前可能被外部进程或并发故障写入；writer 当前以 `r+b` seek 到末尾追加 header，hash/byte counters 却只覆盖本次追加，最终仍报告 complete 并发布一个无法通过自身 validator 的 bundle。初始化必须原子确认保留文件仍为空，否则 fail closed。 [`app/workers/session_writer.py:723`]
- [x] [Review][Patch][High] writer 可生成任意长度 JSONL，但 recovery/validator 对单行强制 1 MiB 上限；超大 protocol metadata/session payload 会让本程序刚发布且报告 complete 的 bundle 在下一次扫描中被自动隔离。写入侧与验证侧必须共享同一上限并在发布前 fail closed。 [`app/workers/session_writer.py:1164`]
- [x] [Review][Patch][Medium] raw complete validator 仍接受仅含合法 metadata、缺失固定 CSV header 的零样本文件，也不检查跨行 `ai_epoch/sample_sequence/monotonic_ns` 的重复或倒退；writer 同样未在 ingress 侧拒绝重复 identity。必须流式验证 header 存在及采样身份/单调时间顺序。 [`app/services/session_file_service.py:530`]
- [x] [Review][Patch][Medium] JSONL complete validator 只检查通用字段，未知 `record_type`、缺少 receipt/quality 专属字段、重复 `event_id`、producer sequence 重复/倒退，以及 manifest/`session_closed` 的 receipt/sample/event/queue 计数矛盾仍可被判为 complete；ownership marker 与 manifest 身份也未交叉核对。必须按 record type 严格验证 schema、envelope identity 和汇总计数。 [`app/services/session_file_service.py:640`]
- [x] [Review][Patch][Medium] TTL 持久化只覆盖合法正整数路径；`TtlPulse.monotonic_ns` 为 0/负数时仍可能生成不可关联事件，为 `None`/不可转换值时 `int()` 会从 owner handler 抛出并终止 ActuationWorker。owner 必须在接受前严格验证 timestamp/epoch/sequence/monotonic identity，并把非法 pulse 结构化拒绝而非抛出。 [`app/workers/actuation_worker.py:1360`]
- [x] [Review][Patch][Medium] quality adapter 仅把 `actuation_receipt` 转成 `quality_event`，`quality_acknowledged/quality_ack_rejected` 仍落为无 snapshot 的 protocol event；同时 quality event 丢弃已有 receipt/event timestamp 与可用 `actual_ns`，改用新的 wall clock 且 `monotonic_ns=None`。必须按 quality schema 持久化 severe latch/ack 转换并保留原始时间身份。 [`app/workers/actuation_worker.py:387`]
- [x] [Review][Patch][Medium] collision cleanup 的 `rmdir()` 失败后虽尝试补写 ownership marker，但 marker 创建再失败会被吞掉，仍可能留下无 marker/manifest、recovery scanner 永远忽略的 orphan `.session.part`。二次失败必须保留可识别 ownership 或返回能被后续扫描发现的明确恢复记录。 [`app/services/session_file_service.py:337`]
- [x] [Review][Patch][Medium] 输出根目录的本地性只在入口检查；recovery 枚举到的子目录若是指向网络盘/UNC 的 junction、symlink 或其他 reparse point，仍会跟随并读取 marker/raw/log，从而绕过 v1 本地路径禁令，且网络 read 阻塞时 cancel 无法保证 teardown 有界。扫描前必须拒绝或隔离解析到网络位置的 child。 [`app/services/session_file_service.py:818`]
- [x] [Review][Patch][Medium] writer/validator 仍接受 Python JSON 扩展值 `NaN`/`Infinity`，且 `session_started_payload` 在固定生命周期字段之后展开，可覆盖 `event/timestamp/producer` 等 canonical 字段；这会产生其他严格 JSONL 消费者无法解析或身份被伪造的“complete”日志。序列化必须 `allow_nan=False`，所有 adapter 都必须剥离 canonical 字段。 [`app/workers/session_writer.py:750`]

### Review Findings — 2026-07-30 修复后独立复审

- [x] [Review][Patch][High] `close()` 的 timeout 判定与成功终态写入之间仍有竞态：`Event.wait()` 已超时后、调用线程取得 `_state_lock` 前，writer 可先写入 complete `_final_result`，使超过 deadline 的关闭返回成功并发布 complete；超时必须在同一终态仲裁中抢占成功发布。 [`app/workers/session_writer.py:703`]
- [x] [Review][Patch][High] `HardwareWorker.bind_session_recorder()` 缺少 Actuation 路径已有的 cancellation token：bind payload 出队后若 timeout 线程先取得 `_ttl_control_lock`，API 可返回失败，而 owner 随后仍迟到绑定旧 ingress，阻塞下一 generation。 [`app/workers/hardware_worker.py:146`]
- [x] [Review][Patch][High] publish 后失败的回滚保护仍非 fail-closed：若 staging 路径在回滚前重现，代码会跳过 final→staging rename 且不写 incomplete marker；若回滚 rename 与 marker open/write/fsync/replace 同时失败，异常也只被记录。两条路径都会留下含 complete manifest、可通过 validator 的最终目录。 [`app/workers/session_writer.py:1168`]
- [x] [Review][Patch][High] canonical receipt 去重会吞掉已接受 envelope 的 producer sequence；重复 receipt 后若还有同 producer 新记录，writer 仍发布 complete，但 complete validator 会因 sequence 跳号拒绝并在下次扫描隔离该 bundle。 [`app/workers/session_writer.py:924`]
- [x] [Review][Patch][Medium] Actuation owner 忽略 `recorder.post_fence()` 返回值并立即解绑，`recorder_fence` handler 随后无条件 ACK `accepted=True`；queue-full/identity failure 时 Controller 会收到虚假的 owner fence 成功确认。 [`app/workers/actuation_worker.py:441`]
- [x] [Review][Patch][Medium] 呼吸门控路径已有采样 `expected_open_ns`，但 `open_requested` 等阈值事件没有写入 `ProtocolGateEvent.monotonic_ns`，最终日志为 `null`，无法直接关联 raw 与 receipt 的单调时间轴。 [`app/services/protocol_executor.py:984`]
- [x] [Review][Patch][Medium] recovery ownership 在多重失败下仍会丢失：初始 owner marker 创建失败且 unlink/rmdir 清理失败，或 collision cleanup 的 rmdir、marker、recovery manifest 连续失败，都会留下 scanner 永久忽略且 UI 不登记精确位置的 orphan `.session.part`。 [`app/services/session_file_service.py:389`] [`app/services/session_file_service.py:1307`]
- [x] [Review][Patch][Medium] complete validator 对 receipt/quality 仍主要检查字段存在，未严格验证 timing/epoch/sequence/valve/stale/p95/transitions 类型与时序，也未强制 `measurement_point`、`actual_ns_semantics` 为 `daqmx_write_ack`；语义相反的日志仍可被认证为 complete。 [`app/services/session_file_service.py:907`]
- [x] [Review][Patch][Medium] final bundle 的 hash/byte/count/last-sequence 等验证失败返回未携带 manifest 中已存在的 `last_session_sequence`，导致 RecoveryFinding 和中文 UI 显示“不可用”，丢失 AC9 要求的可用最后成功序号。 [`app/services/session_file_service.py:1045`]
- [x] [Review][Patch][Medium] raw recovery 校验未捕获 `csv.Error`；单个小于 1 MiB 行上限但超过 `csv.field_size_limit()` 的损坏字段会让 `validate_complete_bundle()` 抛出，并中断该输出目录的整次 recovery scan，而不是报告并隔离该候选。 [`app/services/session_file_service.py:679`]
- [x] [Review][Patch][Medium] 240 UTF-16 code unit 预算未纳入 collision staging 内的 ownership marker；短 stem、长输出根目录下，`__999` raw/log 路径可在预算内而 marker 路径已超过 240，导致预览通过但 reserve 失败。 [`app/services/session_file_service.py:1245`]

## Dev Notes

### 固定的产品与数据决策

- `{Timestamp}_{Subject}_{Condition}` 沿用 PRD/epics 下划线规范；字面 `*` 在 Windows 非法。[Source: docs/prd.md:24-28] [Source: docs/epics.md:23-25]
- PRD/epics 固定的是 `{Timestamp}_{Subject}_{Condition}.raw/.log` basename，没有规定两文件必须直接平铺在所选目录。本 Story 将“一对文件”作为 `<output>/<stem>/` session bundle 发布，两个最终 basename 完全保持 FR2.1；目录级 commit 用于满足“不得留下看似有效的单边半成品”，实现时不得自行退回 sibling 逐文件 rename。
- `.raw v1` 是项目原生、可读的 100 Hz 校准前 AI0 呼吸流，不声称兼容 ProgOlfactoTao 未定义的二进制格式，也不声称保存 1 kHz AI6 连续波形。PRD 只要求旧协议 `.txt/.csv` 兼容，没有定义旧 `.raw` byte compatibility。[Source: docs/prd.md:24-28]
- `.log v1` 是结构化 JSONL，不是 `logging.basicConfig` 文本拷贝；中文 `message` 服务操作人员，稳定字段服务追踪与自动分析。
- 一次 session 绑定一份协议运行；pause/rearm 不切 session，stop/completed 后新 start 必须新建 session。该边界避免 trial/receipt/quality 跨文件歧义。
- 成功 session 的丢失计数必须为 0。任何 queue overflow 或 I/O failure 都进入 failed 并安全停止，不能“尽量写一点仍算成功”。

### 当前代码状态：预计 UPDATE 与强回归文件

- `app/controllers/main_controller.py`
  - 当前：Controller 接收 `breath_samples`，发布 `ProtocolGateEvent`，另一路记录 receipt 文本；协议 start 尚无 session gate。
  - 本 Story：编排 session start/fail/close、protocol start gate、Controller producer envelope/fence 与最终 ack；UI handlers 不是 raw/receipt 持久化必经路径。
  - 必须保留：生产 protocol 同步 writer 拒绝、Flow lease、readiness/safety 转换、既有异步 ActuationWorker 路径。[Source: app/controllers/main_controller.py:1201-1206] [Source: app/controllers/main_controller.py:1208-1246] [Source: app/controllers/main_controller.py:1479-1516]
- `app/workers/hardware_worker.py`
  - 当前：独占共享 AI0/AI6 task，验证 epoch/sequence/monotonic 后把 100 Hz breath batch 直接给 ActuationWorker，并向 UI signal 发布；`flush_logs()` 只委托 HAL no-op。
  - 本 Story：增加 O(1)、有界、非阻塞的 immutable envelope ingress 和 producer fence；顺序保持“先 ActuationWorker、后 recorder、再 UI”，不拥有文件句柄。
  - 必须保留：唯一 AI task、TTL detection、故障 latch/退避、ActuationWorker direct sink 与 batch 顺序。[Source: app/workers/hardware_worker.py:413-514] [Source: app/workers/hardware_worker.py:268-275]
- `app/workers/actuation_worker.py`
  - 当前：独占 executor/gating/metrics/DO，发出 executor result、receipt、snapshot；`e401a31` 已修复历史 AI backlog 对首动作的阻塞。
  - 本 Story：在 owner 内为 event/receipt 分配稳定 envelope identity 并 `put_nowait` 到 recorder，terminal 后发 producer fence；把 recorder failure generation 纳入现有 interlock latch。Qt signals 继续服务 UI，不在 action loop 做序列化或等待 I/O。
  - 必须保留：控制消息优先级、无损相邻 AI batch 合并、deadline/紧急队列、receipt identity、metrics reset/rearm 语义。[Source: app/workers/actuation_worker.py:986-1038] [Source: app/workers/actuation_worker.py:1902-1926]
- `app/services/shutdown_service.py`
  - 当前：安全全关/owner handoff 后调用 `HardwareWorker.flush_logs()`，该调用不是 session 持久化。
  - 本 Story：默认不改，只复用其返回给 Controller 的最终 shutdown event 并做强回归；session timeout 不得倒置 DO→AI→serial 顺序。若确需新 hook，必须是最小、结构化、非文件 I/O 的扩展。[Source: app/services/shutdown_service.py:152-178]
- `app/views/main_window.py`、`app/models/app_state.py`、`app/main.py`
  - 当前：只有概览/校准/预检/协议页，状态栏已有错误/telemetry/时序告警；全局 logging 只有 console/basicConfig。
  - 本 Story：增加文件页和 session snapshot/lifecycle 连接；复用状态栏，不把业务/文件 I/O 放进 View。[Source: app/views/main_window.py:58-125] [Source: app/main.py:71-79]
- `config/default_config.json`
  - 当前：已有阈值、硬件变体、动作质量和 worker 参数，无 session writer 参数。
  - 本 Story：只加入跨机器通用的安全默认；输出目录/受试者/条件属于会话输入，不写成仓库默认。

### 必须保留的 Story 3.2–3.4 契约

- 3.2 的事件最少包含 trial、valve、gate/sample/threshold/safety/result，且明确留给 3.5 复用。[Source: docs/sprint-artifacts/3-2-breath-gated-stimulation.md:71-75]
- 3.3 的 manual/TTL event 保留采集时间、mode/source/reason、epoch/sequence；旧 epoch、持续高电平、模式不匹配不推进 trial。[Source: docs/sprint-artifacts/3-3-manual-vs-ttl-trigger-modes.md:83-88]
- 3.3 的协议替换、readiness loss、显式 rearm 和关阀失败恢复语义不能被 session UI 绕过。[Source: docs/sprint-artifacts/3-3-manual-vs-ttl-trigger-modes.md:55-71]
- 3.4 的 receipt/quality 字段已经由结构化对象提供，3.5 直接持久化而非重新计算。[Source: docs/sprint-artifacts/3-4-low-jitter-actuation-20ms.md:114-130]
- HardwareWorker 为 AI owner、ActuationWorker 为 DO/executor/metrics owner、FlowWorker 为 serial owner；session writer 是第四个纯 I/O owner，不属于 HAL。[Source: docs/architecture.md:47-54]
- `daqmx_write_ack` 只表示 HAL write 成功回执，不表示机械阀物理完成。[Source: docs/architecture.md:50]

### 文件格式摘要

`.raw`：

```text
# {"schema":"olfactorypilot.raw","schema_version":1,"session_id":"...","nominal_rate_hz":100,...}
record_sequence,timestamp,monotonic_ns,ai_epoch,sample_sequence,ai0_raw
1,1785146400.123,123456789000,7,4102,-0.4412
```

`.log`：

```json
{"schema":"olfactorypilot.event","schema_version":1,"session_id":"...","session_generation":3,"session_sequence":1,"producer":"session","producer_sequence":1,"event_id":"session:3:1","record_type":"session_event","event":"session_started","timestamp":"2026-07-27T18:00:00.123+08:00","source":"session","result":"success","message":"会话已开始。"}
```

以上示例固定字段与编码，不固定数值；实现测试必须验证 round-trip 和每行完整结束。

### 测试与完成门禁

- 纯命名/manifest/recovery 放在 service 单测；writer 线程用 fake filesystem/writer 和 event/barrier；Controller/View 用 pytest-qt；真实 Windows 权限差异用 fault injection，不依赖 POSIX chmod。
- 性能不能只看平均吞吐。`e401a31` 曾在最终 closure 前再次测到首个 open `35.3069ms/31.3884ms`，修复后才以无 trace 生产路径通过 200+200；3.5 必须验证首动作、rolling p95、final-last-100 和安全场景。[Source: docs/sprint-artifacts/3-4-low-jitter-actuation-20ms.md:336-336]
- Story 3.4 最终 HIL 没有外接 AI0 呼吸源/AI6 TTL 源，且 ack 不是机械时序。3.5 的 HIL 只证明 session writer 并发负载未破坏已有软件/DAQ write 门禁，不得扩大声明。[Source: docs/sprint-artifacts/3-4-low-jitter-actuation-20ms.md:381-383]
- 不使用真实填盘、删除用户数据、拔线或短接做失败测试；破坏性 HIL 仍需单独授权。

### Latest Technical Notes

- Python 3.11 `os.fsync()` 在 Windows 调用 `_commit()`；对 buffered file 必须先 `flush()` 再 `os.fsync(f.fileno())`。来源：https://docs.python.org/3.11/library/os.html#os.fsync
- `os.O_CREAT | os.O_EXCL` 在 Windows 可用；但本 Story 用同父目录独占 staging `mkdir` 预留整对 stem，避免两个文件分别预留的竞态。来源：https://docs.python.org/3.11/library/os.html#open-constants
- Windows 禁止 `< > : " / \ | ? *`、控制字符、保留设备名，并不应以空格或句点结尾。来源：https://learn.microsoft.com/zh-cn/windows/win32/fileio/naming-a-file
- Windows move 前必须关闭写句柄；publish 必须在 raw/log/manifest 全部 flush/fsync/close 后执行。来源：https://learn.microsoft.com/en-us/windows/win32/fileio/moving-and-replacing-files
- 不引入 TxF 或额外数据库；目录 publish 失败由显式 `.session.part` + recovery 语义处理，不能声称跨掉电绝对原子。

### 风险与未决问题

- **旧系统 `.raw` 兼容**：仓库没有 ProgOlfactoTao `.raw` byte schema 或下游导入契约。本 Story 固定项目原生 v1；若实验室已有依赖旧 `.raw` 的分析工具，需要另行提供样例和兼容/导出 Story。
- **受试者隐私**：FR2.1 明确把 Subject 放入文件名，文件名和日志会含可识别输入。本 Story 只做清洗、不自动脱敏；实验室需要明确使用真实姓名还是受试者编码及数据保留策略。
- **文件布局**：为保证 pair commit，最终使用 `<output>/<stem>/` bundle，而非输出目录下两个 sibling 文件。若既有外部脚本硬编码 sibling 路径，需要在实现前提供该脚本并决定兼容导出，不得偷偷回退到逐文件 rename。
- **网络路径**：UNC/网络文件系统的 rename/durability 语义无法按本地 Windows volume 保证，v1 明确拒绝；未来支持必须有目标文件系统的故障测试。

## References

- [Source: docs/epics.md:189-244]
- [Source: docs/prd.md:24-28]
- [Source: docs/prd.md:57-63]
- [Source: docs/ux-design.md:13-20]
- [Source: docs/ux-design.md:60-74]
- [Source: docs/architecture.md:38-80]
- [Source: docs/project-structure.md:235-250]
- [Source: docs/sprint-artifacts/3-2-breath-gated-stimulation.md:71-75]
- [Source: docs/sprint-artifacts/3-3-manual-vs-ttl-trigger-modes.md:83-88]
- [Source: docs/sprint-artifacts/3-4-low-jitter-actuation-20ms.md:114-130]
- [Source: app/services/hal.py:10-49]
- [Source: app/models/protocol_execution.py:31-92]
- [Source: app/models/actuation.py:52-146]
- [Source: app/controllers/main_controller.py:1208-1246]
- [Source: app/controllers/main_controller.py:1479-1516]
- [Source: app/workers/hardware_worker.py:413-514]
- [Source: app/workers/actuation_worker.py:986-1038]

## Dev Agent Record

### Agent Model Used

GPT-5 Codex

### Implementation Plan

- 按 Task 1→6 顺序执行 red-green-refactor：先固定会话模型、Windows 命名/目录事务与 recovery 契约，再实现单写者 writer。
- 通过 producer-facing `put_nowait` ingress 将 HardwareWorker/ActuationWorker 的 immutable batch/event/receipt 投递到独立 I/O owner；Qt signals 仅保留 UI 用途。
- Controller 负责 session/protocol gate、安全 stop、producer fence 与 finalization ack；View 只提交意图并渲染 snapshot。
- 每个 task 完成后运行对应定向测试，最终运行全量 pytest、ruff 与 `git diff --check`；未获授权不运行真实 NI HIL。
- Task 9 继续采用 RED→GREEN→REFACTOR：先用 Event/fake filesystem/clock/fault injection 固定 pending document、bind timeout、旧 recovery callback、scan lock/cancel、无效 manifest 与 JSONL 空白行交错，再实施最小修复。
- 方案 A 使用覆盖完整 Qt event loop 的 Windows named mutex 强制单实例；第二实例在创建 Controller/HAL 前中文提示并退出，正常退出、构建失败和进程崩溃均不遗留 ownership。
- 本次“修复后独立复审”按清单顺序处理 4 High、7 Medium：每项先以 Event/fake clock/filesystem/fault injection 或结构化 schema 变异建立 RED，再做最小 GREEN 与共享 validator/终态 helper REFACTOR。
- Task 10 review continuation 先复现首个正式动作队列顺序、master `valve=0` 契约与正式 runner 缺口，再以 owner monotonic cutover、共享 receipt contract 和原生 SessionWriter/fence/final-validator 路径完成自动化闭环；真实 HIL 验收独立保留为未完成项。
- 本次独立复审修复限定为 4 High、2 Medium：以 canonical receipt 内容一致性、writer failure interlock 顺序、性能 Gate 前置、Git candidate 绑定、master_prepare 矩阵及单 session 单协议作为六个独立 RED→GREEN 边界，不扩展通用重构。

### Debug Log References

- Story creation baseline: branch `main`, HEAD `e401a319d1da93302bcc8908fc9ed7d161b3da08`, upstream `OlfactoryPilot-CN/main`, clean worktree.
- Story 3.4 closure commit verified: `e401a31` exists, is current HEAD, and is an ancestor of current HEAD.
- Story 3.5 implementation baseline confirmed: branch `main`, HEAD `e401a319d1da93302bcc8908fc9ed7d161b3da08`, upstream `OlfactoryPilot-CN/main`; pre-existing changes were the untracked Story file and its `ready-for-dev` sprint-status update.
- 2026-07-30 真实 HIL 失败证据：candidate `c9baff6e6910266621fb2a36c6b62f880f42a27e`；运行目录 `logs/benchmarks/story-3-5-20260730-154234-live`；证据文件 `failure.json`、`metadata.json`、`hardware-mapping.json`、`raw-receipts.csv` 与中止 bundle `session-output/20260730-154242-838_HIL-NO-SUBJECT_Story-3.5-Windows-NI` 均保留为只读输入。
- Task 10 RED：新增首个正式结构化样本必须晚于 owner trigger cutover、runner 必须等待 owner trigger ACK、master `valve=0` writer/validator round-trip、错误主阀 target 拒绝、当前中止 bundle 只读验证及原生 Story 3.5 runner 生命周期测试；首轮定向执行稳定得到 9 failed。
- Task 10 只读证据复核：中止 bundle 的 raw/log SHA-256、byte、count、last sequence、producer fences 与 `dropped_count=0` 均未变化；修复后的 validator 在配置 `Dev2/P1.0` 为 master 时接受该 bundle，未改写任何证据文件。
- Task 10 MockHAL smoke：正式 runner 的 `--story-3-5-recording` 模式在 20 open/20 close 下完成 UI、SessionWriter、hardware/actuation/controller fences、四个安全场景与 bundle final validator；该运行仅用于软件路径验证，不构成真实 NI HIL 证据。
- 本次独立复审 RED：HIL runner 的 candidate/performance/failure-callback/session-boundary/quality-preservation 5 个测试稳定得到 5 failed；writer/master 契约组合得到 9 failed、3 个既有非法类别场景已正确拒绝。所有 RED 均在修改对应实现前运行。
- 2026-07-30 第二次真实 HIL 失败证据：candidate `7c4d971aa370056b8a70cee3592344bb54dd7ad7`；正式目录 `logs/benchmarks/story-3-5-20260730-183616-live` 只读；正常 open/close 各 `68/200`，触发 receipt `protocol-9-close-1000283` jitter `30.3166 ms`、result success，随后 severe latch 并中止。bundle 完整性 Gate 通过但 severe 后缺少完整 21 条 emergency-close receipts；独立 close 目录 `logs/benchmarks/story-3-4-20260730-183741-live` 只证明随后硬件 owner 最终关闭，不替代失败 run 自身的安全收尾证据。
- 第二次 HIL trace 根因复核：触发 close 的 `started_ns - expected_ns = 29.6321 ms`，而 `actual_ns - started_ns = 0.6845 ms`，超限发生在 Actuation owner 调度、不是 NI DAQ write；旧 trace 在 open 后立即 `trial_trace_end`，比 close deadline 早约 `98.8 ms`，因此只留下 close submit、漏掉 close execute/UI/message 阶段。代码审计确认普通 deadline 被非安全 priority message 抢占，且 HIL `ReceiptCollector.record()` 在 receipt signal 路径同步打开/写 diagnostic JSONL；两项均违反 deadline/owner 隔离。修复后 close deadline 在到期前 5 ms 保留 owner 窗口、到期后先于非安全 message，安全 stop/interlock/recorder failure 仍优先；diagnostic receipts 改为 owner 停止后落盘，canonical SessionWriter ingress 不变。
- 第二次 HIL 安全顺序复核：历史 bundle 的 sequence `728..748` 是 21 条 `shutdown-close` success，早于 actuation fence `746`、controller event 与 `session_closed=750`，证据文件 hash/size/mtime 在回归前后不变；缺陷是正常 close severe 分支未提交 `severe-close`。修复后 owner 立即提交全部配置目标 `severe-close`，runner 有界确认完整 target set 后才标记 abort close confirmed；随后 Actuation shutdown 发 fence、Hardware 发 fence、Controller 发 fence并 finalize。全关不完整时 writer 先锁存 `shutdown_emergency_close` failure，禁止 complete bundle。
- 第二次 HIL follow-up RED→GREEN：deadline reservation、延迟 close trace、owner 路径无 diagnostic 磁盘写、close-severe 全目标回执/fence 顺序及 runner 中止等待首轮稳定得到 5 failed；最小修复后相关 5 passed，扩展受影响整文件 108 passed。

### Completion Notes List

- Ultimate context engine analysis completed - comprehensive developer guide created.
- Story 已将高层三条 AC 扩展为可测试的命名、schema、writer ownership、failure、close/publish、recovery 与 HIL 契约。
- Task 1 完成：新增不可变 session identity/envelope/fence、受控幂等状态机、Windows NFC/保留名/路径预算清洗、独占 staging pair 预留、`__001…__999` 碰撞与只读验证/recovery quarantine；定向 17 passed，全量 395 passed。
- Task 2 完成：新增独立 SessionWriterWorker、O(1) `put_nowait` ingress、raw/JSONL schema、receipt/protocol/quality adapter、generation/sequence/fence 门禁、hash/count manifest、幂等 close 与全阶段 fail-closed；定向 24 passed，全量 419 passed。
- Task 3 完成：Controller 建立 session/protocol 强 gate；Hardware/Actuation owner direct ingress 与 recorder interlock 已接入；failure 先锁存再安全 stop，SAFETY receipt 继续处理；stop/completed/reset/exit 以三 producer fence 收尾；相关定向 147 passed，全量 436 passed。
- Task 4 完成：新增中文“文件”页与独立 SessionViewSnapshot，实时显示原值/清洗值/staging/最终路径，活动会话冻结输入并联动协议 capability，recovery 与错误提供中文下一步；定向 4 passed、相关 UI 93 passed、全量 440 passed。
- Task 5 完成：加入安全 writer 默认配置与中文 fallback warning，复用既有 ShutdownService 结构化事件/关闭链并在硬件释放后有界 finalize，补齐四个包导出并同步架构/项目结构文档；全量 442 passed。
- Task 6 完成：补齐目录不可写、`__999` 碰撞上限、缺失 manifest recovery、receipt 完整字段、recorder failure 后 shutdown owner 顺序，以及 telemetry 不得清除 recorder latch 的回归；定向 199 passed、全量 447 passed，ruff 与 `git diff --check` 通过。
- 真实 Windows/NI HIL 未运行：本轮未获得用户明确授权，Story 仅进入 `review`，不置 `done`，不扩大 3.4 的硬件时序声明。
- 未提交、未推送。
- 2026-07-27 code review follow-up 完成：recovery 候选身份、manifest/UTF-8/JSONL/containment、preview/reserve 单次时间样本与 envelope 深层冻结已修复。
- writer close timeout/finalize 使用单一失败终态与 publish cancel barrier；queue-full callback 移至 ingress 锁外，`session_closed` 在 rename 前不再声称已发布，shutdown payload 不能覆盖 canonical 字段。
- Controller/Actuation 会话边界已收紧：CLOSING 拒绝全部非 SAFETY 类别；异步 master-prepare 重校验 session/document/readiness；controller fence 后禁止追加；unsafe/possibly-open/handoff 失败禁止 complete publish；owner fence 携带 owner-local metrics snapshot。
- failed writer 必须终止后才能建立新 generation，Actuation recorder bind/fence 在 owner 队列串行化；新 writer 初始化失败覆盖为新 generation 的 failure identity。
- recovery scan 改为专用 QThread、每次请求可刷新、显示 last_sequence，并通过 active-staging 登记避免与活动 writer 竞态隔离。
- 新增统一 pytest autouse teardown，确定性停止 Controller QTimer、Recovery QThread、SessionWriter、Actuation/Hardware/Flow QThread、finalizer Python thread 与窗口；已知关闭后 UI 解冻竞态修复。
- 本轮 RED 证据：新增缺陷测试分批先得到 8 failed/1 passed、12 failed、2 failed/2 teardown errors，再实施修复；GREEN 证据：Story 定向 224 passed，全量 `pytest` 472 passed，`python -m ruff check .` 与包含未跟踪文件的临时-index `git diff --check` 均通过。
- 本轮未运行真实 NI HIL；全量测试仅使用 MockHAL/自动化 fault injection，不形成新的真实硬件时序声明。
- 2026-07-29 独立 code review 结论为 changes requested：确认 8 项 High、5 项 Medium；上一轮 18 项复核为 9 项闭环、5 项部分闭环、4 项未闭环，已登记为未完成 Task 8。审查时定向测试复跑 224 passed、全量 472 passed、ruff 与含未跟踪文件的 diff check 通过；首次定向运行曾在 writer 测试中途无 traceback 以 exit code 1 异常退出。未运行真实 NI HIL，Story 保持 `review`。
- Task 8 RED 证据：High 缺陷测试首轮稳定得到 10 failed；Medium 缺陷测试首轮稳定得到 5 failed；方案 A 最终生命周期审计新增 1 failed，随后才修改实现。并发覆盖使用 `threading.Event`、fake clock/filesystem 与自动化 fault injection，不以毫秒级 sleep 作为唯一断言。
- Task 8 High-1 闭环：`test_session_boundaries_wait_for_pending_plans_and_compensate_late_prepare`、`test_recorder_bind_rechecks_boundary_after_writer_initializes` 覆盖 pending protocol/master/manual/pretest 与 bind 二次校验；`MainController._session_boundary_rejection()`、`handle_session_start_requested()`、`handle_session_end_requested()`、`_maybe_begin_session_finalization()` 统一等待边界收敛，迟到成功 master-prepare 追加 owner-thread 安全 stop。
- Task 8 High-2 闭环：`test_recorder_fence_cannot_overtake_earlier_owner_messages`、`test_recorder_fence_waits_for_earlier_action_receipt` 覆盖消息与动作/receipt 交错；`ActuationWorker` 将 fence 作为普通 owner 队列屏障，只有其前方消息、动作、receipt 和 safe transition 全部完成后才确认。
- Task 8 High-3 闭环：`test_unsafe_shutdown_cancels_publish_after_finalization_started` 以 manifest fault Event 固定 finalization 交错；`_prepare_session_for_global_stop()`/`_finish_session_after_global_stop()` 对已进入 CLOSING/finalizing 的 writer 仍传播取消发布，unsafe/possibly-open/handoff 失败不能生成 complete bundle。
- Task 8 High-4 闭环：`test_close_timeout_does_not_publish_or_wait_forever`、`test_slow_finalize_timeout_claims_terminal_result_and_never_publishes`、`test_publish_path_probe_never_holds_state_lock_against_failure` 覆盖严格 deadline 与阻塞路径探测；`SessionWriterWorker.close()` 只消费同一 timeout 预算，publish 的 `exists()`/rename 位于 `_state_lock` 外，timeout/failure 抢先后会撤销已 rename 的目录。
- Task 8 High-5 闭环：`test_normal_finalizer_thread_is_tracked_and_identity_bound`、`test_pretest_thread_never_overwrites_session_finalizer_reference`、`test_stale_finalizer_cannot_overwrite_new_generation_state` 覆盖线程引用和代际漂移；Controller 分离 pretest/finalizer 引用，finalizer 更新 state、interlock、result/event 前核对 writer、session_id 与 generation。
- Task 8 High-6 闭环：`test_teardown_cancels_recovery_worker_before_dropping_reference`、`test_teardown_releases_prepared_reservation_for_recovery` 及 `tests/test_shutdown_actuation.py` 覆盖 QTimer、Recovery、PREPARED reservation、writer/finalizer 和 owner 退出；`RecoveryScanWorker.cancel()` 支持协作取消，`teardown()` timeout 后保留仍运行线程引用，并将未确认记录的 PREPARED staging 释放为可恢复状态，`app/main.py` 的 `aboutToQuit` 连接 `shutdown_and_teardown()`。
- Task 8 High-7 闭环：`test_reserve_registration_is_atomic_against_recovery_scan`、`test_writer_initialize_timeout_keeps_reservation_active_until_thread_stops` 使用 Event 固定 reserve/scan 与 init-timeout 交错；reserve 创建与 active 登记、scan 候选处理共用锁，writer 真正停止后才 `mark_inactive()`。
- Task 8 High-8 闭环：`test_recovery_ignores_unidentified_part_directory_with_matching_pair` 等 recovery identity 测试证明普通 `.session.part`/同名 raw-log 不会被移动；scanner 仅接受 schema/version/session_id/generation/stem/basename 完整一致的 OlfactoryPilot manifest。
- Task 8 Medium-1 闭环（用户选择方案 A）：`test_first_click_locks_current_timestamp_and_collision_path_before_recording` 覆盖 `__001` 碰撞路径；第一次点击以一次 wall-clock 样本进入 PREPARED 并锁定精确最终路径，第二次确认启动 writer，独立 `recording_started_at` 写入首条日志。
- Task 8 Medium-2 闭环：`test_closed_session_input_edit_replaces_old_descriptor_preview`、`test_failed_session_input_edit_replaces_failed_descriptor_preview` 覆盖终态编辑；`_render_session_snapshot()` 对 CLOSED/FAILED 使用当前输入的新 preview，按钮、屏幕和 reserve 共用已锁定 descriptor。
- Task 8 Medium-3 闭环：`test_output_validation_rejects_mapped_or_resolved_network_location` 通过 fake `GetDriveTypeW` 覆盖映射盘；`_is_network_location()`/`_normalize_output()` 同时拒绝字面 UNC 和 Windows `DRIVE_REMOTE`。
- Task 8 Medium-4 闭环：`test_readiness_latch_rejects_stale_generation_fail_and_close` 覆盖旧 ingress；`RecorderReadinessLatch.fail()`/`close()` 要求 session_id/generation 匹配，旧代际不能关闭或击穿新代际 readiness。
- Task 8 Medium-5 闭环：`test_complete_bundle_validation_streams_files_without_read_bytes` 禁止 raw/log 调用 `Path.read_bytes()`；`validate_complete_bundle()` 逐行解析并增量计算 SHA-256、bytes、count、JSONL identity，不构造完整 records 列表。
- Task 8 Gate 闭环：Story 定向测试 244 passed；全量 `python -m pytest` 492 passed；`python -m ruff check .` 通过；相对 `e401a319d1da93302bcc8908fc9ed7d161b3da08`、通过临时 index 纳入全部未跟踪文件的 `git diff --cached --check` 返回 0。定向集合同时覆盖上一轮 18 项 findings，完整全量回归无退化。
- 本轮只使用 MockHAL、fake filesystem/clock 与自动化 fault injection；真实 NI HIL 未执行，Story 因此恢复为 `review` 而非 `done`。未提交、未推送。
- Task 9 RED 证据：6 项复审 finding 与方案 A 的首批确定性测试稳定得到 12 failed；随后单实例 build-failure 生命周期审计新增 1 failed，均在修改对应实现前确认。交错使用 Event、fake filesystem/clock 与自动化 fault injection，bind 的短 timeout 只触发 API 超时语义，不作为竞态排序的唯一断言。
- Task 9 High-1 闭环：session boundary 纳入 `_pending_protocol_load`；document completion 按 pending object identity 归属，PREPARED/RECORDING/CLOSING 拒绝迟到成功并保留绑定协议，旧回执也不能清除更新请求。
- Task 9 High-2 闭环：`recorder_bind` 携带 cancellation token；timeout 与 owner handler 在同一 condition 下仲裁并移除仍排队请求，返回失败后不会迟到绑定旧 ingress，下一 generation 可立即成功绑定。
- Task 9 High-3 闭环：recovery completion 通过 Qt sender 绑定实际完成 worker，只 wait/deleteLater 该 worker；旧 completion 不再接触或清空新 `_recovery_scan_worker`。
- Task 9 High-4 闭环：active-staging lock 仅保护短时登记检查，完整 bundle 验证和 quarantine 位于锁外；raw/log 流式循环逐行响应 cancel，reserve 与窗口 teardown 不再等待整文件验证。
- Task 9 Medium-1 闭环：reserve 在 raw/log/manifest 前写入严格 identity 的 OlfactoryPilot ownership marker；create_log/create_manifest/manifest 部分写入失败的 staging 可被报告和隔离，普通无 marker/无有效 manifest 用户目录仍被忽略。
- Task 9 Medium-2 闭环：complete bundle 验证拒绝空行和仅含空白的 JSONL 行，不再从 hash/count 契约中跳过。
- Task 9 Decision 闭环：`app/main.py` 使用 Windows 全局 named mutex 覆盖完整 Qt event loop；第二实例在构建 Controller/HAL 前中文提示并以非成功状态退出，正常退出和 build failure 均释放，进程崩溃由操作系统回收句柄。
- Task 9 Gate 闭环：Story 定向测试 259 passed；全量 `python -m pytest` 507 passed；`python -m ruff check .` 通过；相对 baseline `e401a319d1da93302bcc8908fc9ed7d161b3da08`、以临时 index 纳入全部未跟踪文件的 `git diff --cached --check` 返回 0。完整定向集合覆盖 Controller QTimer、Recovery QThread、SessionWriter/finalizer、窗口 teardown 和 generation/session 隔离。
- 本轮仅运行 MockHAL/fake/自动化 fault injection；未连接或运行真实 NI HIL，不形成新的硬件时序证据。Story 与 sprint-status 均保持 `review`；未提交、未推送。
- 2026-07-30 review continuation RED 证据：13 项 finding 的确定性测试首轮得到 22 failed；并发/teardown 覆盖使用 `threading.Event`、受控 chunk reader、fake clock/filesystem 与自动化 fault injection，不以 sleep 竞争作为断言。
- 4 项 High 闭环：Actuation owner 直接以 `recording_ready` 阻断协议启动/NORMAL 动作；recovery identity 与 raw/log 改为限长、64 KiB 分块且逐块响应 cancel；单实例 mutex 在任何 owner 仍存活时保留至进程退出；最终目录使用严格 ownership marker 识别缺失/损坏 manifest 的 owned bundle。
- 9 项 Medium 闭环：complete validator 严格校验 raw v1 header/行、JSONL 稳定字段/首尾事件/整数类型/零丢失；生命周期改用二次确认的 `recording_started_at`；TTL 单调时钟和质量 transition 结构化持久化；PREPARED 可显式取消到 recovery-required；恢复按钮打开真实保留位置；Unicode Cc、collision cleanup 与 recovery scan 顺序均已修复。
- 本轮 Gate 证据：新增缺陷断言 22 passed；Story 定向 `pytest` 281 passed；全量 `python -m pytest -q` 529 passed；`python -m ruff check .` 通过；相对 baseline `e401a319d1da93302bcc8908fc9ed7d161b3da08`、以临时 index 纳入全部未跟踪文件的 `git diff --cached --check` 返回 0。
- 本轮仅使用 MockHAL、fake clock/filesystem 与自动化 fault injection；未连接或运行真实 NI HIL。Story 与 sprint-status 保持 `review`，未提交、未推送。
- 2026-07-30 当前 Review Finding High-1 闭环：新增 Actuation bind 顺序屏障、Hardware inflight AI cutover 与 Controller 双 owner ack 三个确定性测试，RED 为 3 failed；实现 owner-thread bind/ack 与失败解绑后，直接相关 4 passed、受影响 Actuation/Protocol integration 全文件 109 passed。
- 2026-07-30 当前 Review Finding High-2 闭环：`test_owner_rejects_warmup_open_without_recording_ready` 在使用当前 interlock generation 后稳定 RED 为 1 failed；owner 的 `recording_ready` 开阀门禁扩展到 WARMUP，Actuation 全文件 67 passed。
- 2026-07-30 当前 Review Finding High-3 闭环：以 Event + rename fault injection 固定 publish 成功后失败抢先且 rollback rename 再失败，RED 证明 validator 误报 complete；writer 现写入严格 publish-incomplete marker，validator 拒绝且 recovery scanner 隔离，SessionWriter 全文件 35 passed。
- 2026-07-30 当前 Review Finding High-4 闭环：`show`、`exec`、`lifecycle_stopped` 三种异常路径参数化测试初始 3 failed；mutex 仲裁移入 `finally` 并对未知 owner liveness 保守保留 guard，相关 6 passed、`tests/test_app.py` 74 passed。
- 2026-07-30 当前 Review Finding High-5 闭环：raw/log × 预先篡改/初始化 fault injection 四种场景初始 4 failed；writer 在打开后及 header commit 前双重确认预留流仍为空，不再 seek 追加，SessionWriter 全文件 39 passed。
- 2026-07-30 当前 Review Finding High-6 闭环：超大 protocol metadata 测试先因缺少共享 limit contract 进入 RED；writer 与 validator 现共用 1 MiB encoded-line 上限并在写入前 fail closed，Writer+FileService 全文件 90 passed。
- 2026-07-30 当前 Review Finding Medium-1 闭环：缺失 CSV header、重复 sample identity、monotonic 倒退与 ingress 重复 batch 四个测试初始 4 failed；validator 流式维护跨行 identity，ingress O(1) 校验 batch 边界且 writer 全量复核，Writer 41 passed、FileService 53 passed。
- 2026-07-30 当前 Review Finding Medium-2 闭环：7 类 record/envelope/summary/owner 变异初始 5 failed（另 2 项虽已被生命周期门禁拒绝但缺少专属原因）；validator 现校验 record type、receipt/quality 字段、event_id、逐 producer sequence、owner identity 与 sample/event/receipt/queue 汇总，FileService 60 passed、Writer 41 passed。
- 2026-07-30 当前 Review Finding Medium-3 闭环：NaN timestamp、零 epoch/sequence/monotonic、None/不可转换 monotonic 六种非法 pulse 初始 6 failed/owner exception；owner 现先严格验证完整 TTL identity，再生成 `ttl_pulse_rejected` 结构化事件且不推进 trial，Actuation+Protocol integration 116 passed。
- 2026-07-30 当前 Review Finding Medium-4 闭环：quality ack schema 与原始时间身份两项测试初始 2 failed；`actuation_receipt`、`quality_acknowledged`、`quality_ack_rejected` 均走 quality snapshot adapter，并传递 event timestamp 与 monotonic/actual_ns，Actuation+Writer 115 passed。
- 2026-07-30 当前 Review Finding Medium-5 闭环：rmdir + ownership marker 双 fault 注入初始 1 failed；marker 失败现回退写严格 `recovery_required` manifest，manifest 也失败则显式返回 `collision_cleanup_identity`，scanner 可发现并隔离 orphan，FileService 61 passed。
- 2026-07-30 当前 Review Finding Medium-6 闭环：fake resolved-network child 测试初始 1 failed；recovery 现于任何 identity/marker/raw/log 读取前检查 child 原路径与解析路径，网络/reparse 候选只报告真实位置、不读取或移动，FileService 62 passed。
- 2026-07-30 当前 Review Finding Medium-7 闭环：canonical override、writer NaN 与 validator NaN 三个测试初始 3 failed；全部 writer JSON 使用 `allow_nan=False`，validator 使用 strict `parse_constant`，session-start/protocol/session adapters 剥离或覆盖保留字段，Writer+FileService 106 passed。
- 2026-07-30 当前 13 项 Review Findings 最终 Gate：13/13 checkbox 已闭环、Story 无剩余未勾选项；Story 定向集合 325 passed，全量 `python -m pytest -q` 565 passed，`python -m ruff check .` 通过；相对 baseline `e401a319d1da93302bcc8908fc9ed7d161b3da08`、以独立临时 index 纳入全部未跟踪文件的 `git diff --cached --check` 返回 0，真实 index 保持为空。
- 本轮并发/teardown/recovery 使用 `threading.Event`、owner ack、受控 reader、fake filesystem/network 判定与 fault injection；全程仅使用 MockHAL/fake，未连接或运行真实 NI HIL。Story 与 sprint-status 保持 `review`，未 reset、未提交、未推送。
- 2026-07-30 修复后独立复审 RED 证据：4 High 首轮分别得到 1、1、2、1 failed；7 Medium 首轮分别得到 1、1、2、10 failed/1 already-rejected、5、1、1 failed，共 26 个确定性失败断言。并发顺序由 `threading.Event`、owner lock gate、fake monotonic clock 与 fault injection 固定，未以 sleep 竞争作为唯一断言。
- High-1 GREEN：`close()` 记录共享 monotonic deadline，writer 在 complete 终态提交前于同一 `_state_lock` 仲裁 timeout；`test_close_timeout_arbitrates_before_late_complete_result` 与既有 timeout/idempotency 组合 4 passed。
- High-2 GREEN：Hardware recorder bind 新增 cancellation token，timeout 与 owner handler 在 `_ttl_control_lock` 下统一判定；迟到 payload 不再绑定旧 ingress，相关 3 passed。
- High-3 GREEN：publish rollback 在 staging 重现时也写 incomplete marker；marker 写入失败则删除/隔离 complete manifest，并保留最终目录回退路径；两条新增 fault 路径及既有 rollback/timeout 共 4 passed。
- High-4 GREEN：重复 receipt 现在写无 timing 重复的 `duplicate_receipt_ignored` protocol envelope，保留已接受 producer sequence 且完整 receipt 仍只持久化一次；相关 2 passed，complete validator 通过。
- Medium-1 GREEN：Actuation fence 仅在 `post_fence()` 成功时解绑，并把真实 accepted 值回传 ACK；相关 3 passed。
- Medium-2 GREEN：呼吸样本的 `expected_open_ns` 同步写入 `open_requested`/`exhale_trigger` 的 `ProtocolGateEvent.monotonic_ns`；相关 2 passed。
- Medium-3 GREEN：初始 marker 与 collision identity 全失败时保留 recovery manifest 或登记精确 orphan staging；scanner 可报告并隔离，两类多重 fault 与既有 collision 测试 4 passed。
- Medium-4 GREEN：validator 新增 receipt timing/epoch/sequence/valve/stale/action/category/measurement 语义及 quality p95/transitions 严格校验；11 类变异与真实 writer round-trip 共 12 passed。
- Medium-5 GREEN：hash、byte、raw/log count、last-sequence 和汇总失败均传播 manifest `last_session_sequence`；5 类失败路径 5 passed。
- Medium-6 GREEN：raw validator 捕获 `csv.Error` 并返回带 last sequence 的不完整结果，recovery scan 继续报告和隔离；1 passed。
- Medium-7 GREEN：`__999` collision staging 的 ownership marker 纳入 240 UTF-16 code unit 预算；长根目录边界与既有预算测试 2 passed。
- 本轮最终 Gate：受影响核心测试 307 passed；Story 定向集合 409 passed；全量 `python -m pytest -q` 592 passed；`python -m ruff check .` 通过；相对 baseline `e401a319d1da93302bcc8908fc9ed7d161b3da08` 的独立临时 index 纳入全部未跟踪文件后共 30 个 diff entries，`git diff --cached --check` 返回 0，真实 index 前后不变且为 0 entries。
- 本轮全程仅使用 MockHAL、fake clock/filesystem、结构化变异与自动化 fault injection，未连接或运行真实 NI HIL。Story 与 sprint-status 均保持 `review`；未 reset、未提交、未推送。
- 2026-07-30 HIL review continuation 已登记：首个正式 open jitter `33.0973 ms` 触发 severe safety close，21/21 配置目标关闭，run 中止于正常 open `1/200`、close `0/200`；400/400 与全部 p95 Gate 未完成。中止 bundle 的 manifest 完整性字段匹配且无 dropped/fence 缺口，但 validator 以“receipt valve 无效。”拒绝合法 master `valve=0`。Task 10 保持未完成，等待自动化修复与新的真实 HIL 全通过。
- Task 10 首动作根因与修复：旧 runner 在 owner 接受 manual trigger 之前先注入 AI exhale batch；优先级 trigger 越过既有 AI backlog 后，旧样本仍能以过期 expected deadline 启动首个正式 open。executor 现以 owner 接受 trigger 的 monotonic cutover 丢弃此前结构化 backlog，runner 等待 WAITING_EXHALE ACK 后才注入正式 stimulus；recorder-ready 也改为 owner ACK，避免 bind generation 与首命令交错。未排除正式样本、未增加 warm-up、未调整阈值或统计口径。
- Task 10 主阀契约修复：writer 与 complete validator 共用严格 receipt contract；`valve=0` 仅在 target 精确匹配配置的 master device/line 且 category 为 `SAFETY`/`WARMUP`/`MASTER` 时合法，其他零值、负值、bool、错误 target 或普通 valve 类别继续 fail closed。新增真实 SessionWriter round-trip 与当前中止 bundle 只读回归。
- Task 10 runner 修复：`scripts/hil_actuation_benchmark.py` 新增原生 Story 3.5 recording 模式，建立/绑定 SessionWriter 与 hardware/actuation/controller producer，显示 ProtocolView，记录真实/模拟标识和 candidate，收集 fences 并验证最终或中止 bundle；保留 live 明确授权、初始全关和 finally 全关，中止会阻断后续安全场景。
- Task 10 自动化 Gate：新增测试首轮 9 failed 后 GREEN；Story 定向集合 433 passed；全量 `python -m pytest -q` 604 passed；`python -m ruff check .` 与 `git diff --check` 通过。另以 MockHAL 完成 runner 20 open/20 close smoke，final/rolling p95、安全场景和 bundle validator 均通过。未连接或运行真实 NI HIL。
- Task 10 仍未完成：新的真实 Windows/NI 单次正式验收尚未执行，必须以新 candidate 达成 open/close 各 200、全部 p95 Gate、安全场景及 complete bundle 后才能关闭。Story 与 sprint-status 保持 `review`；未提交、未推送。
- 独立复审 High-1 闭环：writer 将 canonical identity 缓存从 set 改为完整 frozen receipt 映射；仅 dataclass 全内容相等才写 `duplicate_receipt_ignored`，valve/action/category/target/result 等冲突立即锁存 writer failure，禁止 complete publish。
- 独立复审 High-2 闭环：runner 为 SessionWriter 注入与 MainController 同序的 failure callback；先同步更新 `recording_ready=False/recorder_failed=True/generation`，再通知 ActuationWorker recorder failure 与 stop，普通及 warmup 开阀立即受 interlock 阻断，安全关闭仍可执行。
- 独立复审 High-3 闭环：aggregate、全部 rolling、final-window p95 和样本完整性集中为正式 performance Gate；在任何安全场景前判定，失败先执行 `performance-gate-abort` 全关再抛出，中止后不进入 stop/low-flow/severe/shutdown。
- 独立复审 High-4 闭环：live Story 3.5 candidate 在创建证据目录前通过 Git 验证 commit object、当前 HEAD 精确匹配及 index/worktree（含 untracked）clean；simulation 仍明确标记 mock/simulation，允许独立 smoke SHA。
- 独立复审 Medium-1 闭环：共享 master contract 允许精确配置 target 上的 `SAFETY/CLOSE`、`WARMUP|MANUAL|PRETEST/OPEN` 与 `MASTER/OPEN|CLOSE`；错误 target、SAFETY open、WARMUP/MANUAL/PRETEST close、NORMAL valve=0 继续 fail closed。MANUAL/PRETEST 真实 SessionWriter round-trip 与 complete validator 均通过。
- 独立复审 Medium-2 闭环：benchmark 通过性能 Gate 后先以其即时 quality snapshot、三 producer fences 和 validator 封存独立 bundle；四个安全协议各自使用独立 recording session/document/bundle，禁止活动 session 加载不同 ProtocolDocument。MockHAL 20/20 smoke 生成 benchmark + 4 safety 共 5 个 complete bundle，benchmark `session_closed.final_quality` 保留正式样本 20 open/20 close，不受 severe/shutdown metrics reset 覆盖。
- 本次独立复审最终 Gate：新增/扩展确定性测试全部 GREEN；Story 定向集合 454 passed；全量 `python -m pytest -q` 625 passed；`python -m ruff check .` 与 `git diff --check` 通过。只使用 MockHAL、temp Git repo 与只读历史证据，未连接或运行真实 NI HIL。
- 新的真实 Windows/NI Acceptance 仍未执行且保持未勾选；Task 10 父项、Story 与 sprint-status 继续为 `review`。未提交、未推送。
- 2026-07-30 第二次真实 Windows/NI Acceptance 失败已登记并完成自动化 follow-up：正常 open/close 各 `68/200` 后，valve 9 close jitter `30.3166 ms` 触发 severe 中止；性能样本与四个安全场景均未完成。只读复核将延迟定位到 owner dispatch（`29.6321 ms`）而非 DAQ write（`0.6845 ms`），并确认历史 bundle 已由 finally 保存 21 条 `shutdown-close`，真正契约缺口为 close-severe 未自行生成 `severe-close`。现已完成 deadline owner reservation、diagnostic I/O 隔离、延迟 close trace、severe 全目标关闭/有界 drain/fence/finalize 及 incomplete bundle fail-closed；Story 定向 462 passed、全量 633 passed、ruff/diff check 通过。新的真实 HIL Acceptance 仍未完成；Task 10、Story 与 sprint-status 继续保持 `review`。
- 2026-07-30 candidate `db5271352eb7bf38f38eb3f56657d18d5ecbda45` 的真实 Windows/NI Acceptance 通过：独立 preflight 确认 Dev1/Dev2、COM6 与 MFC 流量门禁通过；正式 `scripts/hil_actuation_benchmark.py --story-3-5-recording` run 位于 `logs/benchmarks/story-3-5-20260730-200124-live`，初始安全全关 `21/21`，正常 open/close 各 `200/200` 且零失败。open/close/combined aggregate p95 分别为 `12.8520/10.4800/12.7632 ms`，最大 rolling p95 分别为 `12.9486/10.8832/12.8520 ms`，final-last-100 p95 分别为 `12.9205/10.4124/12.8432 ms`，全部严格 `<20 ms`。stop、LOW_FLOW、severe、shutdown 四个安全场景均取得 `21/21` 配置目标关闭；五个独立 bundle 均有 hardware/actuation/controller fences、`dropped_count=0`、complete manifest，runner validator 与事后独立只读完整 validator 全部通过。未连接受试者、未执行破坏性故障测试、未修改代码、未提交、未推送；Story 与 sprint-status 保持 `review`。
- candidate `e37578d15fc4eeb3679d08909d861faf9deac67f` 独立复审 4 项 patch 已闭环：CLOSE-only deadline reservation、owner teardown fail-closed、severe 原 trial identity 与 latency trace command 生命周期均有确定性回归；全量 `636 passed`、ruff 与 `git diff --check` 通过。实现修复后工作树尚未形成新的 40 位 candidate，真实 Windows/NI Acceptance 仍未执行。
- 2026-07-30 最终 closure：确认 HIL candidate 为当前 `HEAD=db5271352eb7bf38f38eb3f56657d18d5ecbda45`；成功 preflight 位于 `logs/benchmarks/story-3-4-20260730-200036-live/`，正式 Story 3.5 证据位于 `logs/benchmarks/story-3-5-20260730-200124-live/`，执行时间为 `2026-07-30T20:01:24.100+08:00` 至 `20:03:04.526+08:00`。硬件为 Dev1 USB-6001 `34887710`、Dev2 USB-6001 `34887797`、COM6/MFC `1500.0 sccm`、主阀 `Dev2/P1.0`；正式参数为 valve 1/9/13、100 ms duration、250 ms inter-trial、200 cycles、可见 ProtocolView 与真实 session recording。
- 最终 HIL Gate：正常回执 open/close 各 `200/200`，合计 `400/400 success`；open/close/combined aggregate p95=`12.8520/10.4800/12.7632 ms`，最大 rolling p95=`12.9486/10.8832/12.8520 ms`，final-last-100 p95=`12.9205/10.4124/12.8432 ms`，全部严格 `<20 ms`。初始全关、stop、LOW_FLOW、severe、shutdown 均为 `21/21` 目标关闭且无缺失/失败。
- 最终 bundle Gate：以真实主阀配置调用生产 validator，benchmark/stop/LOW_FLOW/severe/shutdown 五个 bundle 均 `complete=True`，last sequence 分别为 `2049/55/121/53/54`；独立重算 raw count=`7604/6/181/8/4`、log count=`2049/55/121/53/54`、raw/log SHA-256、byte、连续 sequence 与 hardware/actuation/controller fence 均和 manifest/`session_closed` 一致，五个 bundle 全部 `dropped_count=0`。完整 hash 与证据索引见 `docs/sprint-artifacts/evidence/story-3-5-hil-closure-20260730.md`。
- 最终软件 Gate：`python -m pytest -q` 为 `636 passed in 16.93s`；`python -m ruff check .` 为 `All checks passed!`；相对 baseline `e401a319d1da93302bcc8908fc9ed7d161b3da08` 的独立临时 index 纳入 36 个 diff entries 后，`git diff --cached --check` 返回 0，真实 index 前后均为 0 entries 且未改变。软件 Gate 与 HIL Gate 均通过，Senior Developer Review Outcome 更新为 `Approve`，Story 与 sprint-status 更新为 `done`。

### File List

- `docs/sprint-artifacts/3-5-session-file-naming-and-logging.md`（本 Story 新建）
- `docs/sprint-artifacts/sprint-status.yaml`
- `app/models/session.py`
- `app/models/protocol_execution.py`
- `app/models/__init__.py`
- `app/services/session_file_service.py`
- `app/services/protocol_executor.py`
- `app/services/__init__.py`
- `app/workers/session_writer.py`
- `app/workers/__init__.py`
- `app/workers/hardware_worker.py`
- `app/workers/actuation_worker.py`
- `app/controllers/main_controller.py`
- `app/main.py`
- `app/views/main_window.py`
- `app/views/session_view.py`
- `app/views/__init__.py`
- `config/default_config.json`
- `docs/architecture.md`
- `docs/project-structure.md`
- `tests/test_session_file_service.py`
- `tests/test_session_writer.py`
- `tests/conftest.py`
- `tests/test_protocol_trigger_integration.py`
- `tests/test_protocol_executor.py`
- `tests/test_integration_gating.py`
- `tests/test_actuation_worker.py`
- `tests/test_app.py`
- `tests/test_shutdown_actuation.py`
- `tests/test_session_view.py`
- `scripts/hil_actuation_benchmark.py`
- `tests/test_hil_actuation_benchmark.py`
- `docs/sprint-artifacts/evidence/story-3-5-hil-closure-20260730.md`（最终 HIL closure 索引；绑定本地忽略原始 run 的硬件、时间、指标及逐 bundle hash/count/sequence）
- `logs/benchmarks/story-3-4-20260730-200036-live/`（本地忽略、只读的成功 preflight 原始证据）
- `logs/benchmarks/story-3-5-20260730-200124-live/`（本地忽略、只读的正式 HIL 原始 receipts/trace/summary 与五个 session bundle）

### Change Log

- 2026-07-27：实现 Story 3.5 会话命名、事务式 bundle、独立 session writer、结构化日志、failure latch、producer fence、中文文件页与 recovery；自动化验收通过，状态更新为 `review`。
- 2026-07-27：处理 code review findings 18 项；新增终态/publish 门禁、owner 串行化、严格 recovery/manifest 验证、异步扫描与确定性 teardown；定向 224 passed、全量 472 passed，状态保持 `review`。
- 2026-07-29：独立 code review 发现 8 项 High、5 项 Medium，登记为未完成 Task 8；未修改实现，状态保持 `review`。
- 2026-07-29：以 code review continuation 完成 Task 8 的 8 High、5 Medium 与 Gate；采用方案 A 锁定实际会话开始 timestamp/最终路径并二次确认启动记录，定向 244 passed、全量 492 passed，状态恢复为 `review`。
- 2026-07-29：完成 Task 9 的 4 High、2 Medium、单实例方案 A 与 Gate；定向 259 passed、全量 507 passed，ruff 与含全部未跟踪文件的 baseline diff check 通过；真实 NI HIL 未运行，状态保持 `review`。
- 2026-07-30：完成当前 `Review Findings` 的 4 High、9 Medium；新增 22 个确定性缺陷断言，Story 定向 281 passed、全量 529 passed、ruff 通过；仅使用 MockHAL/fake/fault injection，Story 与 sprint-status 保持 `review`。
- 2026-07-30：完成新登记的 13 项 Review Findings（6 High、7 Medium）；新增/扩展 37 个确定性 RED 场景并逐项闭环，Story 定向 325 passed、全量 565 passed、ruff 与含全部未跟踪文件的 baseline 临时-index diff check 通过；未运行真实 NI HIL，状态保持 `review`。
- 2026-07-30：完成“修复后独立复审”11 项 findings（4 High、7 Medium）；26 个确定性 RED 断言闭环，Story 定向 409 passed、全量 592 passed、ruff 与含全部未跟踪文件的 baseline 临时-index diff check 通过；Story 与 sprint-status 保持 `review`。
- 2026-07-30：登记 candidate `c9baff6e6910266621fb2a36c6b62f880f42a27e` 的真实 Windows/NI HIL 失败：首个正式动作 jitter `33.0973 ms` 后 severe 中止并安全关闭 21/21 目标；记录 master `valve=0` complete-bundle validator 契约缺陷与正式 HIL runner 缺口，新增未完成 Task 10。Story 与 sprint-status 保持 `review`。
- 2026-07-30：完成 Task 10 的自动化 review continuation：修复 owner trigger cutover 前 AI backlog 被首个正式动作消费、统一 master `valve=0` writer/validator 契约，并将 Story 3.5 SessionWriter/fence/final-validator 生命周期纳入正式 HIL runner；新增确定性 RED→GREEN、当前中止 bundle 只读回归与 MockHAL smoke。真实 Windows/NI 验收仍未运行，Task 10、Story 与 sprint-status 保持 `review`，未提交、未推送。
- 2026-07-30：处理 Story 3.5 HIL follow-up 独立复审的 4 High、2 Medium：冲突 duplicate receipt fail closed、writer failure 直达 interlock、性能 Gate 前置、live candidate Git 绑定、MANUAL/PRETEST master_prepare 契约及单 session 单协议/质量隔离全部闭环；定向 454 passed、全量 625 passed、ruff/diff check 通过。真实 HIL Acceptance 仍未运行，Task 10、Story 与 sprint-status 保持 `review`，未提交、未推送。
- 2026-07-30：登记并处理 candidate `7c4d971aa370056b8a70cee3592344bb54dd7ad7` 的第二次真实 Windows/NI HIL 失败：正常 open/close 各 `68/200` 后 `protocol-9-close-1000283` jitter `30.3166 ms` 触发 severe 中止。只读 trace 将超限定位到 owner dispatch 并暴露延迟 close trace 缺口；只读 bundle 复核确认已有 21 条 finally `shutdown-close`，同时证明 close-severe owner 分支未自行全关。完成 deadline reservation、diagnostic receipt I/O 隔离、trace 补齐、severe 21/21 有界 drain/fence/finalize 与 incomplete bundle fail-closed；定向 462 passed、全量 633 passed、ruff/diff check 通过。新的真实 HIL Acceptance 仍未完成，Story、Task 10 与 sprint-status 保持 `review`。
- 2026-07-30：完成 candidate `db5271352eb7bf38f38eb3f56657d18d5ecbda45` 的真实 Windows/NI Acceptance：独立 preflight、21/21 初始全关、200 open + 200 close、全部 aggregate/rolling/final-last-100 p95 Gate、四个安全场景、三 producer fences 与五个 complete bundle validator 全部通过；Task 10 完成，Story 与 sprint-status 保持 `review`，未修改代码、未提交、未推送。
- 2026-07-30：完成 Story 3.5 最终 closure：复核 candidate、400/400 receipt、全部 p95、安全全关、五个 bundle 的 hash/count/sequence/fence 及 `dropped_count=0`；新增可提交 HIL closure 证据索引，全量 `636 passed`、ruff 与 baseline 独立临时-index diff check 通过；Senior Developer Review Outcome 更新为 `Approve`，Story 与 sprint-status 更新为 `done`。

## Senior Developer Review (AI)

### Outcome

Approve

### Review Follow-ups (AI)

- Task 8、Task 9、Task 10 及全部 Review Findings 已闭环，无未完成 review checkbox。
- candidate `db5271352eb7bf38f38eb3f56657d18d5ecbda45` 的真实 Windows/NI HIL 已通过；400/400 receipt、全部 p95、安全关闭、五个 bundle validator/hash/count/sequence/fence 与 `dropped_count=0` 均已复核。
- 最终全量 `636 passed`，ruff 与 baseline 独立临时-index diff check 通过；软件 Gate 与 HIL Gate 均通过，批准 Story 3.5 关闭为 `done`。
