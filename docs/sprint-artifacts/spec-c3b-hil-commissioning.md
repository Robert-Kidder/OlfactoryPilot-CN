---
title: 'C.3b 实机 HIL commissioning'
type: 'chore'
created: '2026-09-10'
status: 'draft'
route: 'dispatch'
review_loop_iteration: 0
context:
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/architecture.md'
  - '{project-root}/docs/sprint-artifacts/evidence/c-3b-hil-commissioning-checklist.md'
  - '{project-root}/docs/sprint-artifacts/evidence/c-3b-2a-alicat-read-only-poll-2026-09-10.md'
  - '{project-root}/docs/sprint-artifacts/evidence/c-3b-alicat-ve-lss-read-only-2026-09-10.md'
  - '{project-root}/docs/sprint-artifacts/evidence/c-3b-alicat-safe-state-normalization-2026-09-10.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** C.3a 只证明了 fake-real 软件契约，当前八个启用气口仍只有模拟验证状态。真实应用启动/连接还会启动 NI DO task，并在自检通过后写 A/B/C=0，不能视为纯只读连接。

**Approach:** 人工先核对 NI MAX、Windows 串口和 Alicat 面板设置；C.3b-2A 完成 live-data poll，后续只读轮次确认固件和 Setpoint Source；C.3b-2C 在明确授权下逐台清零并将 LSS 从 S 改为 U。获得新的 App Connect/NI 授权后，才可启动真实应用、执行 physical verification、安全停止和留证。本规格在真实 App Connect 获批前保持草稿。

## Boundaries & Constraints

**Always:** 首次只用批准的洁净 Air，无气味材料和受试者；出口畅通、急停可达。以当前 `hardware_profile` revision 50 为 authority，第一轮只验证一个气口，B=C=0；动作前核对目标、流量、最长时间和完整收尾。软件回执与人工出口观察分别留证。

**Never:** 已批准的串口访问只包括不带 `--set` 的 live-data poll、`VE` 和 firmware `>=10v05` 时的无参数 `LSS`；不得启动 real app、创建 NI task、写 NI 输出、写 setpoint、修改 LSS mode、切 selector、开关阀或运行其他 live HIL。不得把模拟/C.3a/Story 4.5 历史结果标成当前真实结果；发现身份、映射、极性或安全初值冲突立即停止。

## I/O & Edge-Case Matrix

| 场景 | 输入 / 状态 | 预期行为 | 异常处理 |
|---|---|---|---|
| 现场前核对 | NI、COM、Alicat 与配置一致 | 形成单口授权，仍不动作 | 任一不一致即停止 |
| C.3b-2A | `COM6 @ 19200`，只轮询 `a,b,c` | 原样保存返回并立即释放串口 | 非零状态只报告，不发送清零命令 |
| VE/LSS 只读查询 | 先查固件；仅 `>=10v05` 查询无参数 LSS | 保存完整原始响应并关闭 COM6 | 不带 mode，不修改 Setpoint Source |
| C.3b-2C | A→B→C 逐台清零，再逐台 S→U | 每次写后立即 query/poll；最终 U/U/U、0/0/0 | 任一门禁失败立即停止，不重试、不继续下一台 |
| 首次验证 | 气口2、1500 ml/min、最长30秒已批准 | 按冻结顺序启动、收口并人工确认 | 失败/超时/出口不符立即停止，不继续下一口 |
| 全局停止 | 活动或状态不确定 | A=0→selector补偿→阀1–20关闭→A/B/C=0→释放资源 | 证据不完整则进入人工恢复 |

</frozen-after-approval>

## Open Questions

- 启动副作用 — 是否批准 real app 创建/启动 NI DO task，并在自检通过后写入及回读 A/B/C=0？不批准则先修改执行入口。
- 首次参数 — 是否批准气口2（控制通道2、`Dev1/P0.1`）以 1500 ml/min、最长30秒验证？其他值须先改配置并复核。

## Code Map

