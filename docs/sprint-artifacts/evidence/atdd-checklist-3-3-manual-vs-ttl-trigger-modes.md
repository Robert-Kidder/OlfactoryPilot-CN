---
stepsCompleted: ['step-01-preflight-and-context', 'step-02-generation-mode', 'step-03-test-strategy', 'step-04-generate-tests', 'step-04c-aggregate', 'step-05-validate-and-complete']
lastStep: 'step-05-validate-and-complete'
lastSaved: '2026-07-18'
storyId: '3.3'
storyKey: '3-3-manual-vs-ttl-trigger-modes'
storyFile: 'docs/sprint-artifacts/3-3-manual-vs-ttl-trigger-modes.md'
atddChecklistPath: 'docs/sprint-artifacts/evidence/atdd-checklist-3-3-manual-vs-ttl-trigger-modes.md'
archivedToProjectDocs: '2026-07-30'
generatedTestFiles: []
inputDocuments:
  - 'docs/project-context.md'
  - 'docs/sprint-artifacts/3-3-manual-vs-ttl-trigger-modes.md'
  - '_bmad/tea/config.yaml'
  - 'pytest.ini'
  - 'tests/conftest.py'
  - 'tests/test_protocol_executor.py'
  - 'tests/test_integration_gating.py'
  - 'tests/test_protocol_view.py'
  - '.agents/skills/bmad-testarch-atdd/resources/knowledge/data-factories.md'
  - '.agents/skills/bmad-testarch-atdd/resources/knowledge/component-tdd.md'
  - '.agents/skills/bmad-testarch-atdd/resources/knowledge/test-quality.md'
  - '.agents/skills/bmad-testarch-atdd/resources/knowledge/test-healing-patterns.md'
  - '.agents/skills/bmad-testarch-atdd/resources/knowledge/test-levels-framework.md'
  - '.agents/skills/bmad-testarch-atdd/resources/knowledge/test-priorities-matrix.md'
  - '.agents/skills/bmad-testarch-atdd/resources/knowledge/ci-burn-in.md'
  - '.agents/skills/bmad-testarch-atdd/resources/knowledge/overview.md'
  - '.agents/skills/bmad-testarch-atdd/resources/knowledge/api-request.md'
  - '.agents/skills/bmad-testarch-atdd/resources/knowledge/auth-session.md'
  - '.agents/skills/bmad-testarch-atdd/resources/knowledge/recurse.md'
---

# Story 3.3 ATDD 红灯测试清单

## 第 1 步：前置检查与上下文

- 检测栈：`backend`（Python + pytest）。项目已有 `pytest.ini`、`tests/conftest.py` 与既有 pytest 测试目录。
- Story 准备度：`ready-for-dev`；10 条验收标准清楚覆盖触发、readiness、安全清理、UI、日志和回归。
- 开发环境：解释器可用；当前 shell 为 Python 3.13，项目目标基线为 Python 3.11，实施阶段应在 3.11 环境完成最终验证。
- 主要受影响边界：`ProtocolExecutor` 状态机、TTL 边沿服务、HAL/Worker 输入契约、`MainController` 编排、`ProtocolView` snapshot 渲染。
- 关键测试约束：使用可注入 clock 和确定性样本，不访问真实硬件、不使用真实 `sleep`；断言必须直接验证状态不变量、阀门调用和中文原因。
- 测试分层：边沿/去抖与执行状态机用纯单元测试；readiness、queued pulse、停止/重置原子性用集成测试；UI 仅验证意图 signal、互斥控件和 capability 渲染。
- 风险优先级：可能导致错误开阀、重复推进、陈旧脉冲消费或清理失败后状态丢失的场景均为 P0。

## 第 2 步：生成模式

- 选择：AI 生成。
- 原因：检测栈为 `backend`，验收标准已明确；测试对象主要是纯服务、状态机、HAL/Worker 契约和 controller 边界，无需浏览器录制。

## 第 3 步：测试策略

### 分层原则

- Unit：穷举 `ProtocolExecutor` 状态转换、统一 readiness 守卫、TTL 迟滞/去抖/边沿与事件身份；这是主要红灯层。
- Integration：只验证 HAL → Worker → Controller → Executor 的数据契约、queued pulse 竞态、协议替换与全局清理边界。
- UI（pytest-qt 组件级）：只验证 View 发出意图、互斥选择和 snapshot capability 渲染，不在 UI 层重复状态机断言。
- 不新增浏览器 E2E/API/Contract 测试；本 Story 是本地桌面硬件控制链路，不存在 HTTP 契约。

