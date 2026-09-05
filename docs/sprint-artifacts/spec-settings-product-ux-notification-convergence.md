---
title: 'Settings 产品化 UX 与全局通知收敛'
type: 'refactor'
created: '2026-09-04'
status: 'done'
review_loop_iteration: 0
baseline_commit: '27bc38c3d175e1c4055459d46dfad32ace16e91e'
context:
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/ux-design.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Settings 仍呈现工程表单、开发术语和混合状态文字，气口状态不清；集中通知仍会因候选积压连续弹出。

**Approach:** 用双区、四态 Tile、单一详情卡和页内验证任务重构 Settings；以 sticky 单槽仲裁通知并修复 Qt 测试资源回收。

## Boundaries & Constraints

**Always:** 保持 HardwareProfile/Store/ChannelRegistry、preset、CAS、verification/safety 语义与默认八路；Mock 仅内部回归，production availability 只接受有效 `PHYSICAL_VERIFIED`；通知简短、行动导向。Coordinator 可维护多个 active condition，但 UI 只有一个 sticky winner；严格优先级可立即抢占，lower condition 只保留状态、不复制或堆叠。同 identity 的文案/严重度变化原地更新。

**Ask First:** profile schema、高级映射编辑、既有 target 迁移、任何真实硬件动作。

**Never:** 不做 Auto、Manual 主页面重构、真实 HIL/硬件命令；不产生 `PHYSICAL_VERIFIED`；产品 UI 不暴露开发或诊断术语；通知不是 FIFO toast queue。

## I/O & Edge-Case Matrix

| 场景 | 状态 | 行为 | 失败处理 |
|---|---|---|---|
| Settings 气口 | disabled/pending/physical/failed | 未使用/待验证/可用/异常；selected 独立强调 | changed/incomplete=需验证 |
| dirty draft | mapping/enabled/alias 等未保存 | 验证禁用，按钮附近仅提示“保存后验证” | 不用旧 saved mapping，不弹长通知 |
| simulation 验证 | clean saved、设备安全空闲 | 产品文案与页内任务流程不变；结束后回到“待验证” | 不显示绿色成功、日期或“验证完成” |
| actionable dismiss | winner 被用户关闭 | 该 episode dismiss-until-resolve，页面立即安静 | 不展示已有 equal/lower backlog；新 higher condition 可提示 |
| 通知状态变化 | 多 active condition/event | 每窗口一条；critical>error>warning>info>success；同 identity 原地更新 | resolve/实质状态变化后才重评仍有行动价值的问题 |
| transient | actionable 存在时到达 | success/info/warning suppress 并直接退休 | condition 解除后不得回放过时结果 |

</frozen-after-approval>

## Code Map

- `app/views/hardware_settings_view.py:187` -- 长布局、重复标题、高级表单、验证与页级状态。
- `app/views/manual_experiment_view.py:90,1101` -- PortTile 与唯一全局 InfoBar；Settings 应有专用 tile/presentation ownership，Manual geometry、selected/actual-open/fault 状态路径不改。
- `app/views/notification_coordinator.py:35` -- identity、dismiss 与仲裁。
- `app/controllers/main_controller.py:3644,3915,4312` -- 验证门禁/状态机/通知；动作语义不改。
- `app/views/main_window.py:263` -- 全局通知文案。
- `tests/conftest.py:85` -- QApplication、qtbot 与 DeferredDelete。
- `tests/test_{hardware_settings_view,notification_coordinator,manual_experiment_integration,product_ui}.py` -- 产品回归。

## Tasks & Acceptance

