---
title: 'HIL 前数值输入与倒计时交互收敛'
type: 'bugfix'
created: '2026-09-10'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '9f468b3941740b06b02a68341fe18231f3ff37c3'
context:
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/ux-design.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Manual 与 Settings 的数值控件分别配置精度、范围和 Qt 相对步进，导致整数显示 `.0`、非档位值按自身偏移；Manual 倒计时仍显示小数。

**Approach:** 在已从最新 main 创建的短分支上实现纯 directional snap helper 和共享 Product Numeric SpinBox，统一六个目标控件及两个倒计时 presentation，并用现有 availability 回归证明安全合同未变。

## Boundaries & Constraints

**Always:** origin=0；流量 interval=100 ml/min，秒级时间 interval=5 秒。不在档位时第一步取该方向严格相邻档位，多步继续移动 interval；仅 `stepBy()` 吸附，直接输入/回车/失焦/保存保留合法小数。用小 tolerance 处理浮点误差。各 View 注入领域 range：Manual 使用 snapshot ceiling/range；verification flow 最大值取现场批准与 profile A 上限较小者，duration 为 1–60 秒；wrapping 禁用。显示去 `.0`/trailing zero 而不降精度；单位用不可编辑 suffix，model 只收 numeric value。剩余时间向上取整，deadline 到达沿现有完成/收口状态，不长期显示运行中的 0。Production 仅 `enabled+有效 PHYSICAL_VERIFIED` 可用；simulation 另接受有效 MOCK，发布后当前进程立即刷新。

**Never:** 不连接真实硬件、不进入 C.3b、不运行 clean-clone/HIL。不改 Worker/HAL ownership、SafeStopPlan、lease/receipt/epoch、HardwareProfile transaction、mapping fingerprint、verification evidence、Mock/Physical 隔离、安全偏序、C.3a contract、Auto/TXT/protocol/DAQ/deadline/timestamp 精度。Git 只用 ff-only，禁止 force/rebase/reset；保留含 main 未有提交的历史分支。

## I/O & Edge-Case Matrix

| 场景 | 输入 | 结果 |
|---|---|---|
| 单步 | `10.5 ↑/↓`；`1225 ↑/↓` | `15/10`；`1300/1200` |
| 多步 | `10.5 ±2`；`1225 ±2` | `20/5`；`1400/1100` |
| 直接输入 | `5.5`；`1225.5` | 原值及有效小数保留 |
| 边界 | minimum↓；maximum↑ | 停在合法边界，不负数、不 wrap |
| 倒计时 | 4.2；4.0；0.2 秒 | 5；4；1 秒 |
| 可用性 | PENDING/MOCK/PHYSICAL | production 仅 PHYSICAL；simulation 接受 MOCK/PHYSICAL |

</frozen-after-approval>

## Code Map

- `app/views/spin_box_rules.py` -- 扩展现有 100/5 常量为纯 snap/formatter 和共享 `ProductNumericSpinBox`；保留旧 helper，避免扩散到非本轮页面。
- `app/views/manual_experiment_view.py` -- A/B/C、duration 接入共享控件；范围仍来自 snapshot；只改 countdown presentation。
- `app/views/hardware_settings_view.py` -- 两个验证参数接入共享控件与真实领域范围；保留 float draft/非法配置提示。
- `app/models/hardware_verification.py` -- 已有 verification 范围和 ceil；不改合同。
- `app/models/hardware_profile.py`, `app/controllers/main_controller.py` -- availability/即时刷新 authority；只测试。
- `tests/test_spin_box_rules.py` 及现有 Manual/Settings/profile/integration tests -- 覆盖 helper、widget、倒计时和 availability。
- `docs/ux-design.md`, `docs/project-context.md` -- 更新方向吸附规则并补一次 clean-clone 触发边界。

## Tasks & Acceptance

**Execution:**
- [x] `app/views/spin_box_rules.py`, `tests/test_spin_box_rules.py` -- 实现并纯测格式、单/多步、epsilon、bounds、suffix、direct input。
- [x] Manual/Settings View 与测试 -- 接入六个共享控件，复用业务 range，修正 Manual/Verification 整数倒计时回归。
- [x] profile/integration tests -- 固化 PENDING/MOCK/PHYSICAL 隔离和无重启刷新。
- [x] 两份长期文档 -- 记录新规则与 clean-clone 使用边界。
- [x] 全量验证 -- 完成 Ruff、定向/完整 pytest、PyInstaller、MockHAL simulation smoke 与 diff/temp hygiene；review 通过后再执行获批的中文提交与 ff-only Git 收束。

**Acceptance Criteria:**
- Given 六个目标控件，when 检查类型、显示、suffix、range、wrapping、直接输入与 Up/Down，then 共用同一实现并满足矩阵。
- Given 完成修改，when 跑 Ruff、定向/完整 pytest、PyInstaller、simulation smoke、diff/temp hygiene，then 全部成功且无 clean-clone/HIL/外部临时目录新增。
- Given origin/main 无冲突变化，when 收束分支，then origin/main ff 到中文提交，只保留含独立提交的历史分支。

## Implementation Notes

