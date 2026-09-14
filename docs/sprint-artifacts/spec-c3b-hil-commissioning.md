---
title: 'C.3b 实机 HIL commissioning'
type: 'chore'
created: '2026-09-10'
status: 'in-review'
route: 'dispatch'
review_loop_iteration: 1
offline_lifecycle_remediation: 'done'
offline_connection_presentation: 'done'
offline_do_lifecycle_remediation: 'done'
offline_serial_transaction_remediation: 'done'
physical_commissioning_status: 'blocked-pending-alicat-readonly-preflight-retest'
baseline_commit: '2cc3a8986eaa95aeb7a963ee05f3caea484deb1f'
context:
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/architecture.md'
  - '{project-root}/docs/sprint-artifacts/evidence/c-3b-hil-commissioning-checklist.md'
  - '{project-root}/docs/sprint-artifacts/evidence/c-3b-2a-alicat-read-only-poll-2026-09-10.md'
  - '{project-root}/docs/sprint-artifacts/evidence/c-3b-alicat-ve-lss-read-only-2026-09-10.md'
  - '{project-root}/docs/sprint-artifacts/evidence/c-3b-alicat-safe-state-normalization-2026-09-10.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** C.3a 只证明了 fake-real 软件契约，当前八个启用气口仍只有模拟验证状态。真实应用启动/连接还会启动 NI DO task，并在自检通过后写 A/B/C=0，不能视为纯只读连接。

**Approach:** C.3b-2A/2C 已完成串口确认、逐台清零和 LSS `U/U/U`。C.3b-3 真机启动前发现 lifecycle blocker，因此先离线整改：构造与显示保持零硬件 I/O，窗口显示且事件循环可处理 queued event 后自动发起一次权威安全连接 transaction；失败后只允许人工重试。整改完成前及本轮均不执行真实 App Connect。

## Boundaries & Constraints

**Always:** App 构造、Controller/View 创建、主窗口显示以及 queued auto-connect callback 执行前硬件 I/O 均为 0。每个进程只调度并消费一次 startup auto-connect；startup、Retry 与运行中断线后的人工重新连接共用同一 transaction。连接顺序为安全 DO 接管、自检、B/C/A 清零及新鲜回读，全部成功后才发布 connected。失败必须安全收口且不自动重试。

**Never:** 本轮不得打开 COM6、创建真实 NI task、访问 Alicat、运行 HIL script 或 RealHAL smoke。不得增加 auto-connect 设置、配置、环境变量或 CLI 开关，不得维护 auto/manual 两套连接逻辑，不得自动循环重试、自动恢复断线前动作，且不得改变 ActuationWorker/FlowWorker 单一硬件 owner、回执、SafeStop 与 HardwareProfile 契约。

## I/O & Edge-Case Matrix

| 场景 | 输入 / 状态 | 预期行为 | 异常处理 |
|---|---|---|---|
| 现场前核对 | NI、COM、Alicat 与配置一致 | 形成单口授权，仍不动作 | 任一不一致即停止 |
| C.3b-2A | `COM6 @ 19200`，只轮询 `a,b,c` | 原样保存返回并立即释放串口 | 非零状态只报告，不发送清零命令 |
| VE/LSS 只读查询 | 先查固件；仅 `>=10v05` 查询无参数 LSS | 保存完整原始响应并关闭 COM6 | 不带 mode，不修改 Setpoint Source |
| C.3b-2C | A→B→C 逐台清零，再逐台 S→U | 每次写后立即 query/poll；最终 U/U/U、0/0/0 | 任一门禁失败立即停止，不重试、不继续下一台 |
| 首次验证 | 气口2、1500 ml/min、最长30秒已批准 | 按冻结顺序启动、收口并人工确认 | 失败/超时/出口不符立即停止，不继续下一口 |
| 全局停止 | 活动或状态不确定 | A=0→selector补偿→阀1–20关闭→A/B/C=0→释放资源 | 证据不完整则进入人工恢复 |
| 正常启动 | 窗口首次显示且事件循环开始 | queued auto-connect 恰好请求一次同一连接 transaction | 连接中拒绝重复请求 |
| 窗口立即关闭 | auto-connect 已排队但 callback 未执行 | callback 安全 no-op，硬件 I/O=0 | 不启动后台 owner |
| startup 失败 | 任一连接阶段失败 | fail-closed、释放已接管资源并显示“设备未连接 / 重新连接” | 不调度自动重试 |
| Global Stop 成功 | 已连接且安全收口、资源释放完成 | 显示“设备未连接 / 重新连接” | 不自动重连；重连不恢复旧动作 |
| 运行中断线 | 已连接后通信中断 | 先安全收口，再显示“设备未连接 / 重新连接” | 不自动重连或恢复实验 |
| 安全收口无法确认 | 阀门关闭、流量归零或 SafeStop 无法确认 | badge 仍为“设备未连接”，另持续提示用户立即断电 | 保持 fail-closed，详细原因只进日志/evidence |
| DO 首次接管 | active-high/active-low odor 与 selector 极性 | 每个 task 的第一次物理 drive 是完整安全 packed image | 无法计算或写入安全 image 即拒绝连接 |

