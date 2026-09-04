---
title: 'QFluentWidgets 气口与硬件设置页'
type: 'feature'
created: '2026-09-03'
status: 'done'
review_loop_iteration: 0
baseline_commit: 'c9446bf347e32d0a9558e5615ec5c7b23fd0f2a5'
context:
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/architecture.md'
  - '{project-root}/docs/ux-design.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 正式运行树没有 Settings；旧页虽有 draft/保存/验证，却混淆面板气口、控制通道和 NI target，改通道会残留旧 target。

**Approach:** 复用现有 HardwareProfile 后端，交付固定 2×10 Fluent Settings、单口编辑、只读高级 preset、集中通知和模拟验证。

## Boundaries & Constraints

**Always:** `external_port`=面板气口01–20，`internal_valve`=控制通道，`target`=resolved NI 接口。`valve_mapping.variants["20-channel"]` 是 target preset，HardwareProfile 是运行/持久化 authority；普通改通道同步 preset，历史偏差标为高级自定义，重复映射立即阻断。enabled≠已验证；production 只接受有效 `PHYSICAL_VERIFIED`。配置编辑/普通保存仅在断开且无 owner 时经 draft、CAS、atomic、LKG 完成；连接后只读、不 hot reload。verification 独立授权：仅验证 clean saved profile 的单口；UI/runtime revision+fingerprint 一致，connected+ready+safe idle、无竞争 owner并取得专用 ownership 后才可请求。evidence 事务只更新匹配 revision+fingerprint 的 verification，不得改 mapping。

**Ask First:** 修改 profile schema、迁移既有自定义 target、开放高级映射编辑或真实硬件动作。

**Never:** 不建 Epic/Story；不做 Manual 重构（快捷入口除外）、Auto、USB-6501、SuperLab 或真实 HIL；View 不直控 HAL；不用 QConfig；不删除 rollback 后端，也不在普通页显示 rollback。

## I/O & Edge-Case Matrix

| State | Behavior | Failure |
|---|---|---|
| 默认 profile | 02→02、04→03、06→04、08→05、12→06、14→07、16→08、18→09 | 不得顺序化 |
| 气口04改控制10 | 同步 target，验证=`MAPPING_CHANGED` | preset 缺失则阻断 |
| enabled 气口06选控制03 | 提示通道03已被气口04使用 | 不覆盖/换号/保存 |
| 断开、无 owner | 编辑 draft、普通保存；不 physical verify | 仍经保存门禁 |
| 连接、clean saved、safe idle | mapping 只读、保存禁用、验证可用 | simulation=mock；production=stub |
| 任一竞争 owner | 只读，保存/验证禁用 | actuation 前拒绝 |
| unsaved draft | 验证禁用 | 提示先保存并重连 |
| simulation 单口验证 | 确认/进度/停止；专用持久化 `MOCK_VERIFIED` | 取消=`INCOMPLETE`；失败=`FAILED` |
| verification result | 仅更新匹配 revision+fingerprint 的 evidence | mismatch 拒绝且 mapping 不变 |
| 保存后重启 | alias/mapping/target/polarity/status/revision 恢复 | stale revision 不覆盖 |

</frozen-after-approval>

## Code Map

- `app/models/hardware_profile.py:20-335,466-492` -- 三层 registry、preset 解析、availability/fingerprint/失效。
- `app/services/hardware_profile_store.py:100-337` -- 普通保存不变量；新增受限 verification evidence CAS/atomic 更新。
- `app/views/hardware_settings_view.py:35-631` -- 保留 draft，改造 Fluent 页面并移除 rollback UI。
- `app/views/manual_experiment_view.py:90-226,624-642`; `app/views/main_window.py:49-184` -- 共享 PortTile；bottom Settings 与唯一快捷入口。
- `app/controllers/main_controller.py:3558-3938` -- 分离编辑/验证门禁，编排 Mock receipts/evidence、runtime 发布与通知；physical 保持无动作 stub。
- `config/default_config.json:62-139`; `tests/test_hardware_{profile,profile_store,settings_view}.py`; `tests/test_{manual_experiment_integration,product_ui}.py` -- 默认/preset 与端到端回归。

## Tasks & Acceptance

