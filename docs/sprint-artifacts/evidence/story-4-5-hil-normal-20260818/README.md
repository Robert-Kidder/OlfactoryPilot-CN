# Story 4.5 normal 真实硬件 HIL 证据

执行日期：2026-08-18
结论：`normal` 场景通过，`safe_stop_status=completed`，`verification_passed=true`。运行结束后操作者已关闭上游 Air。

原始来源（归档时）：`%LOCALAPPDATA%\Temp\OlfactoryPilot-HIL\runs\story45-normal-20260818-211027-13556`。本目录保存其哈希核验后的归档副本；11 个 runner payload 视为不可变且未被改写，补充文件另行标注。临时来源是否继续存在不属于证据完整性的前提。

## 固定候选与授权

- Commit：`154e379da400b326162f84097b79270a5f455e0c`
- Tree：`5096759d93f64542a6fa72c7a6d7ced3743d3440`
- 授权 payload digest：`2b2013664f802d4fbda9351e6a902e5ee788c046fe59e0beafd94dcdc793af92`
- 场景参数：洁净 Air；A=`2500 sccm`；B=C=`0`；稳定观察 `20 s`；无气味材料或受试者
- 设备映射：NI Dev1/Dev2；selector=`Dev2/P1.0`；Alicat A/B/C=`a`/`b`/`c`

## 验收结果

- 操作者在停止前的 `odor` 路线阶段、阀 2 出口独立观察到“持续气流”。机械/气路观察保存在 `operator-observation.json`，不以 DAQ 电子 ack 代替，也不证明停止后的 `compensation` 机械位置。
- 时间线断言确认：有效 A=0 写入与匹配 receipt 均先于 selector 安全路线写入；之后气味阀关闭先于终态全流量清零。
- 最终 A/B/C 只读回读均为 setpoint=`0`、mass flow=`0`、gas=`Air`，无状态码。
- selector 最终软件证据为 `compensation`，目标 `Dev2/P1.0`，receipt 无不确定状态。
- 气味阀 1–20 最后成功请求均为 LOW。
- runner 报告的 maintenance、DO、device lease、AI、serial owner handoff 全部完成；这是生产 owner 调用与软件时间线证据，不是独立机械传感器证据。
- 授权违规 `0`，审计错误 `0`，归档前原始证据哈希不匹配 `0`。
- 候选的离线软件门禁为全仓 `820 passed in 28.49s`；命令和结果记录在 [`spec-4-5-hil-live-execution.md`](../../../../_bmad-output/implementation-artifacts/spec-4-5-hil-live-execution.md#actual-results-2026-08-18全部离线-fakemock)，不属于本次机器运行原始 payload。

## 证据边界与字段解释

- `run-manifest.json` 内的授权 payload digest 是对移除 `manifest_sha256` 与 `authorization_token` 后的对象进行 UTF-8 JSON 编码（键排序、无多余空白、保留 Unicode）所得 SHA-256；它绑定授权内容。`hashes.sha256` 中 `run-manifest.json` 的 `ed4f...` 是整个落盘文件的字节哈希，两者用途不同。
- `authorization_cursor=49`、`authorization_total=69` 表示 49 条实际必需写入全部按序核销；剩余 20 条是仅在首次关闭失败时使用的可选 fallback 关闭写入，本次未触发，不是遗漏。
- `a_fault_never_writes_selector=true` 与 `fault_oracle_passed=true` 在 `normal` 场景中是未触发分支的恒真检查；本次没有在真实硬件上注入 fault，不能把这两个字段当作 fault HIL 证据。
- `shutdown-event.json` 的 `airflow=2499.8` 是触发停止时的停止前样本，不是终态流量；终态以 `final-readback.json` 的 A/B/C=`0` 为准。
- `final-state.json` 把操作者观察附在 selector 汇总对象中，但该观察的时间相位是停止前 `odor` 路线。停止后的 `compensation` 结论仅来自有效 receipt、DAQ 电子 ack 与软件状态；若需要机械确认两个出口，必须另开安全、独立授权的映射 HIL。

## 文件说明

| 文件 | 内容 |
|---|---|
| `run-manifest.json` | 固定候选、有效配置、写入授权与候选快照 |
| `preflight.json` | 只读设备身份、量程、Air 与零流量检查 |
| `timeline.jsonl` | 带顺序号和时间戳的跨 owner/HAL 时间线 |
| `commands.jsonl` / `receipts.jsonl` | 已核销命令与 receipt |
| `shutdown-event.json` | 生产全局停止结果 |
| `final-readback.json` / `final-state.json` | A/B/C、selector 与气味阀终态证据 |
| `owner-handoff.json` | maintenance/DO/lease/AI/serial 交还状态 |
| `operator-observation.json` | 操作者独立气流观察 |
| `operator-authorization.md` | 本次单次 manifest 人工授权记录及边界 |
| `site-closure.md` | 运行后上游 Air 人工关闭确认及其证据边界 |
| `archive-envelope.json` | 运行关联信息、原始及人工补充记录哈希 |
| `summary.json` | 场景 oracle、最终汇总与验收结论 |
| `hashes.sha256` | 原始运行证据文件的 SHA-256 清单 |

本目录中的 12 个原始运行文件从仓库外临时证据目录逐字节复制，未改写内容：11 个 evidence payload 加 1 个 `hashes.sha256` 校验清单。该清单覆盖 11 个 payload，不覆盖它自身；后来增加的索引、人工记录和归档 envelope 另行标注，不伪装成 runner 原始输出。