- `ProductNumericSpinBox` 将 Qt 可配置小数位设为其上限 323，避免控件按显示位数改写 double 值或 range endpoint；`textFromValue()` 使用 float 的最短 round-trip 十进制表示并仅移除无意义末尾零，Qt suffix 与 `value()` 保持单位/数值分离，逗号小数 locale 也保持显示/解析一致。
- `directional_snap_value()` 用 `math.isclose(abs_tol=1e-9)` 判断档位；`stepBy()` 先解释直接输入，再按方向及多步移动并 clamp 到各控件现有 range。
- Manual 使用原 snapshot 的 A/B/C 与 duration 范围；Settings 使用 `min(max_approved_flow_sccm, max_sample_a_sccm)` 和 1–60 秒，不改领域模型。
- Manual 到期显示“正在完成…”，Verification 到期显示“正在安全收口”；真实 monotonic deadline 与 Controller/Worker 收口未改。
- availability 实现未修改；新增 PENDING/MOCK/PHYSICAL 与 simulation 当前进程刷新回归。
- Review 补丁修复 locale 显示/解析不一致、越界逐字输入被静默截成另一合法值、固定 UI 下界与六位精度量化；补充真实失焦、View 接线、小 ceiling 与过期确认文案回归。
- 2026-09-10 最终复验：定向 204 passed；完整 1263 passed、1 skipped、0 failures；Ruff、PyInstaller、MockHAL offscreen simulation smoke、`git diff --check` 通过。clean-clone 与真实 HIL 均未运行。

## Spec Change Log

## Review Triage Log

| 来源 | verdict / route | 核实证据 |
|---|---|---|
| blind-hunter 1 | medium / patch | Qt 探针复现 German locale 下显示 `12.5`、默认解析为 `125`，再 step 得到 `130`；formatter 与 parser locale 不一致。 |
| blind-hunter 2 | high / patch | Qt 探针复现 max=1500、原值1400时逐字输入2000只留下200并提交200；会把无效意图静默变成另一合法流量。 |
| blind-hunter 3 | medium / patch | `1e-6` 不是模型下界；模型接受任意 finite `>0`，更小合法 profile ceiling 会使控件 range 无法表达。 |
| blind-hunter 4 | medium / patch | `setDecimals(6)` 实测把 `1.23456789` 改为 `1.234568`；编辑其它字段时可能把未编辑 draft 精度写回。 |
| blind-hunter 5 | medium / patch | QDoubleSpinBox 会按 decimals 舍入 range endpoint；六位精度可把合法 ceiling 向上舍入，和领域上限产生可见偏差。 |
| blind-hunter 6 | false / reject | `project-context.md` 已明确当前正式 runtime 仅 Manual+Settings；列出的 cleaning/pretest/calibration 不是本轮普通产品页面，旧 helper 也被刻意保留以免扩散。 |
| blind-hunter 7 | medium / patch | 原真实 UI 越界测试被 private method 替换，确会漏过已复现的逐字输入2000→200问题；需恢复用户交互级断言。 |
| blind-hunter 8 | medium / patch | locale 缺测与已复现解析缺陷同根；加入逗号小数 round-trip/step 回归。 |
| blind-hunter 9 | low / patch | frozen intent 明确失焦不吸附；现有 standalone test 仅覆盖 Return，补一次真实焦点转移是直接测试修正。 |
| verification-gap 1 | medium / patch | standalone widget 的小数测试不能证明六个 View 控件到 draft 的接线未降精度；Manual/Settings 现有用例仅输入整数。 |
| verification-gap 2 | medium / patch | 仅断言 minimum>0 会允许未来误收紧到1；应改为无负值的0 UI下界，并验证低正数由模型接受。 |
| verification-gap 3 | low / patch | 已过期 AWAITING_CONFIRMATION 的新文案没有断言，旧“0秒内选择”可无声回归。 |
| edge-case-hunter 1 | low / reject | 只有远超本项目 0..5000/1..60 range 与 UI 可产生 steps 的人工 helper 调用才会溢出；修复需定义冻结意图没有规定的极端算术语义。 |
| edge-case-hunter 2 | low / reject | `decimals` 由组件常量控制且调用者不传入用户值；巨大精度仅可由新的恶意/错误代码直接调用 formatter，当前路径不可达。 |
| edge-case-hunter 3 | medium / patch | 与 blind-hunter 3 同一真实根因；合法 ceiling 小于1e-6时 setRange 会与 stored draft 分离。 |
| edge-case-hunter 4 | false / reject | `pending_port` 是测试私有 helper，当前仅以受控合法 literal 调用；无生产输入路径，错误测试调用也会立即抛 IndexError 而非静默改变产品。 |

## Design Notes

`stepBy(steps)` 先以 `floor/ceil` 和 `math.isclose` 找严格相邻档位，再追加 `abs(steps)-1` 个 interval，最后 clamp 到控件 range。直接编辑不调用 snap。

## Verification

**Commands:**
- `python -m ruff check .`
- `python -m pytest <相关定向文件>`；`python -m pytest`
- `python -m PyInstaller --noconfirm pyinstaller.spec`
- `python -m app.main --simulation` 与定时退出 smoke（MockHAL）
- `git diff --check`；parent/TEMP/`.devtmp` 前后快照
