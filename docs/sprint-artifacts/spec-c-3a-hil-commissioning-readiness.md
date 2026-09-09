---
title: 'C.3a 真实硬件 HIL commissioning 准备'
type: 'feature'
created: '2026-09-08'
status: 'done'
review_loop_iteration: 0
baseline_commit: '1354485cc7f2a3d146ebeb8ec011056c49e15cdf'
context:
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/architecture.md'
  - '{project-root}/docs/ux-design.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** production 单口 physical verification 仍是 no-actuation stub；验证参数、提前判定、线路输入与危险开关交互也未达到现场 HIL 前的安全门槛。

**Approach:** 在现有 Controller→ActuationWorker/FlowWorker→HAL、Verification lease、receipt 与 SafeStopPlan 边界内增加独立 physical verification 执行合同；复用已证明的 A-only 气路和安全偏序，同时收敛影响 HIL 的 Settings 配置与交互。

## Boundaries & Constraints

**Always:** A 经 selector odor route 进入目标阀，B=C=0；启动严格为 Verification lease/identity accepted→气味阀1–20全部 close receipts→单条 A-only flow command（A=配置值、B=C=0）matching receipt 且新鲜 SAFE/readback→selector odor matching receipt→目标阀 open matching receipt→以 open receipt `actual_ns` 启动 monotonic deadline。每一步只在当前 profile revision/fingerprint 仍匹配且收到本 run/operation/generation/command/lease 的成功回执后推进；stale、late、duplicate、conflicting 或失败回执均不推进，立即进入既有 fail-closed/SafeStop。lease 获取前不得发硬件动作。结束严格为目标阀 close receipt→A=0 receipt→selector compensation receipt→其余目标安全→释放 Verification lease。

上述启动顺序来自 `ActuationWorker` 已证明 Cleaning primitive（initial close→flow SAFE→selector odor→step open），而非重新设计气路；Story 4.5 真实 evidence 只证明停止偏序。若实现不能原样保持该启动序列、fresh SAFE/readback gate 或既有停止序列，必须 Ask First，不得自行重排 selector、目标阀和 A 非零 setpoint。

时长默认20秒、范围1–60秒。流量默认1500 ml/min，但它只是不跨硬件继承安全结论的产品初始建议值；必须 `0 < flow <= max_sample_a_sccm`、不超过当前设备/量程/现场证据支持范围并经过 MFC 校验，绝不 silent clamp。真实 C.3b 每次启动前显示并由用户确认气口、验证流量、最长验证时间；MFC、HardwareProfile、量程或设备身份变化后，1500 不构成既有现场证据，超出当前证据即阻止执行。RUNNING 可提前正/负/停止；正向仅在安全收口且 revision/fingerprint/run/receipt 匹配后落证据。timeout 只收口并等待确认。

**Ask First:** 发现权威气路与现有代码/现场证据冲突，或必须改变 selector/SafeStopPlan 的既有安全偏序。

**Never:** 不连接真实硬件、不执行 HIL、不 push；View 不访问 HAL/驱动；不冒用 Manual/Cleaning lease，不绕过 availability，不 silent clamp，不凭命令成功写 `PHYSICAL_VERIFIED`，不大改 UI 或创建 Epic/Story。

## I/O & Edge-Case Matrix

| 场景 | 输入 / 状态 | 预期行为 | 异常处理 |
|---|---|---|---|
| physical start | real HAL、ready/SAFE、clean mapping、合法参数、无 owner/recovery | Verification lease 下执行 A-only 路径 | 门禁失败不发 intent；流量超限给指定短提示 |
| 提前结果 | RUNNING 正/负/停止 | Worker 立即收口，完成后分别 VERIFIED、FAILED、INCOMPLETE | 收口/身份失败不得成功并走 recovery |
| timeout | monotonic deadline | 收口后 AWAITING_CONFIRMATION | 不自动成功 |
| 线路草稿 | canonical NI target / 非法、重复、selector 冲突、未登记 device | 合法草稿完成编辑后查看态立即显示，保存后统一 revision | 非法输入即时错误，完成/保存禁用，离开/取消恢复提交值 |
| 危险二值项 | enabled / polarity | 仅 SwitchButton 命中区改变；polarity 只在线路页显式高级编辑中可改 | 文字/空白点击无效；连接时禁改，polarity 变化使指纹失效 |

