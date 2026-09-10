---
title: '仓库卫生后续：统一开发临时目录与长期文档'
type: 'chore'
created: '2026-09-09'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '4b277d162a6bffd470c8e201d9d40ff43d184739'
context:
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/ux-design.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** pytest、截图审查与 clean-clone 临时工作区仍会落到仓库父目录或系统 TEMP，且没有可恢复的 ownership；README 也混入易过期状态、内部实现与 BMAD 版本固定。

**Approach:** 以 `/.devtmp/` 为唯一受管开发临时根，用一个 Python helper 统一 lifecycle；pytest、CI、截图和 clean-clone 共用它，并同步长期文档与回归。

## Boundaries & Constraints

**Always:** session 使用 `.devtmp/<purpose>/run-<uuid>/`，marker 记录项目、用途、run、时间、PID 与进程启动 identity。启动时只删 marker 有效且 owner 已失活者；未知或无法判断项报告并保留。pytest basetemp 放在 session 子目录以免 marker 被 pytest 清除；正常退出清 session，仅非递归删除空父目录。长期证据先迁入 `docs/sprint-artifacts/evidence/`。精确忽略 `/.devtmp/`，其他 untracked 仍进入 HIL gate。

**Never:** 不在 repository parent、系统 TEMP 或 `%LOCALAPPDATA%` 建开发工作区；不模糊删除、删除未知目录或提交 `.devtmp`。不削弱 HIL/Git/Qt 与 `MOCK_VERIFIED`/`PHYSICAL_VERIFIED` 隔离；不改 production、Auto/Breath/HIL/safety 语义，不连接真实硬件，不 merge/push。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| pytest | wrapper 或裸运行 | 唯一 repo-local basetemp，结束无残留 | 失败仍清理并保留退出码 |
| interrupted | dead / active / unknown owner | 下次只删可证 dead session | active/unknown 报告并保留 |
| clean clone | tracked-only 验证 | worktree、venv 均在 owned session，不复制 `.devtmp` | 失败清理并返回非零 |
| HIL scan | `.devtmp` 与普通 untracked | 仅排除前者 | 不扩大 ignore |

</frozen-after-approval>

## Code Map

- `scripts/dev_temp.py` -- 唯一 lifecycle authority：marker、process identity、create/claim/stale/current cleanup 与 CLI。
- `scripts/run-ci.ps1`, `scripts/run-clean-clone.ps1`, `scripts/capture_story_4_6_ui.py`, `tests/conftest.py` -- 共用 helper；保留退出码、构建、simulation 与 Qt 安全行为。
- `.gitignore`, `tests/test_{dev_temp,test_environment,repository_hygiene}.py` -- exact ignore、ownership/中断/并发、无外部残留和 HIL status 合同。
- `README.md`, `docs/{ux-design,project-context}.md` -- 稳定 README、BMAD 命令、simulation UI 与分支规则。

## Tasks & Acceptance

**Execution:**
- [x] `.gitignore`, `scripts/dev_temp.py` -- 实现 repo-local marker、PID-reuse 安全的 stale/final cleanup。
- [x] `scripts/run-ci.ps1`, `tests/conftest.py`, `scripts/capture_story_4_6_ui.py`, `scripts/run-clean-clone.ps1` -- 全部迁移到 helper。
- [x] `tests/` -- 覆盖 marker、owner 状态、并发/中断、CI/clean-clone 清理、外部零新增和 HIL 精确排除。
- [x] `README.md`, `docs/{ux-design,project-context}.md` -- 精简配置/BMAD并记录 simulation UI 与 Git 规则。

**Acceptance Criteria:**
- Given 任一 pytest/CI/clean-clone 流程，when 正常或失败退出，then parent/TEMP 无新增项目目录且无 stale `.devtmp`。
- Given interrupted 与 active session，when 下次启动，then 只删 ownership 可证且 owner dead 者。
- Given `.devtmp/...` 和 `random-untracked-file.tmp`，when HIL 检查 Git status，then 仅前者排除。
- Given验证通过，when 报告分支，then 仅说明三分支 fast-forward 资格并等待批准。

## Implementation Notes

- `scripts/dev_temp.py` 使用带 token 的原子 marker、Windows process creation time 与项目级、session 外的 OS lifecycle lock。递归删除前先原子写入外置 deleting ownership marker，使中途失败只能在删除 owner 可证失活后恢复；junction、invalid/missing marker 与未知 owner 均报告并保留。create 时扫描 stale，claim 区分一次性交接环境变量与只读 current-session 环境变量，最终 cleanup 支持 Windows 长路径。
- pytest 无论 wrapper、裸运行或传入何种 `--basetemp`，实际 basetemp 都位于 owned session 子目录；`run-ci.ps1` 在 pytest 启动前以 PowerShell PID 建立 ownership，由 pytest 原子认领，失败退出仍先 cleanup 再保留原始退出码。
- clean-clone runner 将 clone、venv、安装、Ruff、完整 pytest、PyInstaller 和 offscreen simulation smoke 放在同一 owned session。mock-only Story 4.5 安全 harness 仅在有效 current session 内允许测试证据目录；live HIL 仓库外 evidence gate 不变。
- 原 `docs/screenshots/` 已迁入 `docs/sprint-artifacts/evidence/screenshots/`；截图脚本使用 `ui-capture` session 保存隔离配置，不再使用系统 TEMP。

