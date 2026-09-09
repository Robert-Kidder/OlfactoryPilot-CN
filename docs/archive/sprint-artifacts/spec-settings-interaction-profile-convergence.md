---
title: 'Settings 交互闭环与配置呈现收敛'
type: 'bugfix'
created: '2026-09-05'
status: 'done'
review_loop_iteration: 0
baseline_commit: '1fb8a48b42b8adfe33b53d0f719bd8c2f40a6a96'
context:
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/architecture.md'
  - '{project-root}/docs/ux-design.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Settings 缺少完整设置入口、可维护线路与验证结束确认；倒计时解析文案造成跳秒。配置呈现依赖分散调用，缺成功提交后的统一版本契约。

**Approach:** 以 HardwareProfile/Store 为唯一配置来源，统一提交后刷新；用结构化验证状态闭合确认流程，收敛设置导航、表单与颜色。优先状态正确性、跨页一致性、验证闭环，其次信息架构和结构视觉。

## Boundaries & Constraints

**Always:** 保留现有 state、原子保存/补偿、版本与指纹校验、owner/lease/safety 门禁。普通保存要求断开且安全交还；连接字段保留重启要求。UI timer 只显示，关闭和安全停止归动作 owner。真实可用性只接受匹配指纹的 PHYSICAL_VERIFIED；simulation 不产生现场证据。

**Ask First:** 超出本范围的产品或执行域变化。

**Never:** 不创建 Epic/Story，不开发 Auto，不改 Manual 曲线/流量卡/实验卡/停止按钮整体设计，不连接真实 NI/Alicat/阀门，不运行 HIL，不 push；Manual 不读配置文件，不另建配置 state，不放宽通用证据写入接口。

## I/O & Edge-Case Matrix

| 场景 | 输入/状态 | 结果 | 异常 |
|---|---|---|---|
| 保存 | dirty 且 valid、断开安全态 | 提交后两个 View 同一 revision，短暂“设置已保存” | clean 禁存；失败保留 draft、补偿旧配置 |
| 连接参数 | 串口、NI IDs、Alicat A/B/C 改动 | 显示新保存值，“设置已保存，重启后生效” | 不重建已连接硬件 |
| 普通配置 | alias/enabled/mapping/target/polarity | 立即更新呈现；下次连接用新映射 | 映射/极性改动使证据失效 |
| 气口选择 | Settings / Manual / 通道选项 | 编辑可选 / 动作受门禁 / 占用项禁用 | 保留全部通道，自用项可选，显示“已用于气口 06” |
| 验证结束 | 动作完成且安全收尾 | 等待“没有或位置不对 / 出气正确” | 否定=FAILED；停止/失效=INCOMPLETE |
| 正向确认 | simulation / 已授权 physical contract | MOCK_VERIFIED、待现场确认 / PHYSICAL_VERIFIED、可用 | 过期、重复或无匹配动作证据不得提交 |

</frozen-after-approval>

## Code Map

- `app/controllers/main_controller.py:3770,4252,4288`：保存先 rebind、刷新 Manual 再磁盘 commit；验证发布后依靠释放 lease 刷新。应统一成功提交后通知，不能声称当前完全没有刷新。
- `app/views/manual_experiment_view.py:706`：registry 缓存包含完整 descriptor；离屏窗口探针中气口04改 alias 并停用立即同步，持续不同步尚未复现。
- `app/controllers/main_controller.py:4026,4039,5251`、`app/views/hardware_settings_view.py:929`：tick 与工具栏刷新交替改变文案，regex 未匹配时重置20秒；完成路径直接写 MOCK_VERIFIED。
- `app/models/hardware_profile.py:293,503,565`、`app/services/hardware_profile_store.py:118,192`：所有已映射气口（含停用）保持通道/线路唯一；preset 尚未序列化；现场证据写入目前封闭。
- `app/views/hardware_settings_view.py:395,426,617`：首页缺失，viewport 强制背景，线路/连接使用 Label；`ValveComboBox` 未设置 occupied 状态。已安装 QFluentWidgets 1.11.3 支持 `setItemEnabled` 与 BreadcrumbBar。
- `app/views/main_window.py:117,150`、`app/main.py:126`：Manual palette 与 Settings viewport、库默认 Card 背景不同；Header 380px slot 拉伸仅需66px的徽标。
- `app/views/notification_coordinator.py`：保留单 winner；`scripts/capture_story_4_6_ui.py` 引用已删除的 settings_dialog，截图脚本需更新。