### 验收标准映射

| 测试 ID | 优先级 | 层级 | 红灯场景与核心断言 | 覆盖 AC |
|---|---:|---|---|---:|
| 3.3-UNIT-001 | P0 | Unit / Executor | `start()` 在全部 readiness 满足时进入 `WAITING_TRIGGER`，模式取当前 frozen trial 声明；阀门、呼吸等待起点和重试计数均未启动。 | 1, 9, 10 |
| 3.3-UNIT-002 | P0 | Unit / Executor | manual 来源在 `manual + WAITING_TRIGGER` 仅推进一次到 `WAITING_EXHALE`；双击/重复 signal 不改变 trial、epoch、等待起点且不开阀；随后只有呼气阈值才能开阀。 | 1, 2, 9 |
| 3.3-UNIT-003 | P0 | Unit / Executor | 参数化拒绝 manual：无协议、未开始、错误模式、非 `SAFE`、未连接、自检失败、流量未准备、`blocked/stopped/completed`；返回可操作中文原因且所有执行不变量不变。 | 2, 10 |
| 3.3-UNIT-004 | P0 | Unit / Executor | 有效 TTL pulse 携带采集时间戳、captured epoch/sequence，在 `ttl + WAITING_TRIGGER` 仅推进一次；事件保留原始时间戳，仍需呼气后才开阀。 | 1, 3, 8 |
| 3.3-UNIT-005 | P0 | Unit / Executor | 参数化拒绝 TTL：错误来源/模式、旧 epoch、重复 sequence、非等待态、非 `SAFE`、TTL input 未就绪；trial、两类等待起点、retry、epoch、active valve 均不变。 | 3, 4, 10 |
| 3.3-UNIT-006 | P0 | Unit / Executor | 模式切换矩阵：`READY` 仅写 override 并保持 READY；合法运行等待态清除旧锁存、触发/呼吸等待起点和 retry 后回 `WAITING_TRIGGER`；选择当前模式完全幂等。 | 5 |
| 3.3-UNIT-007 | P0 | Unit / Executor | `TRIGGERED` 切换先安全关阀：关闭成功才提交新模式；关闭失败保留旧 mode/epoch/trial/active valve 并进入 `BLOCKED`。 | 5, 9, 10 |
| 3.3-UNIT-008 | P0 | Unit / Executor | 模式切换拒绝矩阵：`idle/blocked/stopped/completed`、非 `SAFE`、硬件/流量未就绪均保持原状态、模式、epoch 与活动阀；恢复 SAFE 不自动重试。 | 5, 10 |
| 3.3-UNIT-009 | P0 | Unit / Executor | override 不修改 frozen `ProtocolDocument/ProtocolTrial`；完成/跳过当前 trial 后清除 override，并恢复下一 trial 的声明模式（manual/ttl 混合协议）。 | 5, 9 |
| 3.3-UNIT-010 | P0 | Unit / Executor | stop、安全中断、disconnect 清除/失效 trigger arm 与 queued pulse；恢复 SAFE 后旧 pulse 仍拒绝，必须显式重新 start/rearm。 | 6, 10 |
| 3.3-UNIT-011 | P0 | Unit / Executor | `reset/start/rearm` 在残留 `active_valve` 时拒绝；close_failed 后旧 document/trial/mode/epoch/active valve 不丢失，随后 stop 可重试关闭。 | 5, 6, 9, 10 |
| 3.3-UNIT-012 | P0 | Unit / TTL service | 确定性序列 `low → high → high...` 只产生一个 pulse；只有低于 low threshold 后才重新布防，下一次 rise 才产生第二个 pulse。 | 3, 4 |
| 3.3-UNIT-013 | P0 | Unit / TTL service | 阈值边界与迟滞：介于 low/high 的样本不改变 latch；恰好跨 high/low 的语义固定；持续高电平不形成日志风暴。 | 3, 4 |
| 3.3-UNIT-014 | P0 | Unit / TTL service | 用注入 clock 覆盖 bounce：短于 debounce 的高/低抖动不发射/不重新布防，满足 debounce 后只发一次；测试禁止真实 `sleep`。 | 3 |
| 3.3-UNIT-015 | P0 | Unit / TTL service | `ttl → manual → ttl` 且 AI6 全程高电平时不把旧高电平当新 rise；必须先观察有效低电平和去抖，再接受下一上升沿。 | 3, 4, 5 |
| 3.3-UNIT-016 | P0 | Unit / TTL service | pulse payload 在布防/采样时冻结 epoch、sequence、采集时间；后续模式切换不能修改 payload 身份。 | 3, 4, 8 |
| 3.3-UNIT-017 | P1 | Unit / Config + TTL service | `NaN/inf`、high≤low、负 debounce/非法 poll rate 使用安全默认值并产生中文警告；输入读取异常是显式错误，不伪装为低电平/无 pulse。 | 3, 8, 9 |
| 3.3-UNIT-018 | P0 | Unit / Executor | 无有效协议时分别调用 `start`、manual trigger、TTL pulse；三入口均返回中文拒绝，保持 IDLE/原快照且绝不布防、推进或调用阀门。 | 2, 4, 10 |
| 3.3-INT-001 | P0 | Integration / HAL + Worker | Mock 与 Real HAL 暴露同一 AI6 输入契约；Mock 可注入电平；Worker 对一个物理 rise 仅发一个不可变 payload，原样携带 timestamp/epoch/sequence。 | 3, 4, 9 |
| 3.3-INT-002 | P0 | Integration / Worker | TTL 读取异常走独立中文 error signal/事件并阻断执行，不产生 pulse；TTL 采样调度达到独立配置频率且不阻塞既有呼吸/telemetry。 | 3, 6, 10 |
| 3.3-INT-003 | P0 | Integration / Controller | worker TTL pulse/error signal 在初始化只连接一次；manual/TTL handler 转发同一 readiness 快照与原始 payload，不补当前 epoch、不直接写阀门。 | 2, 3, 4, 9, 10 |
| 3.3-INT-004 | P0 | Integration / Controller | 模式切换前已排队、切换后才送达的 pulse 被 executor 拒绝；错误模式、重复 TTL 均不调用阀门 writer。 | 4, 5 |
| 3.3-INT-005 | P0 | Integration / Controller | 协议替换：候选解析失败不清理；解析成功后先清理旧态，成功才原子替换；关闭/清理失败保留旧 protocol/executor/UI 可执行事实。 | 6, 9, 10 |
| 3.3-INT-006 | P0 | Integration / Controller | 协议停止、全局停止/重置、硬件断连、非 SAFE 使 epoch 失效并安全关阀；操作完成后无 `ttl_armed`、等待起点或未关闭活动状态残留。 | 6, 10 |
| 3.3-INT-007 | P1 | Integration / Logging | `ProtocolGateEvent.as_dict()` 与 `protocol_execution` logger 包含 trial、协议/当前模式、source、result、safety、中文 message；TTL 使用采集时间戳。 | 4, 8 |
| 3.3-UI-001 | P1 | Component / View | 模式选择互斥；“手动触发”为独立动作；不存在生产环境 TTL 模拟按钮；View 只发 intent signal。 | 7, 9 |
| 3.3-UI-002 | P1 | Component / View | capability 参数化反映协议、状态、SAFE、连接、自检、流量、TTL input readiness；仅 `manual + WAITING_TRIGGER + ready` 启用手动触发。 | 2, 3, 7, 10 |
| 3.3-UI-003 | P1 | Component / View | 中文展示协议模式、当前模式、TTL 布防、呼吸门控、trial/阀门/等待时间/最近事件，并保留既有状态文案。 | 7, 9 |
| 3.3-REG-001 | P0 | Regression | 3.2 characterization 在 `start()` 后先注入匹配 trigger，再继续验证呼吸超时 skip/retry、开/关阀、计划/实际时长与 close_failed 恢复。 | 1, 9 |
| 3.3-REG-002 | P0 | Regression | parser frozen/加载失败原子性、ValveService `safety_close=True` 只关不打开、非 SAFE 与 BLOCKED 无日志风暴保持通过。 | 6, 9, 10 |