## C.3b 未执行 HIL Checklist Contract

`evidence/c-3b-hil-commissioning-checklist.md` 必须显著标记“未执行 checklist”，不得预填真实结果，并冻结以下现场门禁：

- A. 首次 HIL 不接受试者、不用气味样品，只用实验室批准的洁净气体/空气。
- B. 被验证气口出口完全畅通；手只放在出口前方感受气流，不捏住、封堵或直接堵住管口。
- C. 物理急停/电源停止手段随手可触。
- D. 启动前只读确认：非 simulation/mock；local config 为目标实验台配置；Dev1/Dev2 身份；Alicat 串口；A/B/C Unit ID；当前 mapping；selector target/polarity；A/B/C setpoint 为安全初值；气味阀1–20初始关闭。
- E. 第一轮只验证一个气口，不批量验证8路。
- F. 记录 command、exact receipt、NI target、flow setpoint/readback、verification run identity、open/close 时间、safe-close 完成和用户现场观察。
- G. USB-6001 DO 为 software-timed；C.3b 实测 command→DAQ write receipt、early result→close receipt、timeout→close receipt latency，UI countdown/fake clock 不得替代真实 timing evidence。
- H. 出现 unexpected valve/flow、通信中断、receipt mismatch、安全状态变化、mapping 与现场出口不符或无法安全归零/关闭，立即停止本轮并执行既有 global stop/recovery，不得自动继续下一气口。

</frozen-after-approval>

## Code Map

- `app/controllers/main_controller.py:3671-4358,4480-4583` — verification 门禁、stub、发布、global stop。
- `app/workers/actuation_worker.py:3736-4270,4697-5549`、`app/workers/flow_worker.py:577-622` — Cleaning 启动 primitive，以及可复用 receipt/deadline/安全收口与 lease 校验。
- `app/models/hardware_verification.py`、`app/models/hardware_profile.py:220-680`、`app/services/hardware_profile_store.py:118-320` — 执行/配置/完整证据与 CAS authority。
- `app/views/hardware_settings_view.py:68-159,526-930,1160-1528` — 参数、early result、线路/polarity/switch 交互。
- `config/default_config.json`、`config/local_config.example.json`、相关 `tests/test_*.py` — 默认、fake-HAL/证据/UI 回归。
- `docs/sprint-artifacts/evidence/gas-path-requirements-2026-08-01.md`、归档 4.5 safe-stop spec — 只读气路/偏序依据。

## Tasks & Acceptance

**Execution:**
- [x] models/store/config — 持久化 VerificationConfig 与完整 evidence，不改 mapping 指纹。
- [x] workers/controller — 建立 verification-owned plan，复用 receipt/MFC/deadline/SafeStop，替换 stub。
- [x] Settings/tests — 完成必要交互并覆盖17类 contract/regression、View→HAL 禁界。
- [x] 三份长期文档及 `evidence/c-3b-hil-commissioning-checklist.md` — 只写长期规则、backlog、未执行 checklist。

**Acceptance Criteria:**
- Given fake real HAL 与全部门禁成立，when 用户在3秒内选择任一结果，then Worker 不等20秒即按安全偏序收口，且 safe-close 前无成功 evidence。
- Given fake real HAL 记录完整 actuation sequence，when verification 开始并结束，then 启动严格为全阀 close receipts→A-only flow receipt/fresh SAFE→selector odor receipt→目标阀 open receipt，关闭严格为目标阀 close→A=0→selector compensation→其余目标安全→lease release；任一 identity 不匹配不推进。
- Given timeout、断连、安全变化、owner 冲突或 stale revision/fingerprint/run，when 执行/确认，then fail-closed、无 physical 成功并提供 recovery 路径。
- Given simulation 正向或可信 physical 正向，when 收口并确认，then 分别仅产生 `MOCK_VERIFIED`/完整 `PHYSICAL_VERIFIED`，Manual availability 只接受后者。
- Given 非法线路或大范围行点击，when 用户完成编辑/保存/点击文字空白，then 非法值不进入 draft commit，合法值查看态即时一致，二值状态不误切换。

