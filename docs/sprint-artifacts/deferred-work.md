# Deferred Work

- source_spec: `docs/sprint-artifacts/spec-manual-protocol-domain-boundary-docs-alignment.md`
  summary: 将 readiness 分支对 interlock generation 与 snapshot 的两次读取收敛为一次原子快照读取。
  evidence: 当前连续调用 `interlock.read()[0]` 与 `interlock.read()[1]`，并发更新可能组合不同代际的 generation 与 snapshot；该行为在本轮前已存在。

- source_spec: `docs/sprint-artifacts/spec-manual-protocol-domain-boundary-docs-alignment.md`
  summary: 统一检查 `scripts/run-ci.ps1` 中所有 Python 原生进程的非零退出码传播。
  evidence: 现有 lint/test 路径只直接调用 Python，Windows PowerShell 的 `$ErrorActionPreference` 不保证把原生进程非零退出转换为终止错误；该行为在本轮前已存在。
