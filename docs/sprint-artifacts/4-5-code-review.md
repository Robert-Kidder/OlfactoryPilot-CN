# Story 4.5 Code Review Findings

审查日期：2026-08-17
审查范围：Story 4.5「全局停止顺序与三通阀模型」本次修改及其实际依赖路径。Story 4.1 的既有未提交修改仅在 Story 4.5 调用或依赖时纳入。
原始结论：**阻断**。共发现 **10 个 High、5 个 Medium**，均为 `patch`；无 `decision-needed`、无 `defer`。

软件证据复核结果：Story 4.5 定向测试 `257 passed`，全量测试 `734 passed`，Ruff 通过，`git diff --check` 通过（仅换行符警告）。这些绿灯没有覆盖或拦截下列安全缺口。未操作硬件，未执行 HIL。Edge Case Hunter 返回的是结构化 Markdown 而非预期 JSON；已按 location、trigger、consequence 完整归一化，未丢失证据。

## 整改结果（2026-08-17）

整改提交声明原始 F-01–F-15 及第二轮 adversarial 新增项均已处理，并将状态转为“等待独立复审与另行授权 HIL”。下方“整改后独立复审”是对此声明的最终复核结果，并覆盖本段的阶段性判断。

| Finding | 整改证据 |
|---|---|
| F-01 / F-04 / F-14 | HAL 的 odor 集合排除 selector；旧 21-target HIL 入口退役；Worker 与 DO adapter 仅接受匹配配置目标和专用身份的 selector 命令，safe-high 可执行并可记录。 |
| F-02 / F-03 / F-05 | protocol stop、异常停止、selector 缺失均先 fence 和 A=0；随后才允许 selector，并继续跨 variant odor 与 final A/B/C 收敛；缺失 selector 或 handoff 证据保持 recovery。 |
| F-06 / F-10 | shutdown 在 maintenance fence、DO handoff 与精确 lease release 完成后才允许计划完成；Controller 不再补造 owner handoff。 |
| F-07 / F-08 | cleaning 按完整 Flow/DO command identity 校验；失败、迟到、冲突和 selector 不确定均执行 odor best-effort 后结束于 recovery，不再无限重试。 |
| F-09 / F-11 | Worker 对相同 canonical identity 的任何重复 receipt 锁定 recovery；selector 与同步/异步 flow receipt 均在消费时复核单调 deadline。 |
| F-12 | 非线程 FlowWorker shutdown 也释放并验证串口资源。 |
| F-13 | odor ID 限制为 1–20，并使用规范化物理 line 检测 selector alias 冲突；关闭集合覆盖全部 variant。 |
| F-15 | 新增伪造 selector、错误目标、字符串极性、重复 receipt、阻塞超时、完整 flow identity、queued cleaning 抢占、错误 handoff identity 与 session receipt 回归。 |

整改后软件证据：Story 4.5 扩大定向集 `408 passed in 11.28s`；完整 pytest `755 passed in 19.37s`；Ruff `All checks passed!`；`git diff --check` 无 whitespace error（仅 LF→CRLF 提示）。未连接或操作真实硬件，未运行 HIL。

## High

### F-01 — 旧 HIL 路径仍在 A 清零前把 selector 当作“第 21 个全关目标”执行

- 位置：`scripts/hil_single_line_mapping.py:89-94,192-214`；`app/services/real_hal.py:514-522,743-763`
- 触发：操作者执行仍可运行的 `hil_single_line_mapping.py`。
- 证据：`_collect_valve_lines()` 把 selector 加入 `_digital_lines`，`RealHAL.close_all()` 对集合内每条线直接写低；脚本在 A/B/C 清零前先调用 `hal.close_all()`，并明确标记为 `initial 21-target close`。`finally` 中即使 A=0 失败也继续 `close_all()`，随后仍可能输出 `FINAL_SAFE`。
- 影响：`Dev2/P1.0` 可在没有匹配 A=0 receipt、epoch、lease 或 `SafeStopPlan` 授权时被切换，直接违反 `A=0 receipt → selector safety route` 的严格偏序，并构成跨 Worker/HAL 旁路。

### F-02 — 协议 stop/session stop 仍走“先关气味阀”的旧转换，没有进入统一 SafeStopPlan

