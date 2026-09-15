---
title: 'C.3b Alicat 串口帧边界与响应归属修复'
type: 'bugfix'
created: '2026-09-14'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '51e82a9aeaecf54f79484d895fce92da93056548'
context:
  - '{project-root}/docs/architecture.md'
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/sprint-artifacts/spec-c3b-hil-commissioning.md'
  - '{project-root}/docs/sprint-artifacts/evidence/c-3b-lss-readonly-diagnostic-2026-09-14.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Power-cycle 后三次 LSS timeout，随后 `aVE` 收到 `A U`。probe/RealHAL 按 LF `readline()`，setpoint 写又不消费设备返回，导致迟到/遗留 CR frame 污染下一命令，通信失败还可能被当成安全零流量。

**Approach:** 建立共享、串行、CR-framed 的 Alicat transaction；每条命令在同一锁内完成 TX、完整 RX 和 command-aware validation。首次打开先被动排空并确认 bounded quiet；timeout、partial、framing failure、错 ID/类型或其他 desync 均锁存整个 session 失败，关闭重建前不再 TX。

## Boundaries & Constraints

**Always:** 同一 COM 只允许一条命令在途。poll、VE、LSS、setpoint response 分别验证形态与 Unit ID；setpoint frame 必须先消费/验证，再独立 poll。transport desync 后，即使下一命令同 ID/类型也禁止发送，必须 close/reopen/resync。错误进入既有 fail-closed。保留 B→C→A、Global Stop 偏序、owner、receipt/deadline 和 UI 隔离。

**Never:** 不用 `reset_input_buffer()` 证明归属，不继续读下一帧、跳过 stale、重发相同 poll 或按内容猜归属，不把 timeout/解析失败返回为 `0.0`，不改设备配置、安全顺序、UI、HardwareProfile 或 NI lifecycle；本轮不碰 COM6、Real App、NI 或 HIL。

## I/O & Edge-Case Matrix

| 场景 | 输入 | 结果 | 失败处理 |
|---|---|---|---|
| CR frame | `A U\r`，无 LF | 完成一个 frame | 无 CR 为 partial |
| stale | LSS timeout；`aVE` 收 `A U\r` | VE 拒绝 | desync，停止 TX |
| 同类型 stale | old A poll timeout；迟到 A data；请求 new A poll | new poll 不得 TX | close/reopen/sync 后才可新请求 |
| setpoint | `as0\r`→data；`a\r`→poll | 两帧各归其请求 | 任一错则失败 |
| open sync | RX 有旧 bytes | bounded drain + quiet 后首发 | 不能同步则不发 |
| identity | `aLSS` 收 `B U\r` | 拒绝 | 不找下一帧 |

</frozen-after-approval>

## Code Map

- `app/services/alicat_serial.py` — 新共享 CR reader、initial sync、transaction identity、validators、typed failure；无 Qt/硬件依赖。
- `app/services/real_hal.py` — `read_flow/set_flow` 与 serial open/release 迁移；移除 `_write_serial/_query_serial`，错误不再伪装为零。
- `scripts/probe_alicat.py` — 复用共享 contract；默认仍只 poll，显式 `--set` 消费自身 response。
- `app/services/flow_service.py`、`app/workers/flow_worker.py` — 保持安全顺序/回执，验证 desync 后零 TX。
- `tests/test_alicat_serial.py`、`tests/test_app.py`、`tests/test_flow_worker.py`、`tests/test_startup_connection.py` — stateful fake 覆盖异类型与同类型 stale，以及产品路径回归。
- `docs/architecture.md`、`docs/project-context.md`、commissioning spec — 固化 contract，保持待实机复测。

## Tasks & Acceptance

**Execution:**
- [x] 实现 bounded sync、CR transaction、desync latch、validators 与低频 DEBUG audit。
- [x] 迁移 RealHAL/probe 全部 Alicat command；消费 setpoint response，禁止 timeout→0。
- [x] 用 stateful FakeSerial 覆盖 matrix，证明 old A poll timeout 后 new A poll 零 TX，直至 release/reopen/resync；覆盖 setpoint、startup/Global Stop zero 和无 retry。
- [x] 更新长期文档与 commissioning 状态，不声称实机通过。

**Acceptance Criteria:**
- Given 当前请求未取得完整匹配 CR frame，when 下一请求到达，then 不发送。
- Given setpoint response 成功，when verify poll 开始，then 前帧已消费且目标 setpoint 已校验。
- Given timeout/partial/framing/错 ID/类型或其他 desync，when 同 session 请求下一命令，then 零 TX、不发布成功/零流量、不 retry；仅新 session 可 bounded resync。
- Given old A poll timeout 且其合法 A data frame 迟到，when caller 请求 new A poll，then validator 不参与猜测归属且 new poll 不发送。
- Given startup 或 Global Stop zero，when 完成，then 每条物理命令都经同一 contract，原安全顺序和回执不变。