### 统一不变量断言模板

每个 `rejected/ignored` 场景都应在动作前后捕获快照并显式断言：`trial_index`、当前/协议模式、`arm_epoch`、`waiting_trigger_started_at`、`waiting_started_at`、`retry_count`、`active_valve` 与阀门 writer 调用次数均未改变。不能只断言状态枚举或按钮禁用。

### 红灯确认

- 当前没有 `WAITING_TRIGGER`、统一 readiness 值对象、`accept_trigger()`、`set_trigger_mode()`、TTL service/payload/HAL 输入契约及相关 snapshot 字段，因此上述新增测试在实现前应因缺少 API/状态或断言不符而失败。
- 既有 `test_start_prepares_first_trial_without_opening_valve` 当前断言 `WAITING_EXHALE`，应先改成 `WAITING_TRIGGER` 红灯；3.2 后续测试需显式注入匹配 trigger，不能删减原安全断言来“适配”新状态。
- 红灯失败必须指向缺失行为；不要用 `xfail`、`skip`、条件分支或宽泛异常捕获隐藏失败。

## 第 4 步：红灯测试落位建议（仅设计，不生成测试代码）

用户范围明确要求只交付开发前测试设计与建议位置，因此本次不创建 `tests/*.py`、不创建 fixture 文件，也不修改 Story 或业务代码。标准工作流中的 Playwright API/E2E 工作者不适用于本地 PySide6/pytest 桌面项目；`generatedTestFiles` 保持空列表。