- 位置：`app/workers/actuation_worker.py:2513-2573,2602-2615`；`app/controllers/main_controller.py:2944-2949,2970-2977,3014-3025`
- 触发：协议停止、会话结束、reset 或 global stop 先处理 `post_stop`，且 A 仍非零、selector 仍在 odor route。
- 证据：`_begin_safe_transition()` 失效 epoch 后立即调用 `_submit_all_configured_closes()`，随后直接 `ProtocolExecutor.stop()`；该路径不请求 A=0、不等待 A receipt、不切换 selector，也没有 `SafeStopPlan` 终态。Controller 的 stop/reset 又同时调用此旧路径和 `ShutdownService`，形成两套互相交错的停止语义。
- 影响：stop、异常停止与 shutdown 并未共用一致的安全语义；普通 stop 可以在 A 非零时先执行气味阀写入并结束协议状态。

### F-03 — selector 缺失/无效时，shutdown 跳过 A-zero 并恢复成危险顺序

- 位置：`app/services/shutdown_service.py:212-272`；`tests/test_shutdown_actuation.py:281-298`
- 触发：selector 缺失、配置无效，或因映射冲突而被禁用。
- 证据：`plan` 不建立时，`zero_a_for_safe_stop()` 整段被跳过；随后先执行 `close_odors_for_safe_stop()`，再执行 `zero_all_for_safe_stop()`。现有测试还明确断言 selector 缺失时不调用 A-zero。
- 影响：虽然事件最终标为 recovery，但硬件动作本身已经违反严格偏序。fail-closed 不应只体现在状态字段；在 selector 不可用时仍应先请求并验证 A=0，再 best-effort 关闭 odor，且不得报告安全终态。

### F-04 — 通用 emergency API 可用 `valve=0` 绕过 A=0 门禁直接写 selector

- 位置：`app/workers/actuation_worker.py:892-946`；`app/services/valve_service.py:116-121`；`app/services/actuation_do_adapter.py:44-53`
- 触发：任何 Owner 内调用者执行 `submit_emergency_close(0, reason=...)`，或通过通用 SAFETY 命令提交 `valve=0`。
- 证据：公开 API 未拒绝 0；`resolve_target(0)` 把它解析为 selector target；DO adapter 随后直接写低，不检查 operation/generation/epoch、A-zero receipt 或 `SafeStopPlan.selector_allowed`。
- 影响：selector 并非“仅经专用 API”可达，仍存在可执行旁路，可在 A 非零时提前切换路线。

### F-05 — 异常停止只确认 A，关闭范围仅为 active variant，却硬编码 handoff 并报告完成

- 位置：`app/workers/actuation_worker.py:948-1001,1163-1223,1241-1318`；`app/services/valve_service.py:123-155`；`tests/test_actuation_worker.py:1528-1578`
- 触发：LOW_FLOW、disconnect、recorder failure 等调用 `invalidate_execution()` 的异常路径。
- 证据：异常计划只提交 `safe_stop_a_zero`，没有 B/C zero receipt；关闭集合使用 `emergency_close_steps()`，它只返回 active variant，而不是 `all_configured_close_steps()` 的跨 variant 并集；最后以常量 `owners_handed_off=True` 调用 `plan.complete()`，没有 DO/serial/lease/handoff 证据。现有测试只期望一次 A-zero，随后直接断言 `safe_terminal`。
- 影响：B/C、非 active variant 输出或 owner/lease 仍可能未收敛，系统却写出“异常停止已按 A=0 → selector 安全路线 → 气味阀关闭完成”。这是错误提前报告“已安全停止”。

### F-06 — shutdown 在 AI/serial/lease handoff 之前完成计划，成功证据与真实资源状态可矛盾

- 位置：`app/services/shutdown_service.py:275-323`；`app/workers/flow_worker.py:167-191,360-423,450-483`；`app/workers/actuation_worker.py:1548-1578`
- 触发：活动 protocol lease、maintenance lease 或 cleaning 存在，或 AI/serial 释放失败。
- 证据：`plan.complete()` 只接收 Actuation/DO handoff，发生在 AI release 与 FlowWorker shutdown 之前；后续失败不会把 plan 降级到 `RECOVERY_REQUIRED`。Flow safe-stop 推进 execution epoch 却保留旧 lease/token，`shutdown()` 也不释放 lease，而 `prepare_restart()` 明确拒绝非 `IDLE` lease。全局 fence 不终结 active cleaning 或交还 maintenance recorder/lease。
- 影响：事件可同时出现 `safe_stop_status=completed`、`recovery_required=false` 与 `result=unsafe`；也可能以成功 shutdown 收尾却无法 restart，违反 Worker/HAL 单 Owner、lease、epoch、receipt 和 handoff 的统一证据语义。

