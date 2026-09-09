---
title: '修复手动实验运行态同步与界面闪烁'
type: 'bugfix'
created: '2026-08-25'
status: 'done'
review_loop_iteration: 0
baseline_commit: 'afb0e2c02f3523de67bd14272ccfb9f4dbf0daaf'
context:
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/architecture.md'
  - '{project-root}/docs/ux-design.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** QFluentWidgets 手动实验页由 telemetry、manual snapshot、status 等入口分别渲染并直接创建占布局 InfoBar，连接与通知切换会重排页面，高频 snapshot 会重复刷新全部控件；真实 QThread 中 flow receipt 还可能被写后 telemetry/readiness 反超，使正常 Manual 收尾误入恢复。

**Approach:** 用一致的页面 presentation snapshot、语义通知仲裁和幂等 diff render 稳定 UI；关闭 Mica、固定 Header 与 overlay InfoBar；修正 owner-to-owner queued 顺序并以真实 Qt event loop 单循环及三循环 soak 锁定行为。

## Boundaries & Constraints

**Always:** 保持 Controller/Worker/HAL 单写者、exact lease、epoch/generation/receipt/deadline、stale/late/conflicting 拒绝及 SafeStopPlan/A=0/selector compensation/final convergence 不变量；状态真实性优先，通知按用户语义 identity 而非 cooldown 去重；只使用 simulation/Mock HAL。DirectConnection callback 在 FlowWorker emitter thread 中仅接收 immutable/copied receipt、锁定专用 mailbox、enqueue 后立即返回；receipt/Manual/安全状态机仍由 Actuation owner 串行消费，presentation 仍由 GUI thread 更新。

**Ask First:** 若需访问真实硬件，或改变 RealHAL、真实极性/时序、SafeStopPlan 偏序、物理验证规则，立即停止请求授权。

**Never:** 新建 Epic/Story、实现 Settings、重做产品视觉、删除 pyqtgraph、恢复 QMessageBox、泄露内部状态码、放宽安全证据、修改硬件映射语义、push。DirectConnection callback 不得修改 MainController/AppState presentation、调用 QWidget/GUI API、操作 QObject timer、直接运行 Actuation 状态机、修改 owner-only 状态或依赖 receiver QObject affinity 保证线程安全。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 正常运行 | connect→supply→04→短时 release；真实 QApplication 与三个 QThread | close→A=0→compensation→restore→COMPLETED；阀全关、供气恢复、lease 释放 | 不产生 Recovery、异常 SafeStopPlan 或重复 actionable notice |
| 通知状态 | 多 source 报告同一持续异常；用户关闭；恢复后再进入 | 同 episode 单 identity；关闭后不重建；全部恢复后允许下一 episode | 严重安全异常按显式优先级立即抢占普通事件 |
| 高频同态 | 5 Hz 相同 presentation/port 状态及重复显示值 | 不重复 mutate/repaint；新 telemetry sample 仍保留真实时序 | 不以只比较数值丢弃有效新样本 |

</frozen-after-approval>

## Code Map

- `app/controllers/main_controller.py:332-382,741-811,3410-3514,4101-4146` -- Flow result 当前经 GUI AutoConnection；telemetry/header/status/manual 多入口及同步 test bridge；在此生成并 coalesce 单一 presentation generation。
- `app/workers/flow_worker.py:302-323,504-517,657-667` 与 `app/workers/hardware_worker.py:197-221,456-490` -- result 发射后同线程可立即发布 airflow/readiness，解释真实排队反超；仅作证据，保持 owner/serial 契约。
- `app/workers/actuation_worker.py:4537-5328` -- Manual 正常严格收尾与 fail-closed 门禁；不放宽状态机，仅消费先入 owner 队列的 correlated flow result。
- `app/views/main_window.py:45-337` -- Win11 默认 Mica、动态连接按钮/异常文字重排、六类通知源和无条件 header mutation。
- `app/views/manual_experiment_view.py:87-297,398-1016` -- PortTile 全量 mutation；同一 controller render 触发三轮 20 tiles；InfoBarPosition.NONE + notice host 改变主布局；固定 plot range 已满足本轮边界。
- `app/models/manual_experiment.py:201-232` 与 `app/views/product_text.py` -- immutable domain snapshot 与既有用户文案映射复用点；不从字符串反推状态。
- `tests/test_manual_experiment_integration.py:37-186` -- 现有同步 bridge 基线，保留；未覆盖 QThread/GUI queue/airflow poll。
- `tests/test_manual_experiment_view.py`, `tests/test_product_ui.py`, `tests/test_flow_controls.py:190-243`, `tests/conftest.py` -- overlay/geometry/diff/通知测试落点及真实线程等待范例；`qtbot` 是 no-op，使用 QSignalSpy/QTest/processEvents。

## Tasks & Acceptance