### 建议测试文件位置

| 建议路径 | 动作 | 放置的测试 ID | 说明 |
|---|---|---|---|
| `tests/test_protocol_executor.py` | 扩展 | UNIT-001～011、UNIT-018、REG-001 | 状态机、模式切换、readiness、重复/陈旧触发、无协议、stop/reset/close_failed；沿用现有 `_document()`、`_executor()` 风格并补参数化 factory。 |
| `tests/test_ttl_trigger_service.py` | 新增 | UNIT-012～017 | 纯 TTL detector：阈值、迟滞、上升沿、去抖、epoch/sequence、全程高电平竞态、非法配置和错误；不依赖 Qt/NI/真实时间。 |
| `tests/test_ttl_input.py` | 新增 | INT-001～002 | HAL/MockHAL/RealHAL 契约与 HardwareWorker signal/调度；用可控 Fake/Mock HAL，不连接真实 NI。 |
| `tests/test_protocol_trigger_integration.py` | 新增 | INT-003～007 | Controller readiness 组装、signal 只连一次、queued pulse、协议替换原子性、停止/重置/断连/安全中断与结构化日志。避免继续扩大已很大的 `tests/test_app.py`。 |
| `tests/test_protocol_view.py` | 扩展 | UI-001～003 | pytest-qt 组件测试：intent signal、互斥控件、中文文案、capability enablement；不断言 executor 内部字段。 |
| `tests/test_integration_gating.py` | 迁移/扩展 | REG-001 | 给现有门控集成路径在 `start()` 后补一次匹配 manual trigger，保留校准样本与 ValveService writer 断言。 |
| `tests/test_protocol_parser.py`、`tests/test_valve_service.py` | 仅回归 | REG-002 | 不复制新场景；作为 frozen 模型、加载原子性和 `safety_close` 回归集合。 |
| `tests/test_app.py` | 最小扩展 | 配置装配 smoke（如必要） | 只验证默认/local config 把 TTL 参数正确注入 HAL/Worker/service；核心配置边界仍放在 TTL service 单测。 |

### 建议 fixture / factory 边界

- `FakeClock`：提供 `now()` / `advance_ms()`；所有 timeout、debounce 和 pulse timestamp 测试使用它，禁止 `time.sleep()`。
- `make_protocol(*triggers)`：生成 frozen 的 manual/ttl 混合 `ProtocolDocument`；每个测试获得新对象，避免共享可变执行态。
- `make_readiness(**overrides)`：默认全 ready，只覆盖当前拒绝原因；参数化时每个 case 只破坏一个条件，保证中文原因可定位。
- `make_pulse(epoch, sequence, captured_at)`：生成 frozen payload；测试不得在 delivery 时重写 epoch/timestamp。
- `ValveWriterSpy(close_outcomes=...)`：记录 `(channel, open_state)` 并可精确模拟首次关闭失败、随后成功；断言保留 `active_valve`。
- `TtlSampleSource` / Fake HAL：按调用顺序返回电平或抛出读取异常，便于验证 worker 不把异常吞成低电平。
- 上述 helper 初期可放在各测试文件顶部；只有至少两个测试模块真正复用时，再新增 `tests/helpers/protocol_trigger_factories.py`，不要把领域 helper 塞进全局 `tests/conftest.py`。