### F-07 — cleaning 的 A-zero receipt 只按 operation/command ID 关联，冲突 identity 可授权 selector

- 位置：`app/workers/actuation_worker.py:2940-3003,3052-3054`
- 触发：收到与 pending command ID 相同、但 generation、execution_epoch、lease_token、source 或 mode 冲突的 Flow receipt。
- 证据：`_is_cleaning_flow_result()` 只比较 operation ID，`_consume_cleaning_flow_result()` 再只比较 command ID；它没有把 receipt 的完整 identity 与已提交命令比较。只要结果成功且数值为零，就推进到 `selector_safe`。审查诊断用相同 command ID、不同 generation 的 receipt 复现了 selector safety command 被提交并最终 `COMPLETED`。
- 影响：stale/late/conflicting receipt 未 fail-closed，旧 epoch 或错误 lease 的回执可以错误授权 selector。

### F-08 — cleaning 的 A-zero failure/timeout/late 分支没有执行 odor best-effort close

- 位置：`app/workers/actuation_worker.py:2947-3013`；`tests/test_cleaning_state_machine.py:424-492,721-743`
- 触发：A-zero receipt 失败、超时、stale，或 command ID 不匹配/迟到。
- 证据：这些分支直接调用 `_finish_cleaning_recovery()`，没有调用 odor close 提交逻辑。现有失败与迟到测试甚至明确断言 `calls[before:] == []`；timeout 测试只检查 selector 没有被切换。
- 影响：selector 虽保持不动，但可能仍打开的气味阀没有 best-effort 收敛，违反 Story I/O matrix 对失败分支的要求，且 owner handoff 可能永久无法满足。

### F-09 — background/global safety receipt 的冲突重复被按 command ID 静默吞掉

- 位置：`app/workers/actuation_worker.py:1034-1052,1163-1187,4205-4211`
- 触发：同一 safety `command_id` 先收到成功 receipt，随后收到字段冲突、stale 或失败的重复 receipt。
- 证据：`_seen_receipts` 只保存 command ID；命中后直接返回，不比较之前的完整 receipt，也不会把第二份 receipt 交给 `SafeStopPlan.accept_selector()`。纯模型虽能拒绝冲突重复，实际 Worker 集成路径绕过了该保护。
- 影响：冲突硬件证据不会锁定 recovery，已经完成的计划仍保留安全终态，违反 conflicting receipt 必须 fail-closed 的要求。

### F-10 — cleaning 在 maintenance lease/fence 完成前向 UI 宣称 owner 已交接，并可用伪造的 zero evidence 释放 lease

- 位置：`app/workers/actuation_worker.py:383-395`；`app/controllers/main_controller.py:1548-1565,1569-1638`
- 触发：Worker 先发布 `COMPLETED`，随后 maintenance lease release 或 producer fence 失败；或流程以 `RECOVERY_REQUIRED` 结束但 pending/open 集合为空。
- 证据：UI 在收到 snapshot 时立即显示“owner 交接均已确认”，真正的 lease release 直到结果处理的后半段才执行；失败时 runtime 仍可能保持 `COMPLETED`。`cleaning_owner_handoff_ready` 对 `RECOVERY_REQUIRED` 也可返回 true，且不检查 A-zero/selector 证据；Controller 随后无条件写入 `flow_zero_confirmed=True` 并尝试释放 maintenance lease。
- 影响：系统可提前报告“已安全停止”，或在缺少真实 flow/selector receipt 时释放门禁，使迟到旧命令与后续命令重新交错。

## Medium

### F-11 — selector receipt timeout 与阻塞写入共用事件循环，迟到成功可逃过 timeout

- 位置：`app/workers/actuation_worker.py:1365-1406,3834-3848`
- 触发：DO writer 阻塞时间超过 safe-stop deadline 后才返回成功。
- 证据：写入和 deadline 消费都在同一 ActuationWorker 事件循环；阻塞写入返回后先消费成功 receipt，将状态推进出 `selector_pending`，随后已到期 timeout 因状态不匹配被忽略。
- 影响：deadline 对这种 late receipt 没有约束力，超时写入仍可被接受为成功，而不是稳定锁定 `RECOVERY_REQUIRED`。