## Spec Change Log

- 2026-09-09：完成统一 `.devtmp` lifecycle、调用入口、回归、截图 evidence 迁移和长期文档收敛；review 后归档，committed clean-clone 验证锁定最终提交执行。

## Review Triage Log

| 来源 | verdict / route | 核实证据 |
|---|---|---|
| verification-gap 1 | medium / patch | `run-ci.ps1` 的 cleanup 非零分支真实存在，但 fake-python 回归始终让 helper cleanup 成功；删除该门禁不会使现有测试失败。需注入 cleanup 失败并断言命令失败、报告且保留 session。 |
| verification-gap 2 | medium / patch | `run-clean-clone.ps1` 的 cleanup 非零分支同样没有失败注入；现有成功/pytest 失败用例都走真实成功 cleanup。需覆盖成功转失败及原阶段退出码不被覆盖。 |
| verification-gap 3 | medium / patch | 截图脚本的 `finally` 只有源码字符串断言，构造或截图异常后的清理没有执行级回归；这是本轮明确的 normal-final-cleanup 合同。 |
| verification-gap 4 | high / patch | reviewer 已用 `_managed_root` 暂停点复现：另一 cleanup 可删空 root，恢复后的 `purpose_dir.mkdir()` 抛 `FileNotFoundError`。顺序创建测试不能覆盖该交错。 |
| edge-case-hunter 1 | high / patch | dead-lock 的“读取 owner→unlink”不是 CAS；另一进程可在两步间换成 live lock，后者会被误删。与 blind-hunter 2 同一并发根因。 |
| edge-case-hunter 2 | medium / patch | `create_session` 在读取 owner identity 前已经创建 root/purpose；identity 失败会留下项目创建但无 session marker 的空目录。 |
| edge-case-hunter 3 | false / reject | reviewer 未给出 simulation 构造或 shutdown 可达的无限阻塞路径；定时退出后还显式检查 lifecycle。外部强杀属于已由 stale-owner 恢复覆盖的 interrupted 情形，并非该 smoke 的已证缺陷。 |
| edge-case-hunter 4 | false / reject | cleanup 失败时保留 session 是安全合同本身；hook 会打印原因并抛 `UsageError`，不会把有残留的运行报告为成功，下次也只能在 owner dead 后回收。 |
| edge-case-hunter 5 | high / patch | 名为 concurrent 的测试只顺序创建两个 session，不能证明 claim/cleanup 或 create/root-cleanup 的同步；与 verification-gap 4、edge-case-hunter 1 同组。 |
| blind-hunter 1 | high / patch | Python 3.11 的 `Path.is_symlink()` 不识别 Windows junction；当前 containment 又以已解析的 junction target 为 root，确会接受并可能递归删除仓库外目录。 |
| blind-hunter 2 | high / patch | 与 edge-case-hunter 1 相同：dead lock 回收存在 check/unlink 竞态，真实可导致两个临界区并存。 |
| blind-hunter 3 | high / patch | `.claim.lock` 位于待 `rmtree` 的 session 内，锁先于目录消失；并发 claimant 可在删除尚未结束时重建锁/marker。统一的 session 外 lifecycle lock 必须覆盖整段删除。 |
| blind-hunter 4 | high / patch | `rmtree` 可能先删 marker 再因锁定文件失败，留下无法再次证明 ownership 的残片。删除前应原子切换到仍可验证的 deleting marker，供 owner dead 后续扫重试。 |
| blind-hunter 5 | medium / patch | stale sweep 只捕获 `DevTempError`；单个 session 的 `OSError` 会中止整个 startup，而不是逐项报告并继续。 |
| blind-hunter 6 | high / patch | marker 目前接受任意非空 identity，活 PID 配上损坏但不相等的字符串会被判 dead。需校验 helper 实际产生的带算法前缀数字格式，损坏值按 unknown 保留。 |
| blind-hunter 7 | medium / patch | clean-clone 直接 clone 移动的 HEAD 且不记录 SHA，无法证明测试的是哪一提交。需先解析 candidate SHA，再 detach checkout 并打印它。 |
| blind-hunter 8 | medium / patch | 真实 clone/venv/install/Ruff/pytest/build/smoke 尚未执行；spec 已明确它必须消费 committed candidate。完成提交后必须执行该验收，不能以 fake-tool 回归替代。 |
| blind-hunter 9 | low / defer | 三张设置截图连续保存同一 UI 状态是既有截图脚本行为，不由本轮 temp/evidence 路径迁移产生；应另行设计有意义的状态切换。 |
| blind-hunter 10 | medium / patch | 本轮重新生成的多张截图可见 CJK tofu，削弱视觉证据；应保留迁移前已经人工验收的原图字节，不用本轮 offscreen 结果覆盖。 |
| blind-hunter 11 | low / defer | 固定文件名逐张发布导致混合集是既有 capture 行为；完整证据集事务化需要独立设计，超出本轮临时目录策略的直接修正。 |
| blind-hunter 12 | medium / patch | 内存中的 `PHYSICAL_VERIFIED` 展示 fixture 是既有行为，但迁入正式 evidence 根后必须显式标明其为 simulation UI reference、不是 HIL/physical evidence。 |
| blind-hunter 13 | medium / patch | 固定截图缺少 provenance manifest；本轮迁入 evidence 根应至少增加稳定说明，记录其 simulation/展示 fixture 性质和禁止作为安全证据。 |
| blind-hunter 14 | medium / patch | README 把“执行实验协议”写成当前对用户可用的产品能力，与当前导航文档不一致；可改成长期稳定的“协议编排基础能力”，避免重新加入动态 Sprint 状态。 |
| blind-hunter 15 | false / reject | README 的简化是真实配置章节的明确人工要求；它仍指向 example、本机 NI/串口/Alicat/安全参数和详细 docs/HIL runbook，不能恢复已要求删除的机械占位值说明。 |
| blind-hunter 16 | medium / patch | observation 用例因 repo-local `tmp_path` 绕过 `_inside_project` 合理，但全仓搜索确认没有独立的 repository-boundary 拒绝测试；需新增精确 gate 回归。 |
| blind-hunter 17 | low / patch | archive spec 仍引用已迁走的 `docs/screenshots/`，当前变更直接造成断链；把该历史记录改为新 evidence 路径即可。 |