**Execution:**
- [x] `app/views/notification_coordinator.py`, `main_window.py`, `manual_experiment_view.py` -- 建立 condition/event 分离的唯一通知输出；semantic episode identity、dismiss-until-resolve、严重级优先；用官方 BOTTOM_RIGHT overlay，删除主布局 notice host。
- [x] `app/views/main_window.py` -- `super()` 后默认关闭 Mica；用固定连接状态容器/动作占位保持 header geometry，已连接只显示简洁状态。
- [x] `app/controllers/main_controller.py`, `manual_experiment_view.py` -- 每次合成一个带 generation 的 coherent presentation snapshot，一帧统一 connected/hardware ready/manual/supply/actionability；ready 时清除旧 readiness detail，queued render 合并且旧 generation 不覆盖新帧。
- [x] `app/views/manual_experiment_view.py` -- PortTile 缓存六元视觉状态；控件、badge、countdown、plot 做最小同值更新；registry 仅变化时重建端口 presentation，不改图表视觉设计。
- [x] `app/controllers/main_controller.py`, `app/workers/actuation_worker.py` -- 建立受限的 thread-safe DirectConnection ingress；现有 receiver 若不满足契约则使用最小 mailbox，禁止把 GUI Controller 变成跨线程共享状态；恢复 result→airflow producer 顺序，不改状态机门禁。
- [x] `tests/` -- 增加通知仲裁、Header/overlay geometry、Mica、同 snapshot mutation、矛盾帧测试，以及真实 QApplication+queued signals+三个 QThread 的完整单循环和三循环 soak。

**Acceptance Criteria:**
- Given connected/SAFE/hardware-ready 变化与任何 queued manual snapshot，when 同一 presentation generation 渲染，then Header、可操作性和通知不矛盾，核心 Header/主页面 geometry 不变。
- Given 相同持续异常来自任意 source，when 用户关闭并继续收到同态更新，then 不重建；全部 source 恢复后再次进入才提示，普通 connected/SAFE/idle/stable 不提示。
- Given 真实 Qt runtime 完成 3 次 Manual release，when 每次 COMPLETED 后继续泵至少两个 telemetry 周期，then 无 Recovery/Protocol 抢占/异常 SafeStopPlan/notification storm，阀门全关、供气恢复、lease 释放且下一轮可启动。
- Given FlowWorker 在 emitter thread 发射 result，when DirectConnection ingress 接收回执，then callback 只完成 thread-safe enqueue 并立即返回，真正 receipt handling/Manual transition 位于 Actuation owner thread，所有 presentation mutation 位于 GUI thread，且三循环无跨线程 QObject warning 或 crash。
- Given 完全相同 snapshot，when 再次 render，then PortTile 无 visual mutation；单端口变化只更新对应 tile，pyqtgraph 不触发布局或逐帧 auto-range。

## Spec Change Log

## Design Notes

错误 Recovery 根因不是状态机偏序：Flow result 先经 GUI queue，而同一 worker 紧接着直接发布写后 airflow/readiness；GUI 忙时 readiness 反超。A=0 后的 LOW_FLOW 或 restore 前的 `flow_setpoints_ready=False` 因而误触发恢复。Direct slot 在 emitter thread 执行且不因 receiver affinity 获得安全性；它只按上述 mailbox 契约恢复 producer 顺序。

## Verification

**Commands:**
- `python -m ruff check .` -- 全通过。
- 定向 pytest：manual/controller/actuation/flow/telemetry/simulation/safety/Qt UI/notification -- 全通过。
- DirectConnection 线程归属回归 -- 证明 ingress 在 FlowWorker emitter thread 仅 enqueue，receipt handling/状态转换在 Actuation owner thread，presentation mutation 在 GUI thread；三循环期间捕获不到跨线程 QObject warning/crash。
- `python -m pytest` 与 `git diff --check` -- 全通过。
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 build` -- 新 import 可打包。
- `python -m app.main --simulation` -- Windows 实窗完成连接、供气、04 三次释放、关闭通知；无闪烁/矛盾/Recovery/异常日志。

## Suggested Review Order

**线程顺序与安全收敛**

- 从受限跨线程入口开始
  [`actuation_worker.py:1220`](../../../app/workers/actuation_worker.py#L1220)

- 按到达序仲裁安全消息
  [`actuation_worker.py:2745`](../../../app/workers/actuation_worker.py#L2745)

- 回执仅由 owner 消费
  [`actuation_worker.py:4844`](../../../app/workers/actuation_worker.py#L4844)

- 恢复后等待新鲜 SAFE
  [`actuation_worker.py:4918`](../../../app/workers/actuation_worker.py#L4918)

**一致 presentation 与稳定布局**

- 合成单一状态帧
  [`main_controller.py:3459`](../../../app/controllers/main_controller.py#L3459)

- 合并并拒绝旧 generation
  [`main_window.py:327`](../../../app/views/main_window.py#L327)

- 固定连接区几何占位
  [`main_window.py:147`](../../../app/views/main_window.py#L147)

- 默认关闭窗口 Mica
  [`main_window.py:54`](../../../app/views/main_window.py#L54)

**通知与幂等渲染**

- 统一语义 episode 仲裁
  [`notification_coordinator.py:49`](../../../app/views/notification_coordinator.py#L49)

- 单帧驱动页面渲染
  [`manual_experiment_view.py:870`](../../../app/views/manual_experiment_view.py#L870)

- PortTile 缓存视觉状态
  [`manual_experiment_view.py:90`](../../../app/views/manual_experiment_view.py#L90)

- 曲线只接收真实新样本
  [`manual_experiment_view.py:948`](../../../app/views/manual_experiment_view.py#L948)

**回归证据**

- 三线程三循环真实 soak
  [`test_manual_runtime_stability.py:103`](../../../tests/test_manual_runtime_stability.py#L103)

- 验证 ingress 只入队
  [`test_manual_runtime_stability.py:66`](../../../tests/test_manual_runtime_stability.py#L66)

- 锁定恢复气流超时
  [`test_manual_experiment.py:846`](../../../tests/test_manual_experiment.py#L846)

- 锁定通知 episode 语义
  [`test_notification_coordinator.py:4`](../../../tests/test_notification_coordinator.py#L4)