### F-12 — 未运行 QThread 时 FlowWorker 可持有串口，但 shutdown 直接返回 handoff 成功

- 位置：`app/workers/flow_worker.py:416-448,450-461`
- 触发：safe-stop 在 FlowWorker QThread 未运行时同步 `process_ready()`，Flow service 因而打开/使用真实 serial 资源。
- 证据：serial release 只在 `run()` 的 `finally`；`shutdown()` 对 `isRunning()==False` 直接返回 true，不调用 release，也不验证 owner。
- 影响：ShutdownService 可记录 serial owner 已交还，实际串口仍由同步调用线程持有，形成错误 handoff 证据。

### F-13 — 配置入口未约束 odor ID 为 1–20，也未按物理 line 规范化检测 selector 冲突

- 位置：`app/models/app_state.py:103-124`；`app/services/real_hal.py:773-782`；`app/services/valve_service.py:123-155`
- 触发：local/legacy variant 使用通道 0、21 或其他越界键，或用 `Dev2/P1.0` 与 `Dev2/port1/line0` 两种别名指向同一物理线。
- 证据：任何可转换为 int 的键都会被接受；冲突检查只做原始字符串相等，而 HAL 后续才把 `P1.0` 规范化为 `port1/line0`。这些映射会进入 odor close 集合。
- 影响：配置可以重新引入“第 21 只普通阀”或让 selector 物理线同时成为 odor target，从普通关闭路径提前写入。

### F-14 — 声称支持的反向 selector 极性在执行与记录层不可用

- 位置：`app/models/safe_stop.py:14-35`；`app/workers/actuation_worker.py:1650-1693`；`app/services/actuation_do_adapter.py:31-43`；`app/services/session_file_service.py:211-236`
- 触发：合法配置 `safe_level=true, odor_level=false`。
- 证据：模型接受该显式极性，Worker 会生成 `SAFETY/OPEN`，但 DO adapter 无条件拒绝所有 SAFETY/OPEN；session validator 也只把 `safety/close` 视为合法 selector safety receipt。
- 影响：受支持配置无法完成安全停止，执行、route 解释和持久化契约不一致；当前默认低电平生产配置不受此项影响。

### F-15 — 自动化测试通过，但关键顺序、owner 和失败分支没有集成覆盖

- 位置：`tests/test_shutdown_actuation.py:125-205,281-298`；`tests/test_actuation_worker.py:1528-1578`；`tests/test_cleaning_state_machine.py:424-492,721-743`；`tests/test_safe_stop.py:87-98`
- 缺口：没有覆盖 Controller `post_stop` 与 ShutdownService 的真实交错、active protocol/maintenance lease 下的 stop/reset/shutdown、真实 serial/AI handoff 失败后的 `safe_stop_status`、`submit_emergency_close(0)`、同 command ID 不同 identity 的 flow receipt、Worker 集成层冲突重复 receipt、阻塞 selector writer 的 late receipt、反向极性、物理 line alias 冲突及越界 0/21 mapping。
- 现状：部分测试把缺陷固化为预期——异常停止只确认 A 即 `safe_terminal`、selector 缺失不请求 A-zero、cleaning A-zero 失败后不提交 odor close。因此 `257/734 passed` 不能作为关键安全顺序与失败分支已覆盖的证据。

## 整改后独立复审（2026-08-17）

结论：**未通过，Story 4.5 不得标记为 `done`。** 经 Blind Hunter、Edge Case Hunter、Acceptance Auditor 三层独立检查及主审逐项源码核验，确认 **9 个 High、1 个 Medium**，均为可直接整改的 `patch`；无 `decision-needed`、无 `defer`、无 dismiss。原始 F-01–F-15 中多数基础缺口已关闭，但 F-01/F-02/F-04/F-06/F-07/F-09/F-10/F-14/F-15 仍被以下可达路径部分重开。

软件门禁仍为绿色：Story 4.5 定向集 `408 passed in 11.44s`，完整 pytest `755 passed in 22.17s`，Ruff `All checks passed!`，`git diff --check` 无 whitespace error（仅 LF→CRLF 提示）。这些测试未覆盖下列路径。复审未连接或操作真实硬件，未运行 HIL。

### R-01 — High — 通用业务命令仍可绕过 A=0 门禁直接写 selector