**Execution:**
- [x] `app/views/hardware_settings_view.py`, `app/views/manual_experiment_view.py` -- 双区、紧凑详情/表格、Settings 专用 Tile/presentation、独占验证面板；移除页级 status，不扩张共享类条件分支、不改变 Manual。
- [x] `app/views/notification_coordinator.py`, `app/views/main_window.py`, `app/controllers/main_controller.py` -- sticky 单槽、稳定 key、原地更新、抑制过时 transient、产品文案。
- [x] `tests/conftest.py`, `tests/` -- 覆盖四态/selected、dirty draft、Mock 隔离、Manual 语义、通知 dismiss/backlog/transient/new episode 和 DeferredDelete。
- [x] `docs/ux-design.md`, `docs/project-context.md` -- 固化长期原则。

**Acceptance Criteria:**
- Given 任一 Settings 气口，when 渲染，then 四态有文字/图标，selected 不覆盖状态，badge 无日期，`MOCK_VERIFIED`=待验证且不可用。
- Given dirty draft，when 尚未保存，then 验证禁用并邻近显示“保存后验证”；保存后再按设备门禁恢复。
- Given simulation 验证，when 启动/运行/结束，then 只见最终产品文案和页内流程，结束后仍待验证，不伪造真实人工确认。
- Given Manual Tile，when Settings 视觉变更，then Manual geometry 不变，selected 仍仅表示选择，actual-open 仍由真实 snapshot 驱动，fault 语义不变。
- Given 三个 actionable condition，when 重复/抢占，then visible InfoBar≤1；用户 dismiss winner 后不轮播已有 lower/equal backlog，新 higher condition 仍可提示。
- Given transient 被 actionable 阻挡，when condition 解除，then 旧 transient 不回放；全部 condition resolved 后的新 episode 可正常提示。
- Given 单进程完整测试，when `python -m pytest`，then 0 failures 且进程自然退出。

## Spec Change Log

## Verification

**Commands:**
- `python -m ruff check .`; 定向 pytest；`python -m pytest`; `git diff --check` -- 全部成功且完整 pytest 自然退出。
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 build` -- 打包成功。
- `python -m app.main --simulation` -- 仅模拟人工检查七类画面，无真实 HIL。

## Suggested Review Order

**Settings 信息架构**

- 双区与全宽详情
  [`hardware_settings_view.py:362`](../../app/views/hardware_settings_view.py#L362)

- 四态专用气口块
  [`hardware_settings_view.py:204`](../../app/views/hardware_settings_view.py#L204)

- 紧凑线路设备页
  [`hardware_settings_view.py:617`](../../app/views/hardware_settings_view.py#L617)

- 权限任务各归其位
  [`hardware_settings_view.py:850`](../../app/views/hardware_settings_view.py#L850)

**验证语义**

- 统一产品确认入口
  [`hardware_settings_view.py:1099`](../../app/views/hardware_settings_view.py#L1099)

- Mock 证据保持隔离
  [`main_controller.py:3913`](../../app/controllers/main_controller.py#L3913)

- 任务结束安全收尾
  [`main_controller.py:4152`](../../app/controllers/main_controller.py#L4152)

**通知仲裁**

- 单槽赢家状态机
  [`notification_coordinator.py:56`](../../app/views/notification_coordinator.py#L56)

- 关闭后页面安静
  [`notification_coordinator.py:236`](../../app/views/notification_coordinator.py#L236)

- 同身份原地更新
  [`manual_experiment_view.py:1102`](../../app/views/manual_experiment_view.py#L1102)

**资源与回归**

- 同步释放 FluentWindow
  [`conftest.py:77`](../../tests/conftest.py#L77)

- 四态选中独立验证
  [`test_hardware_settings_view.py:657`](../../tests/test_hardware_settings_view.py#L657)

- 通知不回放积压
  [`test_notification_coordinator.py:388`](../../tests/test_notification_coordinator.py#L388)

- Manual 语义保持不变
  [`test_manual_experiment_integration.py:697`](../../tests/test_manual_experiment_integration.py#L697)

- 真实窗口单槽集成
  [`test_product_ui.py:266`](../../tests/test_product_ui.py#L266)

- 产品窗口清理契约
  [`test_test_environment.py:52`](../../tests/test_test_environment.py#L52)