## Spec Change Log

- 2026-09-08：冻结 Cleaning-derived 启动偏序与 exact-receipt gate；限定1500仅为配置默认值；补强未执行 C.3b 现场 checklist。

## Design Notes

不直接复用 Manual plan（它要求既有 production verification 且正常结束会恢复供气），也不把验证塞入 Cleaning ownership。Verification 自有 lease/identity/快照/终态，但严格复用 Cleaning 的全阀关闭、A-only flow+fresh SAFE、selector、单阀动作 primitive，以及 SafeStop fail-closed 机制。

## Verification

**Commands:**
- `python -m ruff check .`；相关定向 pytest；`python -m pytest` — 0 failures，自然结束。
- `git diff --check`；`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 build` — 通过。
- `python -m app.main --simulation` 与隔离 UI workflow — 参数、early positive/negative、timeout、stop、invalid/valid line、switch hit area 均通过且无真实硬件访问。
- 全部通过后本地提交 `feat(hil): 完善气口现场验证与调试准备`，工作区干净，不 push，停止等待 C.3b 人工批准。

## Suggested Review Order

**执行入口与所有权**

- 从生产入口核对门禁、lease 与 plan 构造。
  [`main_controller.py:4469`](../../app/controllers/main_controller.py#L4469)
- Worker 依 receipt 推进冻结的启动偏序。
  [`actuation_worker.py:4024`](../../app/workers/actuation_worker.py#L4024)
- Controller 在安全收口后提交最终证据。
  [`main_controller.py:4584`](../../app/controllers/main_controller.py#L4584)

**安全执行与证据**

- A-only 启动与两级归零保持安全偏序。
  [`actuation_worker.py:4159`](../../app/workers/actuation_worker.py#L4159)
- 回执身份、时限和状态推进集中校验。
  [`actuation_worker.py:4481`](../../app/workers/actuation_worker.py#L4481)
- 安全收口完成后才生成可信合同。
  [`actuation_worker.py:4748`](../../app/workers/actuation_worker.py#L4748)
- Alicat 回读不再使用目标值回显。
  [`real_hal.py:341`](../../app/services/real_hal.py#L341)
- 完整合同绑定运行、映射、流量与时间。
  [`hardware_verification.py:178`](../../app/models/hardware_verification.py#L178)
- Store 仅以 CAS 写入现场证据。
  [`hardware_profile_store.py:213`](../../app/services/hardware_profile_store.py#L213)

**Settings 与交互**

- 参数编辑保留非法输入并阻断启动。
  [`hardware_settings_view.py:928`](../../app/views/hardware_settings_view.py#L928)
- 高级 polarity 与危险控件权限集中收口。
  [`hardware_settings_view.py:1245`](../../app/views/hardware_settings_view.py#L1245)

**验证与现场交接**

- fake-HAL 断言完整启动、收口及证据。
  [`test_physical_verification.py:71`](../../tests/test_physical_verification.py#L71)
- 非默认参数重启后仍驱动实际 plan。
  [`test_hardware_profile_store.py:52`](../../tests/test_hardware_profile_store.py#L52)
- 长期架构记录 production 安全合同。
  [`architecture.md:159`](../architecture.md#L159)
- C.3b 清单明确未执行及现场门禁。
  [`c-3b-hil-commissioning-checklist.md:3`](evidence/c-3b-hil-commissioning-checklist.md#L3)
- HIL 后视觉债务集中留在单一 backlog。
  [`ux-design.md:44`](../ux-design.md#L44)
