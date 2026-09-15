# Deferred Work

- source_spec: `docs/archive/sprint-artifacts/spec-manual-protocol-domain-boundary-docs-alignment.md`
  summary: 将 readiness 分支对 interlock generation 与 snapshot 的两次读取收敛为一次原子快照读取。
  evidence: 当前连续调用 `interlock.read()[0]` 与 `interlock.read()[1]`，并发更新可能组合不同代际的 generation 与 snapshot；该行为在本轮前已存在。

- source_spec: `docs/archive/sprint-artifacts/spec-repository-hygiene-follow-up.md`
  summary: 让 UI capture 的三张端口设置截图分别呈现与文件名一致的不同状态。
  evidence: `settings-port-configuration.png`、`settings-port-states.png` 与 `settings-port-alias.png` 在既有脚本中连续保存且中间没有状态切换；该缺口早于本轮临时目录迁移。

- source_spec: `docs/archive/sprint-artifacts/spec-repository-hygiene-follow-up.md`
  summary: 为固定文件名的 UI 截图证据集设计整组事务化发布机制。
  evidence: 既有 capture 流程逐张覆盖最终文件，进程在中途失败时可能留下新旧混合集；完整集合级原子替换需要独立的证据版本与发布设计。

- source_spec: `docs/archive/spec-c3b-serial-transaction-framing.md`
  summary: 明确 Alicat setpoint tolerance 的单位与安全零值上限，并让 SafeStop A-zero receipt 发布真实 readback 而非请求目标。
  evidence: 既有 `alicat_setpoint_tolerance=0.05` 以 device unit 比较，在 1000 倍 readback scale 下理论上可接受约 50 sccm 偏差；`AZeroReceipt.confirmed_a` 又读取请求目标。该 flow/receipt model 早于本轮 framing 修复，需单独安全规格与实机阈值证据。

- source_spec: `docs/archive/spec-c3b-serial-transaction-framing.md`
  summary: 用当前三台 Alicat 的只读 Poll/VE/LSS latency evidence 收敛 frame deadline 与 initial resynchronization quiet 边界。
  evidence: Alicat ASCII 无 request sequence ID；若同 ID/同类型 stale frame 晚于 provisional bounded quiet 才到达，内容校验无法证明归属。需要下一轮只读 HIL 记录 latency 后才能验证或调整当前 `0.2 s` frame deadline及派生 quiet window。
