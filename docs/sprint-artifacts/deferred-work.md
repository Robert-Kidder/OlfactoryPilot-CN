# Deferred Work

- source_spec: `docs/archive/sprint-artifacts/spec-manual-protocol-domain-boundary-docs-alignment.md`
  summary: 将 readiness 分支对 interlock generation 与 snapshot 的两次读取收敛为一次原子快照读取。
  evidence: 当前连续调用 `interlock.read()[0]` 与 `interlock.read()[1]`，并发更新可能组合不同代际的 generation 与 snapshot；该行为在本轮前已存在。
