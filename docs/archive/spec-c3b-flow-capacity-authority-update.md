---
title: 'C.3b-4 Alicat 三路容量 authority 更新'
type: 'chore'
created: '2026-09-15'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
baseline_commit: 'f88c0da82419121f0a7a9601b5bdb614fbf163a4'
context:
  - 'docs/sprint-artifacts/spec-c3b-nonzero-flow-commissioning.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** C.3b-4 当前代码、配置、测试和 authority 仍把 B/C Alicat 容量记为未知，但现场已人工确认 A/B/C 均为 5000 sccm 满量程。

**Approach:** 将三路 device capacity 统一为 5000/5000/5000 sccm，同时保持 commissioning approved maxima 为 500/0/0 sccm、默认授权关闭及所有真实 HIL 边界；B/C 非零必须继续因 approved limit=0 在硬件提交前被拒绝。

</frozen-after-approval>

## Implementation Notes

- 预计仅修改 `FlowDeviceCapacities` 默认值及配置解析默认值、版本化默认/示例配置、C.3b-4 authority 和容量门禁回归。保留通用 `None` 容量的 fail-closed 能力，但不再把当前 B/C 硬件事实建模为 `None`。
- 不修改真实供气 transaction、B→C→A、SafeStop、NI/serial lifecycle、selector/valve 行为或 UI；不执行真实硬件。
- 实际修改 `FlowDeviceCapacities`、版本化 default/example 配置、容量门禁测试，以及当前 C.3b-4 spec、architecture、project-context。没有修改本机 local config；其未覆盖 `real_supply_policy`，正式配置合并后沿用版本化默认值。
- B/C 各 5000 sccm 的容量边界仅在测试专用 policy 临时放宽 approved maxima 下验证；生产配置保持 `enabled=false` 与 approved maxima=`500/0/0`。通用显式 `None` 容量仍 fail closed。
- 当前用户只确认三台“同型号”，没有重新提供逐台型号铭牌/序列号；历史 A `MC-5NLPM-D` 不能据此直接扩展成三台当前身份。A-only 现场身份与压力门禁继续保留。
- 2026-09-16 离线验证：容量/文档卫生定向 `25 passed`，相关 Manual/Flow/Alicat 套件 `155 passed`；完整 pytest `1429 passed, 1 skipped`；Ruff、`git diff --check`、PyInstaller 与 offscreen Mock simulation 均通过。既有 qfluentwidgets deprecation 与 GC ResourceWarning 不影响本次容量门禁。全程未打开 COM6、未启动 Real App、未访问真实 NI/Alicat。

## Review Triage Log

| Finding | Verdict | Route | Evidence |
| --- | --- | --- | --- |
| 缺失 B/C 字段默认 5000 会猜测未知设备 | false | reject | 当前项目三台 device capacity 已由用户明确确认；正式配置由版本化 default + local override 合并，default 显式提供三路 5000。未来不同实验台可显式配置 `None`，此能力仍 fail closed。 |
| architecture 未同步新的容量 authority | medium | patch | 长期规则仍写 A+C 容量未知；已同步容量、A+C 约束和 approved limit 区分。 |
| 缺少版本化配置漂移回归 | medium | patch | 已直接解析 default/example 并断言 `enabled=false`、capacity=`5000/5000/5000`、approved maxima=`500/0/0`。 |
| B/C 5000 容量边界未测 | medium | patch | 已在测试专用放宽批准上限 policy 中分别测 5000 接受、5000.001 拒绝；生产批准上限未变。 |
| 型号/来源可追溯性不足 | medium | patch | 已注明 2026-09-15 操作者人工确认“同型号、各 5000”；逐台当前铭牌/序列号仍未知，拒绝由历史 A 型号推断 B/C 当前型号。 |
| 单独执行规格仍在活动目录且状态未收束 | medium | patch | 本记录完成后归档；C.3b-4 总规格因真实非零 HIL 尚未执行而保持活动 `in-progress`。 |
