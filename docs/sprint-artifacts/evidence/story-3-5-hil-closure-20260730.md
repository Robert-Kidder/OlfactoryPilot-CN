# Story 3.5 真实 Windows/NI HIL Closure 证据

## Gate 结论

- HIL candidate：`db5271352eb7bf38f38eb3f56657d18d5ecbda45`（执行时与 Git `HEAD` 精确一致，index/worktree clean）。
- 正式入口：`scripts/hil_actuation_benchmark.py --story-3-5-recording`。
- HIL Gate：通过。
- 软件 Gate：`python -m pytest -q` 为 `636 passed in 16.93s`；`python -m ruff check .` 为 `All checks passed!`；相对 Story baseline `e401a319d1da93302bcc8908fc9ed7d161b3da08` 的独立临时 index 纳入 36 个 diff entries 后，`git diff --cached --check` 返回 0，真实 index 前后均为 0 entries 且未改变。
- 验收限制：未连接受试者；AI0 性能刺激和 LOW_FLOW 输入使用 production ingress 的软件注入；未使用外部 AI0/AI6 传感器刺激；未执行填盘、拔线、短接等破坏性故障测试；`daqmx_write_ack` 不代表机械阀物理完成。

## 硬件与执行环境

- 主机：`Shark-Yang-PC`，`Windows-10-10.0.26200-SP0`。
- Python：`3.11.15`，64-bit Anaconda build。
- NI：`Dev1` = USB-6001，序列号 `34887710`；`Dev2` = USB-6001，序列号 `34887797`。
- DO resource groups：`Dev1/port0`、`Dev1/port1`、`Dev2/port0`、`Dev2/port1`；配置主阀为 `Dev2/P1.0`。
- 串口/MFC：`COM6` 自检通过；正式 run 流量 `1500.0 sccm`，clean/inert、odor-free。
- 正式参数：代表阀 `1/9/13`，`duration_ms=100`，`inter_trial_ms=250`，`cycles=200`，ProtocolView 可见，session recording、structured logging 与 diagnostic-only latency trace 启用。
- 成功 preflight：`2026-07-30T20:00:36.907+08:00` 至 `2026-07-30T20:00:37.641+08:00`，证据目录 `logs/benchmarks/story-3-4-20260730-200036-live/`。
- 正式 Story 3.5 run：`2026-07-30T20:01:24.100+08:00` 至 `2026-07-30T20:03:04.526+08:00`，总计 `100.4255 s`；正常动作 benchmark 为 `75.8844 s`。

## Receipt 与延迟 Gate

| 指标 | open | close | combined |
|---|---:|---:|---:|
| 正常 success | 200/200 | 200/200 | 400/400 |
| failures | 0 | 0 | 0 |
| aggregate p95 (ms) | 12.8520 | 10.4800 | 12.7632 |
| 最大 rolling p95 (ms) | 12.9486 | 10.8832 | 12.8520 |
| final-last-100 p95 (ms) | 12.9205 | 10.4124 | 12.8432 |

aggregate、每一个具备最小样本数的 rolling p95、final-last-100 p95 均严格 `<20 ms`；正常动作样本数完整且无 action failure。

## 安全关闭 Gate

| 场景 | 成功关闭/配置目标 | 缺失 | 失败 | 结果 |
|---|---:|---:|---:|---|
| 初始安全全关 | 21/21 | 0 | 0 | 通过 |
| stop | 21/21 | 0 | 0 | 通过 |
| LOW_FLOW | 21/21 | 0 | 0 | 通过 |
| severe（注入 open jitter `47.1072 ms`） | 21/21 | 0 | 0 | latch 与全关通过 |
| shutdown | 21/21 | 0 | 0 | `success`，valves closed、heaters off |

## Session bundle 独立复核

复核同时采用两条路径：

1. 以真实配置 `master_valve_line=Dev2/P1.0` 调用生产 `SessionFileService.validate_complete_bundle()`，5/5 返回 `complete=True`。
2. 独立重算每个 `.raw/.log` 的 SHA-256、实际 byte、raw 数据行数、log JSONL 行数，并检查 `session_sequence=1..last_session_sequence` 连续、首尾事件为 `session_started/session_closed`、hardware/actuation/controller 三个 producer fence 存在、`dropped_count=0`；全部与 manifest/`session_closed` 一致。

| bundle（相对 `session-output/`） | raw SHA-256 | log SHA-256 | raw count | log count / last sequence | producer fences H/A/C | dropped |
|---|---|---|---:|---:|---|---:|
| `20260730-200127-513_HIL-NO-SUBJECT_Story-3.5-Windows-NI` | `680d10fcbd165a2cc041388a040ad366efa596402c97ef91dadb248755e80477` | `05ebba43c31f4a7447aad31c59676a35d90ca4fd0e67505436efa7f454792442` | 7604 | 2049 / 2049 | 7596 / 2045 / 2 | 0 |
| `20260730-200248-409_HIL-NO-SUBJECT_Story-3.5-Windows-NI` | `0b1557a4931393259f6e19c7d92010ebc40b7d5daaf8362fbd884ccd29c6b120` | `de5606e53c681a8d13766eda6ec989c1c2c6d4b44958b26841c45dd6aa3260cb` | 6 | 55 / 55 | 6 / 51 / 2 | 0 |
| `20260730-200253-535_HIL-NO-SUBJECT_Story-3.5-Windows-NI` | `3beae45d3a902493f0e5e964439d61f5f4e53ca6932cd0d363b7f4b41d1fba29` | `c4c592b18362ebba2cd5903b7a08e6cb031f0c1b1260dc972f28de049c218726` | 181 | 121 / 121 | 181 / 117 / 2 | 0 |
| `20260730-200258-674_HIL-NO-SUBJECT_Story-3.5-Windows-NI` | `c02183ded2ee8780474f541ece889d83cbd0f37cafde289f051fc585f6978397` | `86b72d8f185e74e4a7f53511c98acfa6b977bfc593011d3581d2f51fa0468231` | 8 | 53 / 53 | 8 / 49 / 2 | 0 |
| `20260730-200303-798_HIL-NO-SUBJECT_Story-3.5-Windows-NI` | `3a0cdc211cc8556471bf458fa4f7f7896fbe278375e77956e781b11c133fcc47` | `c4615ecc6e1aef7cbb57951807a645f73445a70fbfbb4feeb5b248f0a44e1750` | 4 | 54 / 54 | 4 / 50 / 2 | 0 |

## 原始证据位置

- 正式 run 根目录：`logs/benchmarks/story-3-5-20260730-200124-live/`。
- runner 摘要与环境：`metadata.json`、`summary.json`。
- 原始动作回执：`receipts.jsonl`、`receipts.csv`。
- 调度诊断：`latency-trace.jsonl`。
- shutdown：`shutdown-event.json`。
- 五个完整 session bundle：`session-output/*/` 下的 `.raw`、`.log`、`manifest.json` 和 ownership marker。

`logs/` 按仓库策略为本地忽略目录；本报告是提交到 Git 的 closure 索引和不可变 hash/count/sequence 绑定，原始证据保留在上述只读位置。
