# Story 4.1 真实清洗 HIL 探索性证据（2026-07-31）

## 声明边界

- 本文记录 dirty worktree 上的探索性 HIL，不是 clean candidate commit 的正式发布 Gate。
- 运行时 HEAD 为 `49fd99ecd0b4247555d071e6fd7e0fff8b3205b9`；成功短停运行记录的 tracked diff SHA-256 为 `61c8af10a058f018f414b600889717e83f8f04d9ea44be437943187e06b03378`。
- `daqmx_write_ack` 只证明 NI 写入调用返回成功，不证明机械阀物理动作完成。
- Alicat setpoint/readback 与人工手感是不同证据；人工手感不明确时不得宣称气路标签映射通过。
- `LOW_FLOW`、disconnect 与 stale receipt 的自动化故障场景为软件注入；不得扩称为外部传感器或破坏性硬件故障证据。

## 环境与授权

| 项目 | 记录 |
|---|---|
| 主机 | `Shark-Yang-PC` |
| 系统 | Windows 10 build 26200 |
| Python | 3.11.15 64-bit |
| NI | `Dev1 = USB-6001/SN 34887710`；`Dev2 = USB-6001/SN 34887797` |
| 串口 | `COM6 = ATEN USB to Serial Bridge` |
| 介质/现场 | Air；入口约 5 bar；无气味材料或容器；无受试者 |
| 配方 | A=`1500 ml/min`；B/C=`0`；软件通道 2 / 机外标签 2 |
| 授权 | 用户明确授权短暂打开通道 2，随后 21-target 全关并将 A/B/C 清零；后续明确授权将观察时间延长至 5 s |

## 安全基线

- `logs/benchmarks/story-3-4-20260731-193421-live/summary.json`：首次 close-only 为 21/21 成功。
- 首次异常 HIL 后，`logs/benchmarks/story-3-4-20260731-195327-live/summary.json`：再次 close-only 为 21/21 成功，无 missing/failed target。
- 异常 HIL 后只读核对 Alicat：A/B/C setpoint 均为 `0.0`，A mass flow 为 `0.0 sccm`。

## 首次失败运行与修复

`logs/benchmarks/story-4-1-20260731-195058-stop-live/summary.json` 在进入 `RUNNING` 前被 `DATA_STALE` 阻断，没有通道 2 open 证据。失败运行暴露两项缺陷：

1. B/C/A setpoint 串行写入和验证期间，旧 flow sample 可暂时超过 1 s stale 窗口；master 尚关闭时也会立即中止准备态。
2. 已在 `STOPPING` 时，每次重复 unsafe readiness 都重新提交 21-target 全关并重置 close progress，导致 owner 无法交接。

修复后：

- 只有在 initial-close/flow-start/flow-wait-safe 且 master 未开时，才允许等待暂态 `LOW_FLOW`/`DATA_STALE`。
- setpoint 成功后最长等待 5 s；只有实际 flow sample 恢复 `SAFE` 且 unsafe latch 清除后才能 master open。
- 5 s 内未恢复则 fail closed。
- `STOPPING` 幂等；重复 unsafe 更新不再重建全关集合。late successful open 只追加对应目标的 safety close。
- 对应确定性测试与 simulation stop/LOW_FLOW/disconnect smoke 均通过。

## 成功短停运行

| Run | 通道 2 实际 open→close | 终态 | 全关/清零 | Bundle |
|---|---:|---|---|---|
| `story-4-1-20260731-195909-stop-live` | 513.0346 ms | `completed/aborted` | 21/21；A/B/C=`0/0/0`；无 possibly-open | `maintenance-v1` 完整；56 events；44 receipts；log SHA-256 `e7d29d07fbf396e7c652ea8a151405b2e3758d19c716373f3848df795daf8690` |
| `story-4-1-20260731-195936-stop-live` | 501.1341 ms | `completed/aborted` | 21/21；A/B/C=`0/0/0`；无 possibly-open | `maintenance-v1` 完整；56 events；44 receipts；log SHA-256 `c98aa7b101d06527ff93ea149c825b0c7b822c513ec2b67d047bec211e31b284` |
| `story-4-1-20260731-200050-stop-live` | 5009.3851 ms | `completed/aborted` | 21/21；A/B/C=`0/0/0`；无 possibly-open | `maintenance-v1` 完整；56 events；44 receipts；log SHA-256 `b199722cd22e807c854cc0c5993063a69b12d4708475dc6da4b763e723d5740b` |

三次成功 run 的 producer fences 均为 `controller=2`、`flow=2`、`actuation=50`，bundle validator 均通过。日志顺序均为：

1. initial 21-target close receipts；
2. flow `cleaning` 回执 A/B/C=`1500/0/0`；
3. `flow_wait_safe` → `flow_ready`；
4. master open receipt；
5. 软件通道 2（`Dev1/P0.1`）open receipt；
6. stop；
7. 21-target close receipts；
8. flow `zero` 回执 A/B/C=`0/0/0`；
9. producer fences 与 terminal publish。

## 人工映射观察

- 约 513 ms 第一次现场观察：用户没感受到标签 2 气流。
- 约 501 ms 第二次现场观察：用户没感受到标签 2 气流。
- 约 5.009 s 第三次现场观察：用户仍未感觉到标签 2 气流。
- 结论：软件通道 2 / 机外标签 2 的物理映射验证失败。虽然 DAQmx 写入、Alicat setpoint/flow 流程及电子回执通过，但这些证据不能证明机械阀动作或指定出口实际出气。
- 后续必须在保持 Air、无气味材料/容器、无受试者、`<=1500 ml/min` 的前提下定位实际出气出口，并检查机外标签、气路连接、堵塞及主阀/通道阀机械动作；在此之前不得把该映射或完整清洗 HIL 标记为通过。

## 尚未完成的正式 Gate

- clean candidate commit 上的正式 HIL 重跑。
- 候选配方 `10 s × 3` 全程运行。
- 真实 LOW_FLOW/disconnect 安全场景与本次代码对应的 200 open + 200 close 性能 Gate。
- 逐路软件通道/机外标签映射；手感不明确的通道需要不引入污染、背压或超限风险的外部检测手段。