- 位置：`app/workers/actuation_worker.py:682-749`；`app/services/actuation_do_adapter.py:47-67`
- 触发：调用公共 `ActuationWorker.submit()`，提交目标匹配 selector 的 `MASTER`、`WARMUP`、`MANUAL`、`PRETEST` 或 `CLEANING` 类 `valve=0` 命令；例如默认极性下的 `MASTER/CLOSE`。
- 证据：Worker 只封堵未授权的 `SAFETY/valve=0`；adapter 把上述 category 直接归为 `selector_business_route`，不要求 `SafeStopPlan` 或匹配 A=0 receipt。MockHAL 复现得到 `submit=True` 且写入 `Dev2/P1.0=False`。
- 影响：A 非零时仍存在可执行 selector 路线切换，违反 selector 唯一专用入口与严格偏序。

### R-02 — High — safety receipt 未按完整 command identity 校验

- 位置：`app/workers/actuation_worker.py:4365-4391,4707-4726`
- 触发：DO writer 返回 command ID、operation/generation 等主要字段相同，但 `arm_epoch`、`expected_ns`、`safety_generation` 或 `action_kind` 被改变的 safety/selector receipt。
- 证据：执行路径使用手写的缩减 tuple 比较，遗漏上述字段；虽然 `_receipt_matches_command()` 已定义完整比较，但这里只用于 cleaning，普通/background safety receipt 不调用它。
- 影响：被 fence 失效或身份损坏的 receipt 仍可推进 selector/safe-stop 证据并形成错误安全终态。

### R-03 — High — 重复 stop 可在 lease handoff 前提前进入 `STOPPED`

- 位置：`app/workers/actuation_worker.py:2883-2896,2982-2996`
- 触发：第一次 protocol stop 已完成气路动作并发出 handoff 请求、Controller 尚未确认时，第二次 stop 已在 Worker 队列中等待。
- 证据：第二次 stop 调用 `_maybe_finalize_safe_transition()`；当 `_safe_transition_handoff_identity` 非空时，保护条件反而不返回，随后可直接 `_finalize_safe_transition()`，未调用 `plan.complete(owners_handed_off=True)`。
- 影响：状态可提前报告 `STOPPED`，而精确 lease handoff 尚未完成，直接命中“不得错误提前报告已安全停止”的阻断条件。

### R-04 — High — global shutdown 抢占 cleaning 时可伪造完整 handoff，并遗留旧 lease/token

- 位置：`app/workers/actuation_worker.py:489-507,1859-1924,2224-2248`；`app/controllers/main_controller.py:1573-1644`；`app/services/shutdown_service.py:295-370`
- 触发：queued 或 active cleaning 已建立 maintenance recorder/lease 后执行 stop、reset 或 shutdown。
- 证据：Worker 抢占后发布 `RECOVERY_REQUIRED`，但 Controller 因 `cleaning_owner_handoff_ready=False` 不提交 controller/flow fences；`handoff_maintenance_for_safe_stop()` 只提交 actuation fence仍可返回 true，ShutdownService 随即把它当作完整 handoff、释放 lease并允许 `plan.complete()`。重启准备也不清理 Controller/Worker 的旧 cleaning token/plan。
- 影响：shutdown 可报告 success，而 maintenance bundle 未完成、producer 未全部 fence；重连后的恢复命令还可能携带已释放 token 而被持续拒绝。

### R-05 — High — 活动协议全局停止后残留幽灵 protocol lease

- 位置：`app/controllers/main_controller.py:3221-3234,3873-3889`；`app/services/shutdown_service.py:315-330`
- 触发：协议持有 lease 时执行成功的全局 stop/reset，再重新连接或发布 interlock。
- 证据：ShutdownService 已令 FlowWorker/shared lease 回到 `IDLE`，但 Controller 不清理 `_protocol_lease_epoch`；`_publish_interlock_from_state()` 因该旧值继续发布 `device_lease=protocol`。MockHAL 复现中 shutdown event 为 success、FlowWorker 为 IDLE，但 Controller token 仍为 `2`，重新发布后仍是 protocol。
- 影响：重连后的 startup zero、手动/预检和普通 flow 路径被幽灵 lease 拒绝，成功 reset 后设备仍不可正常使用。

### R-06 — High — 无 lease 的 protocol stop 会永久毒化 FlowWorker admission

