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