</frozen-after-approval>

## Code Map

- `app/main.py` — 合并默认与本机配置；`hal_mode=real` 且无 `--simulation` 时创建 RealHAL并启动 workers。
- `app/controllers/main_controller.py` — Connect、自检后 A/B/C 清零、physical verification 与全局停止入口。
- `app/workers/hardware_worker.py`、`app/services/hardware_check_service.py` — 启动即枚举 Dev1/Dev2，并打开/关闭 COM；不验证 Unit ID。
- `app/services/real_hal.py` — 惰性串口和按 port 分组的 NI DO tasks。
- `app/services/shutdown_service.py`、`app/workers/{actuation_worker,flow_worker}.py` — 停止偏序、回执等待、失败恢复。
- `app/services/hardware_profile_store.py` — 当前 profile 是 authority；last-known-good 仅供显式回滚。
- `scripts/hil_*.py`、`scripts/probe_alicat.py` — 历史 Story 工具；含真实访问/写入的模式不得直接用于本轮。
- `app/views/main_window.py` — 稳定的连接状态区、失败后的人工 Retry，以及 close-before-callback 门禁。
- `tests/test_startup_connection.py`、`tests/test_do_lifecycle.py` — one-shot lifecycle、统一 transaction、无自动重试及 NI 首次安全 image 的离线回归。

## Tasks & Acceptance

**Execution:**
- [x] `docs/sprint-artifacts/evidence/c-3b-hil-commissioning-checklist.md` — 已记录现场身份、安全条件和 2A 单次只读授权。
- [x] `docs/sprint-artifacts/evidence/c-3b-2a-alicat-read-only-poll-2026-09-10.md` — 已保存 A/B/C 原始返回、解析、异常和 COM 释放依据。
- [x] `docs/sprint-artifacts/evidence/c-3b-alicat-ve-lss-read-only-2026-09-10.md` — 已保存 A/B/C firmware 与 LSS 原始返回；三台均为 `10v14.0-R24`、mode `S`。
- [x] `docs/sprint-artifacts/evidence/c-3b-alicat-safe-state-normalization-2026-09-10.md` — 已逐台清零并逐台改为 U；最终 setpoint/mass flow=`0/0/0`、LSS=`U/U/U`，COM6 已关闭。
- [ ] production physical verification UI — 获批后只验证首个气口并安全收口。
- [ ] `docs/sprint-artifacts/evidence/` — 保存当前运行的动作、回执、流量、时间与人工观察。
- [x] `app/main.py`、`app/controllers/main_controller.py`、`app/workers/` — 实现显示后一次性自动连接与分阶段安全 transaction；未接管时退出/全局停止不碰硬件。
- [x] `app/services/real_hal.py` — 保持构造 passive，并让 NI 首次物理 drive 使用按 polarity 计算的完整安全 image。
- [x] `app/views/main_window.py` — 正常路径不显示主“连接设备”按钮；失败后显示人工 Retry，Header geometry 稳定。
- [x] `docs/{architecture.md,project-context.md,ux-design.md}` — 固化启动与硬件接管分离、一次自动连接、无自动重试/恢复等产品规则。
- [x] `tests/` — 覆盖一次性 lifecycle、transaction 顺序、失败收口、断线、simulation 和 polarity。

**Acceptance Criteria:**
- Given 现场信息未确认，when 请求 C.3b-2，then 不接触真实硬件。
- Given 全部门禁获批，when 首次验证结束或中止，then 只涉及一个气口并留下完整安全终态。
- Given 回执失败、超时或出口不符，when 收尾，then 不写真实可用状态且不继续下一气口。
- Given App 尚未执行 queued auto-connect callback，when 构造、show 或关闭，then hardware I/O 始终为 0。
- Given startup auto-connect 失败，when 继续处理事件 60 秒，then 不出现第二次连接；仅人工 Retry 可使请求计数从 1 变为 2。
- Given safe DO、自检与 B/C/A 清零未全部完成，when UI/telemetry 更新，then `connected` 与 `hardware_ready` 仍为 false。

## Implementation Notes