**Execution:**
- [x] `app/models/hardware_profile.py`, `app/services/hardware_profile_store.py` -- 建立 preset/resolution、自定义语义和 verification-only CAS 事务。
- [x] `app/views/hardware_settings_view.py` -- 用 SettingCardGroup、SettingCard、Switch、ComboBox、LineEdit、Expand、Badge、Button、MessageBox、Progress 实现总览、详情、高级只读和验证 UX。
- [x] `app/views/main_window.py`, `app/views/manual_experiment_view.py`, `app/controllers/main_controller.py` -- 接入导航/快捷入口/通知；分离编辑与验证权限，Mock 只验证 clean saved profile，physical 保持 stub。
- [x] `tests/`、三份长期文档 -- 覆盖矩阵、alias elide、availability、持久化/重启/revision/LKG、导航和 HIL 边界；只作必要文档增量。

**Acceptance Criteria:**
- Given 默认配置，when 选择气口04，then 2×10 总览不变，详情显示控制03，高级显示 `Dev1/P0.2`。
- Given 已验证 mapping，when panel→control 或 target/polarity 变化，then fingerprint 失效；仅 alias/enabled 变化不伪造物理验证。
- Given saved profile、模拟设备 connected/ready/safe idle 且无竞争 owner，when 单口验证，then mapping 只读、持久化 `MOCK_VERIFIED`、重启恢复且 production unavailable。
- Given unsaved draft，when 请求验证，then 在 hardware intent 前拒绝并提示先保存、重连。
- Given Manual/其他 owner active，when 请求验证，then 在 actuation 前拒绝。
- Given verification result，when revision+fingerprint 匹配，then 只更新该口 evidence；否则拒绝且 mapping 四字段不变。
- Given production，when physical backend 仍为 stub，then 无真实执行和现场确认绝不产生 `PHYSICAL_VERIFIED`。
- Given Manual，when 点击“气口设置”，then `switchTo()` 同一 Settings，Manual 其余视觉不变。

## Spec Change Log

## Design Notes

preset 表示标准接线，descriptor.target 表示当前运行值；普通 UI 只生成 preset-resolved target，历史非 preset 值只读标识为“自定义映射”。高级编辑待同事务持久化和 HIL 边界完善后开放。

## Verification

**Commands:**
- `python -m ruff check .`; 定向 pytest; `python -m pytest`; `git diff --check` -- 0 failure。
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 build` -- 成功。
- `python -m app.main --simulation` -- 不连接真实硬件，完成指定 Settings/重启/Manual smoke。

## Suggested Review Order

**安全权限与验证事务**

- 先看配置编辑与验证权限如何独立判定并持续复核。
  [`main_controller.py:3644`](../../app/controllers/main_controller.py#L3644)

- Mock 单口验证取得专用 ownership，production 保持无动作 stub。
  [`main_controller.py:3915`](../../app/controllers/main_controller.py#L3915)

- evidence-only CAS 只更新匹配 revision 与 fingerprint 的证据。
  [`hardware_profile_store.py:178`](../../app/services/hardware_profile_store.py#L178)

**三层映射与一致性**

- preset 定义稳定的控制通道到 NI target 关系。
  [`hardware_profile.py:21`](../../app/models/hardware_profile.py#L21)

- 映射 fingerprint 变化沿用既有验证失效机制。
  [`hardware_profile.py:537`](../../app/models/hardware_profile.py#L537)

- 普通改控制通道同步 preset target，缺失 preset 即阻断。
  [`hardware_settings_view.py:697`](../../app/views/hardware_settings_view.py#L697)

**正式 Settings 产品界面**

- 页面组织固定 2×10 总览、单口详情和只读高级区。
  [`hardware_settings_view.py:187`](../../app/views/hardware_settings_view.py#L187)

- Settings 固定置于 FluentWindow 底部导航。
  [`main_window.py:189`](../../app/views/main_window.py#L189)

- Manual 快捷入口切换到同一 Settings 页面。
  [`manual_experiment_view.py:661`](../../app/views/manual_experiment_view.py#L661)

**回归证据与长期约束**

- 默认八路、修改映射、失效与重启恢复集中回归。
  [`test_hardware_profile_store.py:333`](../../tests/test_hardware_profile_store.py#L333)

- clean saved、竞争 owner 与 actuation 前拒绝形成端到端验收。
  [`test_manual_experiment_integration.py:679`](../../tests/test_manual_experiment_integration.py#L679)

- 三层 authority 与默认映射写入长期架构约束。
  [`architecture.md:155`](../architecture.md#L155)