- `app/main.py` — 合并默认与本机配置；`hal_mode=real` 且无 `--simulation` 时创建 RealHAL并启动 workers。
- `app/controllers/main_controller.py` — Connect、自检后 A/B/C 清零、physical verification 与全局停止入口。
- `app/workers/hardware_worker.py`、`app/services/hardware_check_service.py` — 启动即枚举 Dev1/Dev2，并打开/关闭 COM；不验证 Unit ID。
- `app/services/real_hal.py` — 惰性串口和按 port 分组的 NI DO tasks。
- `app/services/shutdown_service.py`、`app/workers/{actuation_worker,flow_worker}.py` — 停止偏序、回执等待、失败恢复。
- `app/services/hardware_profile_store.py` — 当前 profile 是 authority；last-known-good 仅供显式回滚。
- `scripts/hil_*.py`、`scripts/probe_alicat.py` — 历史 Story 工具；含真实访问/写入的模式不得直接用于本轮。

## Tasks & Acceptance

**Execution:**
- [x] `docs/sprint-artifacts/evidence/c-3b-hil-commissioning-checklist.md` — 已记录现场身份、安全条件和 2A 单次只读授权。
- [x] `docs/sprint-artifacts/evidence/c-3b-2a-alicat-read-only-poll-2026-09-10.md` — 已保存 A/B/C 原始返回、解析、异常和 COM 释放依据。
- [x] `docs/sprint-artifacts/evidence/c-3b-alicat-ve-lss-read-only-2026-09-10.md` — 已保存 A/B/C firmware 与 LSS 原始返回；三台均为 `10v14.0-R24`、mode `S`。
- [x] `docs/sprint-artifacts/evidence/c-3b-alicat-safe-state-normalization-2026-09-10.md` — 已逐台清零并逐台改为 U；最终 setpoint/mass flow=`0/0/0`、LSS=`U/U/U`，COM6 已关闭。
- [ ] production physical verification UI — 获批后只验证首个气口并安全收口。
- [ ] `docs/sprint-artifacts/evidence/` — 保存当前运行的动作、回执、流量、时间与人工观察。

**Acceptance Criteria:**
- Given 现场信息未确认，when 请求 C.3b-2，then 不接触真实硬件。
- Given 全部门禁获批，when 首次验证结束或中止，then 只涉及一个气口并留下完整安全终态。
- Given 回执失败、超时或出口不符，when 收尾，then 不写真实可用状态且不继续下一气口。

## Implementation Notes

- NI MAX 的 `0214581E` / `02145875` 是十六进制显示，分别等于历史十进制 `34887710` / `34887797`，不是设备更换证据。
- 2A poll 三个首 ID 均与查询地址大小写不敏感一致，未 timeout；B/C 只确认可寻址和 19200 通信，不确认 Setpoint Source。
- 设备返回按当前项目约定解析为 mass flow=`0.6/0.2/0.2 sccm`、setpoint=`1500/1500/500 sccm`。非零 setpoint 保持原状并阻断后续阶段。
- 后续 `VE` 返回三台均为 `10v14.0-R24`，达到 LSS 的 `10v05` 门槛；无参数 `LSS` 均返回 `S`。这确认 B/C 也使用保存型 Serial/Front Panel source，但不能确定当前非零值的具体来源。
- 2C 每台仅发送一次 `S0.000` 并立即验证，再仅发送一次 `LSS U` 并 query/poll；全部门禁通过，无重试。未做 power cycle，且未触碰 NI/selector/气味阀。

## Spec Change Log

- 2026-09-10：记录现场 NI/Alicat A/安全准备事实及 C.3b-2A 只读串口证据；因 A/B/C 非零 setpoint 与 B/C Setpoint Source 未确认，保持 `draft` 并停止。
- 2026-09-10：完成官方协议支持的 `VE`/无参数 `LSS` 只读查询，确认 A/B/C firmware=`10v14.0-R24`、mode=`S`；B/C Setpoint Source blocker 已解除，非零 setpoint 与 App Connect 授权仍未解决。
- 2026-09-10：完成 C.3b-2C；A/B/C setpoint 从 `1500/1500/500` 逐台清零，LSS 从 `S/S/S` 逐台改为 `U/U/U`。Alicat 安全初值 blocker 已解除；真实 App Connect 与气味阀真实初始关闭确认仍未授权/完成。

## Review Triage Log

## Verification

**Commands:**
- `python -m pytest tests/test_hardware_profile.py tests/test_hardware_profile_store.py tests/test_physical_verification.py -q` — 仅 Mock/临时配置；预期全部通过。
- `python scripts/probe_alicat.py --port COM6 --baud 19200 --ids a,b,c` — 2026-09-10 唯一获准的真实串口只读命令；退出码 0，未传 `--set`。