- NI MAX 的 `0214581E` / `02145875` 是十六进制显示，分别等于历史十进制 `34887710` / `34887797`，不是设备更换证据。
- 2A poll 三个首 ID 均与查询地址大小写不敏感一致，未 timeout；B/C 只确认可寻址和 19200 通信，不确认 Setpoint Source。
- 设备返回按当前项目约定解析为 mass flow=`0.6/0.2/0.2 sccm`、setpoint=`1500/1500/500 sccm`。非零 setpoint 保持原状并阻断后续阶段。
- 后续 `VE` 返回三台均为 `10v14.0-R24`，达到 LSS 的 `10v05` 门槛；无参数 `LSS` 均返回 `S`。这确认 B/C 也使用保存型 Serial/Front Panel source，但不能确定当前非零值的具体来源。
- 2C 每台仅发送一次 `S0.000` 并立即验证，再仅发送一次 `LSS U` 并 query/poll；全部门禁通过，无重试。未做 power cycle，且未触碰 NI/selector/气味阀。
- 2026-09-11：产品明确启动后自动尝试连接一次，不提供开关；C.3b-3A 审计发现 worker 启动即接管 DO/串口、自检且过早发布 connected。真实 App Connect 尚未执行，本轮只做离线整改与验证。
- 2026-09-14：离线整改、三层复审和完整离线验证均已完成，记录为 `offline_lifecycle_remediation=done`；活动 spec 仍保持 `in-review`，`physical_commissioning_status` 为 `pending-c3b-3-real-connect`，不得据此宣称真实 App Connect 或后续气口验证已完成。
- 当前 `config/local_config.json` 的 21 个受管输出均计算为 safe LOW；在“USB-6001 上电 DIO 为 input 且弱 pull-down”的已知前提下，未发现当前 profile 的 polarity blocker。这是代码/配置判断，不是现场证据。
- 2026-09-14 首次真实 C.3b-3 已执行：startup auto-connect、自检、B→C→A 清零和零流量 idle 通过；Global Stop 首个 selector safe write 触发 NI-DAQmx `-200846`，selector/odor 安全回执不确定，系统正确进入 `RECOVERY_REQUIRED`，操作者随后断电。根因是首次 `auto_start=True` 单点 On-Demand safe write 返回后，DO task 未保持会话期 Running，而后续 `auto_start=False` 写错误地只以“session 对象存在”作为可写依据。历史 evidence 保持 FAIL；离线修复不能替代真实复测。

## Spec Change Log

- 2026-09-10：记录现场 NI/Alicat A/安全准备事实及 C.3b-2A 只读串口证据；因 A/B/C 非零 setpoint 与 B/C Setpoint Source 未确认，保持 `draft` 并停止。
- 2026-09-10：完成官方协议支持的 `VE`/无参数 `LSS` 只读查询，确认 A/B/C firmware=`10v14.0-R24`、mode=`S`；B/C Setpoint Source blocker 已解除，非零 setpoint 与 App Connect 授权仍未解决。
- 2026-09-10：完成 C.3b-2C；A/B/C setpoint 从 `1500/1500/500` 逐台清零，LSS 从 `S/S/S` 逐台改为 `U/U/U`。Alicat 安全初值 blocker 已解除；真实 App Connect 与气味阀真实初始关闭确认仍未授权/完成。
- 2026-09-11：人工重新确定产品启动语义为“窗口显示后自动连接一次”；移除原启动授权 open question，新增 passive startup、one-shot、统一 transaction、失败不自动重试、运行中断线不自动恢复和安全 DO 首次 image 约束。C.3b-3 真实连接仍未执行。
- 2026-09-14：人工进一步统一产品连接表现为“正在连接…”、“设备已连接”、“设备未连接”三态；startup failure、Global Stop 成功及 runtime disconnect 安全收口成功均显示“设备未连接 / 重新连接”。这项决定取代 Review B8 的旧终止性 UI 结论，但不削弱 SafeStop、unsafe latch 或严重安全提示。C.3b-3 真实连接仍未执行。
- 2026-09-14：首次真实 C.3b-3 的连接与零流量阶段通过，Global Stop 因 NI On-Demand DO task 生命周期错误失败并进入 `RECOVERY_REQUIRED`；真实失败已由独立 evidence commit 固化。C.3b-3C 只做离线修复，commissioning 继续 blocked，等待同规格真实重跑。
- 2026-09-14：真实复测 preflight 在 power-cycle 后因 A/B/C 无参数 LSS 均 timeout 安全停止；随后只读诊断发送 `aVE\r` 却收到 LSS 形态的 `A U\r`，证明存在 delayed/stale response 错归属风险。Real App 未启动、NI task 未创建、无硬件写入。当前仅开展离线 CR-framed serial transaction 修复；C.3b-3 继续 blocked，必须先重新进行只读 Alicat preflight 并记录 Poll/VE/LSS latency。

## Review Triage Log

