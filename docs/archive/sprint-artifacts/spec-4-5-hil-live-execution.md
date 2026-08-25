---
title: 'Story 4.5：真实硬件 HIL 受控执行入口'
type: 'feature'
created: '2026-08-18'
status: 'done'
baseline_commit: '94456c4837c60f77163cfdc8928e2c960f2390f8'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/implementation-artifacts/spec-4-5-hil-offline-preparation.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Story 4.5 已有生产门禁和 mock 演练，但缺少能约束真实 NI/Alicat 写入并执行生产停止契约的 HIL 入口；旧 benchmark 会自动恢复流量，不适用。

**Approach:** 新增单场景 runner：先生成固定候选、逐项写入和收尾的 manifest；操作者一次确认哈希后，broker 才逐条核销并保存证据。

## Boundaries & Constraints

**Always:** 干净候选、每场 fresh process、Air、无气味/受试者；normal：只读确认 A 为已记录的 `MC-5NLPM-D`、满量程 `5000 sccm` 后，才允许 A=`2500 sccm`、B=C=`0`，selector LOW=补偿/HIGH=气味，阀 1–20 LOW=关，仅代表阀 2 可开；A 达稳定阈值后保持 `20 秒`再 stop，操作者只在阀 2 出口前方 2–5 cm 感受且不得接触或封堵管口；有效 A=0 receipt 严格先于 selector LOW；电子回执与人工观察分开；异常仍收尾并交还 owner。

**Ask First:** 新候选 commit、打开 Air、执行某 manifest 哈希的 live 场景；`2500 sccm` 是本次诊断 manifest 的显式例外，不修改生产默认 `1500 sccm` 上限；改变气体、诊断流量、极性/映射或写入集须重确认。

**Never:** 不调用旧 benchmark 生命周期或提供 live `--all`；不写未授权项；ack 不冒充机械证据；selector 不确定不重试；不跨线程接管 DO；不 push。

## I/O & Edge-Case Matrix

| 场景 | 初始状态 | 预期 | 失败结果 |
|---|---|---|---|
| normal | A/B/C=0；确认 A 满量程 5000；A=0 时置 HIGH、开阀 2，再设 A=2500 | 稳定后观察 20 秒，再执行：fence → A=0 receipt → selector LOW → 1–20 LOW → A/B/C=0 → handoff | 型号/满量程不符则零写入；其余完整才 `COMPLETED` |
| A fail/timeout/stale/late | A/B/C 实测 0；receipt 边界注入 | selector API/HAL 写入均为 0；其余收尾继续 | `RECOVERY_REQUIRED` |
| selector stale/late/uncertain | 有效 A=0 receipt | 最多一次 LOW 请求；软件路线 UNKNOWN；继续收尾 | 不重试 |
| handoff/runner 异常 | 安全起始态 | 拒绝越权；`finally` 只做预授权收尾 | 不误报；硬崩溃提示关 Air |

</frozen-after-approval>

## Code Map

- `scripts/hil_story45_live.py` -- manifest、生产 owner 生命周期、证据。
- `app/services/authorized_hal.py` -- 默认拒绝的顺序写入 broker。
- `tests/test_hil_story45_live.py` -- broker、偏序、故障和收尾测试。
- `docs/sprint-artifacts/evidence/story-4-5-hil-runbook.md` -- 现场单。

## Tasks & Acceptance

**Execution:**
- [x] `app/services/authorized_hal.py` -- 仅代理 manifest 完全匹配的写入并审计。
- [x] `scripts/hil_story45_live.py` -- 实现零硬件 `plan`、只读 `preflight`、单场景 `run` 和生产 owners。
- [x] `tests/test_hil_story45_live.py` -- 覆盖授权、normal、receipt 故障、越权、收尾。
- [x] `docs/sprint-artifacts/evidence/story-4-5-hil-runbook.md` -- 更新确认、停止及恢复步骤。

**Acceptance Criteria:**
- Given 候选/场景/哈希不匹配，when run，then 打开硬件前退出且零写入。
- Given normal 已授权，when stop，then A=0 receipt 早于 selector LOW，终态 A/B/C=0、1–20 LOW、selector=补偿、owners 已交还。
- Given A receipt 无效，when 收尾，then selector API/HAL 零调用且进入恢复态。
- Given selector receipt 无效或出现越权写入，when 收尾，then 不重试、保持 UNKNOWN/记录违规，只执行已授权收尾。

## Spec Change Log

- 2026-08-18：两层只读复审后加固有效配置绑定、Alicat 帧/单位/气体校验、连续流量边界、无条件收尾、owner 证据和人工观察闭环；冻结意图未改。

## Verification

**Commands:**
- `python -m pytest -q tests/test_hil_story45_live.py` -- 离线矩阵与硬件隔离通过。
- `python -m pytest -q && python -m ruff check .` -- 回归与 lint 通过。
- `python scripts/hil_story45_live.py plan --scenario normal ...` -- 生成 manifest，零硬件访问。

**Actual results (2026-08-18，全部离线 fake/mock)：**
- 新增测试 `36 passed`；9 个场景均通过独立 oracle，只有带“持续气流”观察的 normal 可正式通过，所有 fault 均返回非零。
- 全仓 `820 passed in 28.49s`。
- `python -m ruff check .`、`git diff --check` 通过。
- 干净子进程加载 `plan` 后 `nidaqmx=False`、`serial=False`；只读 preflight 测试仅发送 `a/b/c`、`a/b/c??D*`、`a??M*`、`aFPF 5` 查询帧。
- 未枚举、打开或写入真实 NI/Alicat；尚未创建新候选 commit，尚未运行 live。

## Suggested Review Order

**现场入口与安全生命周期**

- 单场景入口先锁定配置和授权，再读取硬件并强制收尾。
  [`hil_story45_live.py:1249`](../../../scripts/hil_story45_live.py#L1249)

- 生产 shutdown、故障注入、证据和 owner 交还在一处闭环。
  [`hil_story45_live.py:788`](../../../scripts/hil_story45_live.py#L788)

- 20 秒期间持续执行有限值和 2250–2750 sccm 边界。
  [`hil_story45_live.py:700`](../../../scripts/hil_story45_live.py#L700)

**授权与设备身份**

- Manifest 同时绑定候选、有效配置、逐项写入和可选兜底。
  [`hil_story45_live.py:245`](../../../scripts/hil_story45_live.py#L245)

- Broker 只暴露明确审计的方法并拒绝所有未知 HAL 能力。
  [`authorized_hal.py:43`](../../../app/services/authorized_hal.py#L43)

- Preflight 核验 NI、数据帧、Air、状态码、型号和满量程。
  [`hil_story45_live.py:392`](../../../scripts/hil_story45_live.py#L392)

**证据与人工闭环**

- 安全停止后离线记录阀 2 观察，禁止人工覆盖自动失败。
  [`hil_story45_live.py:1313`](../../../scripts/hil_story45_live.py#L1313)

- 现场单将一次哈希授权、关气动作和观察口径讲清楚。
  [`story-4-5-hil-runbook.md:85`](../../../docs/sprint-artifacts/evidence/story-4-5-hil-runbook.md#L85)

**测试护栏**

- 九场景生产停止矩阵区分故障 oracle 与硬件成功。
  [`test_hil_story45_live.py:342`](../../../tests/test_hil_story45_live.py#L342)

- 越界、非有限及掉流都必须中止并完成安全收尾。
  [`test_hil_story45_live.py:501`](../../../tests/test_hil_story45_live.py#L501)