## Tasks & Acceptance

**Execution:**
- [x] `app/models/hardware_profile.py`, `app/services/hardware_profile_store.py`：可选 canonical target_preset 持久化，兼容旧20路配置；高级线路修改同步绑定 descriptor，未绑定线路也可保存；保留不相关自定义线路。完整校验设备、唯一性、selector 冲突，原子保存/回滚覆盖线路表；兼容配置键只作派生输出。极性编辑归已映射 descriptor。
- [x] `app/models/hardware_verification.py`, `app/models/__init__.py`, `app/controllers/main_controller.py`：结构化 phase、external_port、started/deadline、duration、can_stop、awaiting_user_confirmation、result；保留 run identity/revision/fingerprint。统一保存/回滚/证据提交后的 profile presentation refresh，两个 View 带同一 revision；失败补偿刷新旧值，不呈现尚未提交候选。
- [x] `app/views/hardware_settings_view.py`, `app/views/main_window.py`：设置首页两入口卡+返回导航；Manual 快捷入口直达气口配置。2×10气口五态，selected 独立 outline；设备连接断开可编辑；线路01–10/11–20默认紧凑查看，显式“编辑线路”；共享 dirty/valid 保存动作。名称最大420px、通道280px、串口320px、设备420px、Alicat280px，紧凑双列表单。
- [x] `app/views/hardware_settings_view.py`, `app/controllers/main_controller.py`：开始确认→RUNNING 倒计时/确定进度/立即停止→AWAITING_CONFIRMATION→结果。停止、安全失效、断连、退出和过期确认均拒绝成功证据；页内任务不发全局进度通知，成功 transient、可行动失败走现有 winner。
- [x] `app/views/port_formatting.py`, `app/views/product_theme.py`, `app/views/manual_experiment_view.py`, `app/views/main_window.py`, `app/main.py`：共享编号优先与 alias elide helper；集中 page/surface/secondary/border/amber/text/success/warning/error tokens，viewport transparent，Card 保持原生层级、Mica关闭。Manual只改同步、名称/状态、“总流量”；Header徽标内容宽度、外部geometry稳定。
- [x] `tests/test_hardware_profile.py`, `tests/test_hardware_profile_store.py`, `tests/test_hardware_settings_view.py`, `tests/test_manual_experiment_view.py`, `tests/test_manual_experiment_integration.py`, `tests/test_product_ui.py`, `tests/test_notification_coordinator.py`：覆盖下列契约及矩阵，更新旧自动验证完成/alias优先/只读设置断言；保留失败补偿、拒绝越权、自然退出回归。
- [x] `scripts/capture_story_4_6_ui.py`, `docs/screenshots/`：用隔离 simulation 配置生成十类截图并检查实际画面；状态展示 fixture 不写 production evidence。`docs/ux-design.md`, `docs/architecture.md`, `docs/project-context.md` 只更新长期约定；现场未证实项归 architecture 的 C.3 HIL commissioning checklist。

**Acceptance Criteria:**
- Given 气口04启用、空alias、通道03，when 改为柠檬并停用保存，then Manual立即显示“气口 04”主标题、“柠檬”副标题及不可用状态；返回Settings保持一致，无需重开页面或应用。
- Given 气口04映射03，when 保存10，then Store/state.profile/state.registry/Settings/Manual版本一致且使用10，证据失效，下次连接解析10的target；另测编辑未绑定10的线路、重载后再映射仍使用新线路。
- Given 气口04分别处于未启用Settings、不可用Manual、通道已占用，when 点击，then 分别可选择编辑、无actuation、选项可见但禁选并标明占用；所有已映射气口保留唯一性。
- Given 已知20秒验证，when fake clock推进且穿插无关status刷新，then 20→19→…→0单调下降、进度不重置；动作完成保持AWAITING_CONFIRMATION，只有当前运行的用户确认能完成事务。
- Given simulation正向确认，when 保存证据，then MOCK_VERIFIED、待现场确认、production availability=false；Given可信physical完成契约，when用户正向确认，then才允许生成PHYSICAL_VERIFIED并可用；production实际执行入口仍不开放，普通store接口不能绕过契约。
- Given 否定/停止/断线/过期或重复确认，when终结，then分别FAILED/INCOMPLETE/拒绝写入，释放所有权且不自动转成功。
- Given 任一气口有/无长alias，when两页渲染，then编号永远主信息；alias只在第二行，截断才tooltip。Settings状态为未启用/待验证/待现场确认/可用/需检查；Manual unavailable muted、available neutral、selected amber、actual-open green、fault red且有可见文字/图标。
- Given 保存/验证/连接/安全通知交错，when渲染，then最多一个global winner，页内验证不形成InfoBar堆叠；保存结果不常驻。
- Given simulation截图，when人工检查，then首页、气口配置、五态、alias、线路查看、线路编辑、验证开始确认、运行、结束确认、Manual即时同步均完整；无明显背景拼色、超宽输入框或Mock/内部术语。