- 位置：`app/workers/flow_worker.py:109-116,141-145`；`app/controllers/main_controller.py:3567-3586`
- 触发：协议处于 READY、尚未取得 protocol lease 时点击 stop。
- 证据：safe-stop flow 写入 `_safe_stop_identity`；Controller 在 `_protocol_lease_epoch is None` 且 lease 为 IDLE 时直接把 handoff 判为成功，却没有清理 FlowWorker identity。MockHAL 复现随后 `acquire_protocol_lease(...) == False`。
- 影响：界面可显示已停止，但后续协议在同一进程内无法再次取得 lease，只能重启 owner/程序恢复。

### R-07 — Medium — cleaning 静默接受完全相同的重复 receipt

- 位置：`app/workers/actuation_worker.py:1116-1131,3483-3497`
- 触发：同一 cleaning command 的同一 receipt 被重复投递或 replay。
- 证据：cleaning 在全局 `_seen_receipts` 防重前被分流；`_consume_cleaning_receipt()` 只在内容不同时 fail closed，完全相同则直接返回。纯 `SafeStopPlan` 已拒绝 identical duplicate，但集成路径不一致。
- 影响：重复硬件证据未按整改契约锁定 `RECOVERY_REQUIRED`，也缺少对应集成回归。

### R-08 — High — cleaning `flow_start` 身份冲突后可能保留非零 A/B/C

- 位置：`app/workers/actuation_worker.py:3395-3411,3865-3901`
- 触发：已提交非零 cleaning setpoint 后，收到同 operation 但完整 `FlowCommand` identity 冲突的 result，随后原 result 迟到。
- 证据：冲突分支清除 pending flow 并调用 `_recover_cleaning_without_selector()`；该函数只提交跨 variant odor close，不提交 A/B/C zero。迟到的原结果再次因无 pending command 被拒绝。
- 影响：终态虽为 `RECOVERY_REQUIRED`，已接受的非零流量 setpoint 可继续存在，fail-closed 收敛不完整。

### R-09 — High — 反向 selector 极性与 session receipt 契约仍不一致

- 位置：`app/services/valve_service.py:179-217`；`app/controllers/main_controller.py:2786-2806`；`app/services/session_file_service.py:227-239`
- 触发：合法配置 `safe_level=true, odor_level=false`，并在 recording session 中开始协议。
- 证据：协议 master prepare 会把 odor route 生成 `WARMUP/CLOSE`；validator 对 WARMUP 只允许 `action=open`，因此硬件写入成功后 receipt 持久化仍被拒绝。
- 影响：整改声称支持的反向极性在真实录制流程不可用，并会触发记录失败/恢复。

### R-10 — High — 可执行 HIL benchmark 未适配统一 safe-stop/handoff

- 位置：`scripts/hil_actuation_benchmark.py:1140-1167,1373-1403`
- 触发：LOW_FLOW recovery，或 benchmark 在最终 `shutdown_via_service()` 前异常/人工中止。
- 证据：LOW_FLOW 路径先等待 Worker 进入 `STOPPED`，之后才释放 protocol lease，但 Story 4.5 的 Worker 必须先收到 handoff 确认才进入 `STOPPED`，形成循环等待；脚本也未连接 `protocol_safe_stop_handoff_requested`。异常 teardown 仍只调用 odor-only `emergency_close_all()`，之后直接停止 Flow owner，不确认 A=0、selector safe 或 final A/B/C zero。
- 影响：恢复路径超时，并可能在非零 A 与 selector odor route 下结束可执行硬件脚本。该脚本虽未在本次执行，也未被 Story 4.5 直接编辑，但它调用了本 Story 已改变的公共 Worker 语义，因此属于实际依赖回归。

### 复审后的 F-01–F-15 状态

| 原 Finding | 复审结论 |
|---|---|
| F-01 / F-02 | 部分重开：`hil_single_line_mapping.py` 已退役，但 benchmark 异常 teardown/LOW_FLOW handoff 未适配；重复 stop 仍可提前 `STOPPED`。 |
| F-03 / F-05 / F-08 / F-11 / F-12 / F-13 | 已关闭。 |
| F-04 | 重开：通用业务 category 的 selector 公共提交旁路仍可执行。 |
| F-06 / F-10 | 重开：global shutdown 对 cleaning handoff 取证不完整，并有 protocol/cleaning lease 状态残留。 |
| F-07 | 部分重开：full identity 能识别冲突，但 `flow_start` 冲突后的 A/B/C 收敛不完整。 |
| F-09 | 重开：cleaning identical duplicate 仍被静默接受。 |
| F-14 | 重开：safe-high 的安全动作可执行，但 recording 中的 odor-route `WARMUP/CLOSE` receipt 被拒绝。 |
| F-15 | 重开：R-01–R-10 的关键集成与失败分支均无阻断回归。 |