### 建议参数化矩阵

1. 统一 readiness：`has_protocol`、`connected`、`hardware_ready`、`flow_setpoints_ready`、`safety_state`；TTL 入口额外加 `ttl_input_ready`。
2. executor 状态：`IDLE/READY/WAITING_TRIGGER/WAITING_EXHALE/TRIGGERED/BLOCKED/STOPPED/COMPLETED`，每个动作只列合法态，其余统一验证拒绝不变量。
3. 触发身份：正确 epoch + 新 sequence、旧 epoch、新 epoch + 重复 sequence、模式切换前 queued pulse、stop 后 queued pulse。
4. TTL 电平：低、高阈值临界值、中间迟滞区、持续高、短低 bounce、有效低、第二次 rise、`NaN/inf`、读取异常。

### 推荐红灯激活顺序

1. 先激活 UNIT-001～005，建立 `WAITING_TRIGGER`、readiness 和 trigger API 的外部契约。
2. 激活 UNIT-012～016，固定 detector/payload 契约，再接 HAL/Worker。
3. 激活 UNIT-006～011，完成模式切换和清理原子性。
4. 激活 INT-001～007，打通跨线程 queued event 与 controller 边界。
5. 最后激活 UI-001～003 和 REG-001～002，验证展示与 3.1/3.2 回归。

每个切片先运行指定测试并确认因目标行为缺失而失败，再实现到绿色；不要同时激活全部红灯导致失败信号不可诊断。

### 建议执行命令（实施阶段）

```powershell
python -m pytest -q tests/test_protocol_executor.py
python -m pytest -q tests/test_ttl_trigger_service.py
python -m pytest -q tests/test_ttl_input.py tests/test_protocol_trigger_integration.py
python -m pytest -q tests/test_integration_gating.py tests/test_protocol_view.py
python -m pytest -q tests/test_protocol_parser.py tests/test_valve_service.py
python -m pytest
python -m ruff check app tests
```

红灯阶段应先运行当前切片的单个文件或 `-k` 用例并保存预期失败证据；本设计阶段未创建测试文件，因此没有运行这些命令。

## 第 5 步：校验与交接

### 校验结论

- 通过：Story 已批准且 AC 1～10 均有至少一个明确测试映射。
- 通过：用户点名的手动推进、TTL 推进、模式切换清理、重复 TTL、迟滞/边沿/去抖、无协议、非 SAFE、硬件未就绪、停止/重置清理均有 P0 场景。
- 通过：主测试层级为 Unit；计划 30 个场景（P0 25、P1 5），Integration/UI 只覆盖边界，不重复穷举状态机。
- 通过：红灯失败原因、确定性 clock/sample、阀门 close_failed 恢复、不可变 pulse 身份和统一不变量断言均已明确。
- 通过：Story 元数据与手工交接路径已写入 frontmatter；没有修改用户的 Story 文件。
- 不适用：Playwright API/E2E、HTTP mock、`data-testid`、浏览器 session。本项目为 PySide6 + pytest 桌面项目。
- 按用户范围不执行：测试脚手架落盘、fixture 落盘、`test.skip()` 检查和红灯运行；`generatedTestFiles: []` 准确反映现状。
- 环境提醒：最终绿色/回归应使用项目目标 Python 3.11；当前检查 shell 为 Python 3.13。
- 工作区卫生：未启动浏览器、未创建随机 temp artifact；唯一新增交付物位于配置的 `_bmad-output/test-artifacts/`。

### 交接摘要

- Story：`3.3` / `3-3-manual-vs-ttl-trigger-modes`
- Story 文件：`docs/sprint-artifacts/3-3-manual-vs-ttl-trigger-modes.md`
- ATDD 清单：`_bmad-output/test-artifacts/atdd-checklist-3-3-manual-vs-ttl-trigger-modes.md`
- 已创建测试文件：0（按用户要求仅设计）
- 下一步：开发实施时按“推荐红灯激活顺序”逐片先写/激活 pytest 红灯，再实现业务代码；建议进入 `bmad-dev-story`，实现完成后再用 `bmad-testarch-automate` 扩大自动化覆盖。
