---
title: '收紧产品数值输入校验与单位显示'
type: 'bugfix'
created: '2026-09-10'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
baseline_commit: '1d7c1905032a6438d82d93f5c07022e5d0741f97'
context:
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/ux-design.md'
  - '{project-root}/docs/archive/sprint-artifacts/spec-pre-hil-usability.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 共享 `ProductNumericSpinBox` 会把明确超出业务范围的正数保留为中间编辑文本，并允许用户键入或粘贴超过一位的小数，导致提交回退成为正常路径、用户误判保存精度，以及长非法文本挤压合法 suffix。

**Approach:** 让 Manual A/B/C/持续时间与 Settings 验证流量/时长统一依赖 Qt validator contract：产品手动输入最多一位小数，明确越界、负值、非有限值及超精度键入或粘贴立即拒绝；移除普通路径上的越界提交信号流程，并按合法业务最大值保证 suffix 与箭头完整显示。保留合法一位小数和 non-grid 直接输入、既定 directional snap、整数去 `.0`、backend 最终校验，以及基于精确 duration/deadline 的向上取整倒计时；不改变 Worker/HAL、硬件安全语义、verification/availability/deadline 或内部时序精度。

</frozen-after-approval>

## Implementation Notes

- `app/views/spin_box_rules.py` 将产品精度收敛为 1 位小数；保留 Qt 的空文本/小数分隔符等 `Intermediate`，仅对已可解析的越界或非有限值返回 `Invalid`。业务 range 上下界向内对齐到 0.1，避免 Qt 四舍五入产生超出领域上限的可输入值。删除回车/失焦回退与 `outOfRangeCommitAttempted`，不改模型校验。
- 共享控件依当前 range、locale、prefix/suffix 和箭头区计算紧凑尺寸；Manual 流量区改为两列，使当前最小正常窗口下六个目标控件的合法最大值、一位小数、完整 suffix 和箭头同时可见。
- 回归覆盖越界键入/粘贴、负值、非有限值、第二位小数、合法 integer/decimal/non-grid、directional snap、六控件 suffix 布局、backend defensive validation，以及 5.8 秒的精确 intent 与首帧向上取整。
- Blind Hunter 补丁：按解析后数值拒绝 `1e-2` 等绕过 0.1 精度的科学计数输入；Manual/Settings 旧版多位精度 draft 在进入产品控件时归一为可见一位值，避免执行或保存不可见精度；size hint 改用 Qt style 的 spin-box edit-field metrics，不再依赖当前几何。

## Review Triage Log

| 来源 | verdict / route | 核实证据 |
|---|---|---|
| blind-hunter 1 | medium / patch | Qt 探针证实 `1e-2` 在原实现可提交为 `0.01` 却显示 `0`；现按解析后数值的 0.1 可表示性返回 `Invalid`。 |
| blind-hunter 2 | false / reject | 单独 `-` 尚未形成 negative numeric value，作为 Qt 合理 `Intermediate` 与要求一致；`-1` 及负值粘贴均已证实为 `Invalid`。 |
| blind-hunter 3 | medium / patch | Manual 探针证实旧 `duration_s=5.84` 会显示 5.8 但执行 5.84；现在 render 时将 view draft 归一为控件可见值，执行 intent 为精确 5.8 s。 |
| blind-hunter 4 | medium / patch | 任一 Manual 数值变化会回写四个控件；现 render 后的整个 view draft 已与四个可见值一致，不再在后续编辑中静默丢失隐藏精度。 |
| blind-hunter 5 | medium / patch | Settings 原 `_verification_config_values` 保留多位值而控件只显示一位；现 verification draft/原始字典在 render 后同步为可见值，安全上限仍保留领域精度。 |
| blind-hunter 6 | medium / patch | 原 size hint 用当前 widget/lineEdit 几何差，在布局前可变；现用固定 probe rect 与 `SC_SpinBoxEditField` style metrics 求 chrome 宽度。 |
| blind-hunter 7 | low / patch | 原测试仅给独立 View 设宽；现在真实 `MainWindow` 最小 1180×720 容器中校验 view minimum hint 及六控件文本像素宽度。 |
| blind-hunter 8 | medium / patch | 5.8 s 原测试直接注入 draft；现从控件键盘输入 5.8、回车提交、生成 5.8 s intent，再验证首帧显示 6 秒，并增加科学计数/旧多位值回归。 |