## Implementation Notes

- Primer 要求等当前 response 后再发下一命令；`S` 返回含新 setpoint 的 data frame，VE 返回 ID/firmware/date，LSS 返回 ID/mode。`alicat_timeout_s` 保持可配置；`0.2 s` 仅为兼容默认值，是尚未经当前三台设备重验的 provisional value。本轮不换另一 magic number，correctness 不依赖延长 timeout。下次只读 HIL 记录 Poll/VE/LSS latency 后再定默认值。
- 3.5-byte serial idle 表示设备收到 CR 后等待总线空闲再处理，不是 response completion deadline。Initial bounded sync 同时考虑 19200 8-N-1 character time、配置的 frame receive deadline和本次 delayed-response 风险，不以固定几毫秒 sleep 宣称旧响应不再到达；边界仍待下一次只读 latency evidence 验证。
- 审计 10 s Connect、2 s Global Stop 与 FlowWorker deadline；未来调整 frame deadline 必须显式重验上层预算。Initial sync 只在新 session 执行，Global Stop 不重复 sync。
- 新 session 暂以连续 `2 × frame deadline` quiet 覆盖已观察到的“超过一次 deadline 后才到达”风险，并给同样长度的第二个窗口容纳迟到帧、drain 与 quiet 重新计时；该比例策略与 Connect budget 校验复用同一函数，仍属下一轮 latency evidence 前的保守 provisional 策略，而非设备最大延迟保证。
- setpoint 的实际 ASCII wire target 保持既有三位小数精度；命令 response validator、独立 readback poll 与 probe 都校验同一量化值，避免合法设备回包被未量化目标误判为 desync。
- 仓库静态审计确认产品 runtime 与 `probe_alicat.py` 均无 `readline()`、`reset_input_buffer()`、旧 `_write_serial/_query_serial` 或 write-without-response 旁路。历史专用 `scripts/hil_story45_live.py` 仍保留旧读法；它不属于产品 runtime，本轮未执行，未来若重新授权该历史 HIL 工具必须单独复审。

## Spec Change Log

## Review Triage Log

