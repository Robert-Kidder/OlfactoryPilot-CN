---
title: 'C.3a.1 产品交互契约收敛'
type: 'bugfix'
created: '2026-09-09'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context:
  - '{project-root}/docs/ux-design.md'
  - '{project-root}/docs/project-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 用户手动调节的流量与秒级时间 SpinBox 尚未共享统一步进，Settings 还重复显示启动说明，普通气口详情暴露了危险的 polarity 配置。

**Approach:** 集中定义并复用流量 100 ml/min、秒级时间 5 s 的原生 `setSingleStep` 规则，保持键盘输入值及现有校验/持久化不量化；删除验证参数说明和普通气口详情 polarity，仅在高级线路编辑中保留 polarity。同步 UX 长期规则和回归测试，不修改 physical verification 状态机、Worker/HAL、安全偏序、receipt、lease、evidence contract、协议毫秒精度或内部计时，也不连接真实硬件。

</frozen-after-approval>

## Implementation Notes

- `app/views/manual_experiment_view.py`、`app/views/hardware_settings_view.py`：正式产品目标控件与普通/高级气口编辑界面。
- `app/views/cleaning_view.py`、`app/views/calibration_view.py`、`app/views/pretest_view.py`：仓库中其他现存的用户流量或秒级 SpinBox，应复用同一 helper。
- `tests/test_manual_experiment_view.py`、`tests/test_hardware_settings_view.py`：覆盖 A/B/C、duration、验证参数步进、550 键盘输入不量化，以及普通/高级 polarity 可见性边界。
- `docs/ux-design.md`：记录长期产品交互规则；不新增产品规格文档，本文件仅为 bmad-build 执行工件。
- 验证依次运行 Ruff、相关 UI 定向测试、完整 pytest、`git diff --check`，并尝试 simulation 启动烟测；只做模拟模式检查。
- 实现决定：`app/views/spin_box_rules.py` 只封装原生 `setSingleStep`，不读取、改写或量化当前值；所有现存用户流量/秒级控件复用它。
- 实现结果：普通气口详情不再创建 polarity 文案，`polarity_inputs` 及高级线路编辑的 enable/disable、映射变更和验证失效路径保持原样。
- 验证结果：Ruff 与 `git diff --check` 通过；定向测试 96 passed；全量测试 1168 passed、0 failures。默认 pytest 临时根目录因系统权限不可遍历，按仓库既有 hook 使用 `--basetemp=.tmp/pytest-c3a1-full` 重定向后通过。
- simulation 结果：执行 `python -m app.main --simulation`，应用检测到本机已有实例后按单实例保护正常取消本次启动；未连接或访问真实硬件，也未终止现有实例。

## Review Triage Log

| # | 结论 | 核查与处理 |
|---|---|---|
| 1 | low / patch | UX 规则原文可能被理解为扩展既有小数精度；已限定为控件当前允许的输入精度，仍明确禁止按步进网格量化。 |
| 2 | false | 本项目既有 Settings/Manual 已以 `flow_sccm` 承载显示为 ml/min 的值；100 sccm 与 100 ml/min 在该既有 UI 约定下数值相同，未引入单位转换。 |
| 3 | medium / patch | 已为校准秒控件补充 5 秒步进及键盘输入 7 后步进到 12 的回归。 |
| 4 | medium / patch | 已为清洗流量/秒控件补充步进、非网格键盘值和 candidate 信号值回归。 |
| 5 | medium / patch | 已为旧预测试 A/B/C 与 duration 补充共享步进回归。 |
| 6 | medium / patch | 已补 Settings 时间键盘输入 7、draft 保留 7、步进后 12 的回归。 |
| 7 | low / patch | 已补 Manual 时间非网格输入；A/B/C 均断言相同步进且由同一 factory/helper 构造，无需重复三份交互用例。 |
| 8 | medium / patch | 键盘测试现同时断言 Manual draft 与 Settings draft/candidate authority，不只检查显示值。 |
| 9 | medium / patch | 普通详情文本在其页面范围内断言不存在；测试再进入“线路与设备”，确认高级页面仍显示并按权限启用 polarity。 |
| 10 | false | 执行记录已经明确写明因现有实例触发 guard、未完成第二个 UI 实例启动；将计划措辞进一步收敛为“尝试”。 |
| 11 | low / patch | UX 规则已明确安全前提、危险后果、合法范围、单位及无障碍提示不属于可删除的冗余说明。 |