## Spec Change Log

## Design Notes

物理验证应先形成纯模型/受控 fake owner 的确认契约，要求已授权流程、匹配完成/安全关闭证据和用户正向确认，不能仅凭按钮或环境flag写现场证据；实际设备动作留C.3。等待确认期间保持验证流程所有权，安全变化中止；不靠UI timer关阀。现有正常验证完成时释放lease会刷新Manual，不能将其误报为永久漏刷。

## Verification

**Commands:**
- `python -m ruff check .`；相关定向pytest；`python -m pytest`：0 failures，自然结束。
- `git diff --check`；`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 build`：成功。
- `python -m app.main --simulation`：截图与smoke通过，无真实硬件动作。
- implement/review/verify全部完成后本地提交 `fix(settings): 修复配置同步与验证交互`；记录hash，`git status`干净，不push，然后等待人工验收。

**Results:**

- `python -m ruff check .`：通过。
- `python -m pytest --basetemp=.tmp/pytest-full-final`：1124 passed，0 failures，自然结束。
- `git diff --check`：通过。
- `scripts/run-ci.ps1 build`：通过，生成 Windows 可执行文件。
- `python -m app.main --simulation`：主窗口、Mock 自检及 worker 启动通过；确认后关闭事件循环。
- simulation 截图：10/10 已生成并人工检查。

## Suggested Review Order

**配置发布**

- 从保存入口理解边界
  [`main_controller.py:3797`](../../../app/controllers/main_controller.py#L3797)

- 提交后统一刷新
  [`main_controller.py:4411`](../../../app/controllers/main_controller.py#L4411)

- 线路表成为持久配置
  [`hardware_profile.py:21`](../../../app/models/hardware_profile.py#L21)

**验证闭环**

- 结构化生命周期模型
  [`hardware_verification.py:20`](../../../app/models/hardware_verification.py#L20)

- 现场证据受控提交
  [`hardware_profile_store.py:213`](../../../app/services/hardware_profile_store.py#L213)

- Controller 发布终态
  [`main_controller.py:4558`](../../../app/controllers/main_controller.py#L4558)

**设置体验**

- 设置中心主入口
  [`hardware_settings_view.py:384`](../../../app/views/hardware_settings_view.py#L384)

- 首页两类设置
  [`hardware_settings_view.py:606`](../../../app/views/hardware_settings_view.py#L606)

- 线路设备紧凑编辑
  [`hardware_settings_view.py:750`](../../../app/views/hardware_settings_view.py#L750)

- 占用通道仍可理解
  [`hardware_settings_view.py:195`](../../../app/views/hardware_settings_view.py#L195)

**共享呈现**

- 编号始终优先
  [`port_formatting.py:29`](../../../app/views/port_formatting.py#L29)

- 产品色彩集中定义
  [`product_theme.py:10`](../../../app/views/product_theme.py#L10)

- Manual 状态分层
  [`manual_experiment_view.py:96`](../../../app/views/manual_experiment_view.py#L96)

**回归证据**

- alias 停用即时同步
  [`test_manual_experiment_integration.py:464`](../../../tests/test_manual_experiment_integration.py#L464)

- mapping 同版本发布
  [`test_manual_experiment_integration.py:495`](../../../tests/test_manual_experiment_integration.py#L495)

- deadline 单调倒计时
  [`test_hardware_settings_view.py:606`](../../../tests/test_hardware_settings_view.py#L606)