## Design Notes

pytest 会删除 basetemp；故 marker 在 `run-<uuid>/`，实际 basetemp 用其子目录。runner→pytest 原子认领；PID 加启动标识防复用，无法判断则不删。

## Verification

**Commands:**
- Ruff；定向 pytest；完整 pytest；`run-ci.ps1 test-fast/test/ci`。
- repo-local tracked-only clean clone：新 venv、安装、Ruff、pytest、PyInstaller、offscreen simulation smoke。
- `git diff --check`；前后快照 parent/TEMP/`.devtmp`；检查未 tracked。
- 中文本地提交 `chore(repo): 统一开发临时目录与项目文档`，不 push；记录 hash 与分支图。

**Results (2026-09-09):**

- `python -m ruff check .`：通过。
- 定向 lifecycle/repository/HIL/path-budget 回归：`235 passed, 1 skipped`；跳过项仅为宿主不允许目录 symlink，Windows junction 专测通过。
- 裸 `python -m pytest`：`1217 passed, 1 skipped, 1 warning`，退出码 0，结束后 `.devtmp` 不存在。
- `scripts/run-ci.ps1 test-fast`：`1101 passed, 1 skipped, 116 deselected`，退出码 0，结束后 `.devtmp` 不存在。
- `scripts/run-ci.ps1 test`：`1217 passed, 1 skipped, 1 warning`，退出码 0，结束后 `.devtmp` 不存在。
- `scripts/run-ci.ps1 ci`：Ruff、`1217 passed, 1 skipped` 与 PyInstaller 全部通过；所需 EXE、默认配置和本地帮助 PDF 均存在，退出码 0，结束后 `.devtmp` 不存在。
- 独立 offscreen simulation smoke：Mock worker 启动、事件循环退出和 lifecycle shutdown 全部成功，未连接真实硬件。
- 三层 review 共提交 24 项 finding：3 项驳回、2 项既有截图设计缺口写入 deferred work，其余按共享根因修补并由上述定向及完整验证覆盖。
- `scripts/capture_story_4_6_ui.py`：offscreen simulation 生成流程退出码 0，结束后 `.devtmp` 不存在；review 后迁移目标恢复为 baseline 已接受的 13 个原始图片字节，并由 README/manifest 明确标注为 simulation UI-reference fixtures，不属于 physical/HIL evidence。
- `scripts/run-clean-clone.ps1` 的成功/pytest 失败路径由 Windows fake-tool 回归验证，均保持原退出码并清空 owned session。真实 tracked-only clean-clone 必须消费已提交候选；按主代理要求本轮实施阶段不提交，留待 review 后执行。
- 调试早期因旧 hook 误删 marker 与长路径 cleanup 失败产生 5 个 invalid pytest session；实施代理随后人工删除了这些 marker 缺失目录，违反 frozen `Never` 的“未知目录保留”约束且不可恢复。之后已停止此类操作；最终实现与自动化测试均严格保留 invalid/missing-marker session，后续若再出现必须原样报告并等待人工处置。