| Layer/ID | 严重度 | 处理 | 证据/结论 |
|---|---|---|---|
| Blind B1 | high | direct-fix | self-check completion 携带 connection request ID，过期/异阶段失败不再撕毁当前连接。 |
| Blind B2 | high | direct-fix | 异阶段 self-check success 在任何状态变更前即被拒绝。 |
| Blind B3 | high | direct-fix | startup-zero completion 同时校验阶段与当前 request ID，旧 retry 结果不能推进新事务。 |
| Blind B4 | high | direct-fix | startup-zero context 改为每次连接唯一值，并且授权一次即消费。 |
| Blind B5 | high | direct-fix | `PREPARING_SAFE_OUTPUTS` 超时会取消并 join DO owner；资源归属不确定时进入恢复态。 |
| Blind B6 | high | direct-fix | `prepare_do_output()` 异常被 owner 捕获、回报并按已接管程度清理。 |
| Blind B7 | medium | direct-fix | Global Stop 成功/失败都会同步 connection phase、ready 状态和 UI。 |
| Blind B8 | medium | rejected | Global Stop 在既有产品契约中是本进程终止性安全收口；不支持同进程 reconnect 是预期行为。 |
| Blind B9 | medium | direct-fix | config/polarity/上次不安全退出 blocker 都进入稳定 FAILED/人工 Retry presentation。 |
| Blind B10 | low | direct-fix | Retry/重新连接状态 badge 使用错误色，不继承旧的 connecting 样式。 |
| Blind B11 | high | direct-fix | connected 后串口 sample 错误或 AI 错误会触发 runtime disconnect 与 fail-closed。 |
| Blind B12 | medium | rejected | 生产代码没有 connected 状态下的独立 self-check 调用者；重复连接请求按单事务契约拒绝，Reset 走专用安全路径。 |
| Blind B13 | medium | direct-fix | 硬件已部分/完全接管时 Global Stop 始终可用。 |
| Blind B14 | high | direct-fix | startup-zero bypass 改为 Controller 发放的一次性精确授权，不再接受任意来源字符串。 |
| Edge E1 | high | direct-fix | self-check signal 增加 request identity 并覆盖 late completion 回归。 |
| Edge E2 | high | direct-fix | zero receipt/context 与当前连接事务绑定。 |
| Edge E3 | high | direct-fix | prepare timeout 覆盖 owner 取消、释放以及不自动 retry。 |
| Edge E4 | high | direct-fix | HAL 暴露 DO resource ownership；孤儿资源进入 RECOVERY_REQUIRED。 |
| Edge E5 | high | rejected | legacy safety-union alias 的既有契约为 active-high；当前 HardwareProfile 仍覆盖其实际 targets 的 polarity。 |
| Edge E6 | high | direct-fix | active-low safe write 失败时 cache 保持配置的 safe level，而非硬编码 false。 |
| Edge E7 | medium | direct-fix | connection phase 拥有 Header；普通 telemetry 不再覆盖 connecting/failed/runtime-disconnected。 |
| Edge E8 | low | direct-fix | 失败态 badge 色彩回归已补齐。 |
| Gap G1 | medium | direct-fix | `hil_cleaning_gate.py` 改用权威 connection transaction；未运行真实入口。 |
| UX U1 | n/a | supersedes B8 | 2026-09-14 人工产品决策明确 Global Stop 成功后显示“设备未连接 / 重新连接”，人工重连复用唯一 transaction 且不恢复旧动作；失败仍保持 fail-closed 与持续断电提示。 |
| Gap G2 | medium | direct-fix | clean-clone smoke 源码改为 show 后 one-shot auto-connect，并断言 connected/ready；本轮未运行 clean-clone。 |
| Gap G3 | high | direct-fix | 新增 current-profile pull-down/polarity blocker 行为测试和离线配置审计。 |
| Gap G4 | high | direct-fix | 新增 prepare timeout、异常及 partial acquisition cleanup 回归。 |

## Verification

**Commands:**
- `python -m ruff check .` — 通过。
- 定向回归 — 139 passed；覆盖 one-shot lifecycle、统一 transaction、late result、timeout/partial cleanup、runtime disconnect、safe packed image 和 polarity。
- `python -m pytest` — 1320 passed，1 skipped；唯一 warning 为第三方 `qfluentwidgets` 使用 SciPy deprecated import。
- `git diff --check` — 通过；仅报告工作树 LF/CRLF 转换提示，无 whitespace error。
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 build` — PyInstaller 构建成功；未运行产物。
- offscreen MockHAL simulation smoke — exactly one startup auto-connect，进入 `CONNECTED` 并 clean shutdown。
- 当前 local config 离线审计 — `hal_mode=real`、COM6@19200、Dev1/Dev2；21 个受管输出的 safe level 均为 LOW，`pull_down_compatible=True`。
- 本轮未运行 probe、任何 HIL script、RealHAL hardware smoke、clean-clone，未打开 COM6 或真实 NI task。