- Independent R1 / high / direct-fix：initial quiet 原先仅一帧 deadline，不能覆盖本次 delayed response；改为共享的双-deadline quiet/双-quiet budget，并增加迟到重计时和无法 quiet 时零 TX 回归。
- Independent R2 / high / direct-fix：既有 session desync 且 transport closed 时，RealHAL 可能先自动创建新 Serial；改为优先拒绝既有 desync/意外关闭 session，只有显式 release 后才可重建。
- Independent R3 / medium / direct-fix：补充 FlowWorker SafeStop + stateful serial 回归，证明 A timeout 后后续 B/C/A 零 TX且不隐式重开。
- Independent R4 / high / direct-fix：三位小数命令却以未量化目标校验会制造 false desync；统一 wire target 并增加严格 tolerance 回归。
- Independent final：上述 finding 均关闭，no blocking findings；`0.2 s` 与 resync 边界仍须下一轮只读 HIL latency 验证，不是离线 blocker。
- Blind BH1 / false / rejected：产品调用全部由单一 `FlowWorker` owner 串行进入 `FlowService`，不存在两个合法 `set_flow` caller 在 response 与 poll 间并发插入；直接绕过 owner 不属于可达产品路径。
- Blind BH2 / high / patch：仅依赖 connection 既有 timeout 会让声明的 frame deadline 漂移；transaction 现在每次将 transport timeout 设为配置 deadline，并以 monotonic elapsed 拒绝超时后才完成的 frame，新增 slow-frame 回归。
- Blind BH3 / high / patch：NaN/Inf timeout 可令 sync 不终止；session、sync window 和 config budget 均增加 finite 校验及回归。
- Blind BH4 / medium / patch：只有五个数字而无 gas 的截断 data frame 原可通过；validator 现在要求非数字 gas 字段，新增 gasless 回归。
- Blind BH5 / maybe-false / rejected：额外 token 可能是多词 gas 名或官方状态字段，当前 authority 未给出可安全区分的完整 grammar，也未要求 Air-only runtime；需要官方 frame/status contract 才能判定，不能猜测拒绝。
- Blind BH6 / high / defer：默认 `alicat_setpoint_tolerance=0.05` 在 device unit 下可能接受约 50 sccm 偏差；该单位/默认值和 flow model 早于本次 framing 变更，且人工作业明确本轮不改 flow model，已记录 deferred work。
- Blind BH7 / medium / patch：`math.isclose` 默认相对容差削弱绝对 wire-target 校验；已明确 `rel_tol=0.0`。
- Blind BH8 / high / patch：非预期 validator exception 原会绕过 desync latch；现在统一转换为 response mismatch 并锁存，正常控制流异常仍不捕获 `BaseException`。
- Blind BH9 / false / rejected：close 失败时保留旧 session 会阻止不确定资源上的 reopen/TX，属于 fail-closed；清空引用反而可能在旧 handle 未释放时打开第二连接。
- Blind BH10 / low / rejected：兼容配置 `alicat_setpoint_verify_retries` 仍可出现非 1 值但已明确不执行 retry；强制拒绝当前现场兼容配置会造成无必要启动阻断，代码和 spec 已明确 deprecated/ignored 语义。
- Blind BH11 / high / defer：`AZeroReceipt.confirmed_a` 仍取请求目标而非 readback，和宽 tolerance 共同削弱清零证据；这是既有 SafeStop/flow receipt model，已与 BH6 合并记录 deferred work，本轮不改 Global Stop/flow model。
- Blind BH12 / medium / patch：无效 Alicat Unit ID 原延迟到 acquisition 后才失败；RealHAL 构造现被动校验 mapping value，新增无硬件 I/O 回归。
- Blind BH13 / medium / patch：deadline、分片/非有限、validator 锁域、pending stale、probe wiring、close/desync 等覆盖不足属实；已补成 stateful FakeSerial/CLI 回归。
- Edge EH1 / high / patch：同 BH3，非有限 timeout 已拒绝。
- Edge EH2 / high / patch：同 BH2，transport timeout 与 monotonic elapsed 共同执行 frame deadline。
- Edge EH3 / high / patch：非有限 setpoint tolerance 可接受任意 frame；现于 TX 前拒绝并有零 TX 回归。
- Edge EH4 / high / patch：pyserial `write` 必须返回完整 byte count；`None`/short write 现锁存 transport failure，测试 fake 也遵守真实返回 contract。
- Edge EH5 / maybe-false / defer：协议无 sequence ID，若 stale 超过 provisional bounded quiet 后才到达则无法判别；当前无真实 maximum latency 可证伪，已明确必须由下一轮只读 Poll/VE/LSS latency 决定边界并记录 deferred work。
- Edge EH6 / high / patch：缺失或负 `in_waiting` 原会被当作空队列；现均作为 sync/transport failure 锁存，负值已有回归，产品 pyserial 明确提供该属性。
- Edge EH7 / false / rejected：同 BH9；close failure 保留 latch 是安全阻断，不允许在未确认 release 时恢复。
- Edge EH8 / medium / patch：同 BH7，已禁用隐式 relative tolerance。
- Edge EH9 / high / patch：同 BH8，所有普通 validator exception 均 fail closed 并锁存。
- Gap VG1 / medium / patch：原并发测试只阻塞 RX，不能证明 validation 仍在锁内；新增 blocking validator 测试，第二 TX 直到 validator 完成才发生。
- Gap VG2 / high / patch：缺少“健康 transaction 后注入 pending stale”回归；新增测试证明第二命令零 TX并锁存。
- Gap VG3 / medium / patch：新增 truncated、gasless、NaN、Inf、-Inf 参数化回归，均 mismatch、latch、阻断后续 TX。
- Gap VG4 / medium / patch：新增 shutdown budget 可容纳而 Connect budget 不可容纳的独立 rejection 测试。
- Gap VG5 / medium / patch：新增 `probe_alicat.main` CLI wiring 测试，证明默认只 poll，显式 `--set` 才按 poll/set-response/verified-poll 顺序执行并使用量化 wire target。
- Final diff F1 / medium / patch：上层 deadline 原先先经 `max()` 再判有限性，可能把 `NaN/-Inf` 折叠为最小值；现改为原始毫秒值必须有限且为正后再换算，并补参数化回归。

## Verification

**Commands:**
- `python -m ruff check .`
- serial/RealHAL/FlowWorker/startup/Global Stop 定向 pytest；`python -m pytest`
- `git diff --check`
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 build`
- offscreen MockHAL simulation；不得运行 real/HIL/COM6/NI。

**Current results:** Alicat transaction/probe 定向套件 50 passed，覆盖 RealHAL、FlowService/FlowWorker startup/Global Stop 调用路径；`python -m ruff check .` 通过；`python -m pytest` 为 1391 passed、1 skipped、0 failures；`git diff --check` 通过；PyInstaller build 成功，生成 102.23 MB artifact；offscreen MockHAL smoke 通过 exactly-once auto-connect、CONNECTED 与 clean teardown。所有结果均为 Fake/Mock，不构成实机通过。