复审状态已同步为 `in-progress`。本轮只修改审查/状态文档，没有修改程序代码，没有操作硬件。

## R-01–R-10 整改完成（2026-08-17）

用户选择“Apply every patch”后，R-01–R-10 已全部实现并补充阻断回归。本节覆盖上一节的阶段性 `in-progress` 状态；Story 4.5 软件范围现为 **done**。

| Finding | 整改结果 | 回归证据 |
|---|---|---|
| R-01 | Worker 只接受内部 valve plan/cleaning plan 生成的 selector odor-route 命令；adapter 同时校验目标、极性、operation/generation、step 与 action identity，业务 safe-route 写入被拒绝。 | `test_selector_business_route_cannot_select_safe_level`；`test_public_submit_rejects_business_command_that_selects_safe_route` |
| R-02 | `ActuationReceipt` 增加并持久化 `safety_generation`；Worker 统一比较 command/receipt 的 epoch、arm、sequence、trial、action、target、operation、generation、step、action kind 与 safety generation。 | `test_selector_receipt_rejects_mutated_arm_and_action_identity` |
| R-03 | pending protocol stop 只在 background `SafeStopPlan.safe_terminal` 成立后才可 finalize；重复 stop 不再绕过 handoff。 | `test_protocol_stop_uses_a_zero_before_selector_then_odors_and_final_zero` 的 handoff 等待期重复 stop 断言 |
| R-04 | ShutdownService 使用 Controller maintenance handoff：actuation/controller/flow 三方 fence 后同步终结 writer；成功后再释放精确 lease，并清理 Controller/Worker cleaning token/plan。failed/recovery maintenance 仅保留 terminal staging，不伪装 complete bundle。 | `test_global_stop_fences_and_finalizes_active_cleaning_bundle` |
| R-05 | shutdown event 根据 FlowWorker 实际 IDLE lease 清理 `_protocol_lease_epoch`、pending-start 与 interlock ledger。 | `test_global_stop_clears_controller_protocol_lease_bookkeeping` |
| R-06 | 无 protocol lease 的 handoff 改走 `release_lease_for_safe_stop(identity)`；IDLE release 同时清理 `_safe_stop_identity/_safe_stop_lease_token`。 | `test_stop_before_protocol_lease_clears_flow_safe_stop_identity` |
| R-07 | cleaning identical/conflicting duplicate receipt 均立即进入 fail-closed stop/recovery。 | `test_identical_cleaning_receipt_replay_requires_recovery` |
| R-08 | `flow_start` identity 冲突改为先提交 A/B/C zero，再进入 recovery；A-zero identity 冲突仍禁止 selector 并关闭 odor。 | `test_conflicting_flow_start_receipt_submits_zero_before_recovery` |
| R-09 | session receipt contract 接受合法反向极性的 selector odor-route `WARMUP/CLOSE`，同时保留 target/category 门禁。 | `test_reverse_polarity_warmup_close_receipt_round_trip_is_valid` |
| R-10 | HIL benchmark 接入 `protocol_safe_stop_handoff_requested`，先精确释放 lease 再确认 `STOPPED`；异常/人工 teardown 改走统一 ShutdownService，不再使用 odor-only legacy close 作为最终安全证据。 | `test_hil_safe_stop_releases_protocol_lease_before_handoff_confirmation`；`test_confirmed_abort_close_still_uses_unified_shutdown_barrier` |

最终软件证据：

- Story 4.5 定向测试：`415 passed in 11.39s`。
- 完整 pytest：`762 passed in 19.59s`。
- Ruff：`All checks passed!`。
- `git diff --check`：无 whitespace error，仅工作副本 LF→CRLF 提示。
- MockHAL 复现确认 active-protocol global stop 后 Controller/Flow/interlock 均为 IDLE；READY 状态 stop 后可重新获取 protocol lease。
- 未连接或操作真实硬件，未运行任何 HIL。

最终分诊：`0 decision-needed`、`0 unresolved patch`、`0 defer`、`0 dismissed`。Story/spec、整改 spec 与 `sprint-status.yaml` 已同步为 `done`；真实硬件验证仍须另行授权，不在本次软件完成范围内。
