---
title: '仓库卫生与可复现开发环境整理'
type: 'chore'
created: '2026-09-09'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: '3d23f90cc912be315f4e6b9d0224356f9bed11e8'
context:
  - '{project-root}/docs/project-context.md'
  - '{project-root}/docs/architecture.md'
  - '{project-root}/docs/prd.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 当前仓库虽有 1168 项测试基线，但活动/归档文档边界、测试辅助代码命名、PyInstaller 数据范围、CI 原生退出码、开发依赖说明、本机配置示例和 ignore 规则仍有失真或污染；部分看似旧的 UI/backend 又承担 Controller、HIL、安全或未来 Auto/Breath 回归，不能按名称删除。

**Approach:** 以 `3d23f90cc912be315f4e6b9d0224356f9bed11e8` 为基线做证据驱动的最小整理：只删除已证明无现行用途的文件，保留并标明复用资产，校正文档/工具链/本地边界，增加基于真实 duration 的快速测试入口，并用 tracked-only clean clone 完整复现。

## Boundaries & Constraints

**Always:** 保持 Manual A/B/C、baseline/stimulus/restore、Controller→Worker→HAL、单写者、lease/owner/epoch/receipt、monotonic deadline、SafeStopPlan、HardwareProfile revision/CAS/fingerprint、physical verification 与 simulation/physical evidence 隔离不变；保留 Protocol executor/scheduler/trigger、Breath metrics/gating/minimum interval 基础、Cleaning/Maintenance 安全 primitive、Calibration 可复用算法及全部相应安全回归。删除必须同时有 import/dynamic registration、runtime、tests、PyInstaller、scripts、当前文档、未来 Auto/Breath、C.3b 与 evidence 的否定证据。

**Never:** 不连接或驱动真实 NI/Alicat/valve，不执行 C.3b HIL，不实现 Auto TXT/USB-6501/Breath UI，不大规模升级依赖，不引入 xdist，不创建 Epic/Story/AGENTS.md，不改写 Git 历史或 push，不为结构美观重写安全架构。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| 普通 clone | 仅 tracked 文件、Python 3.11、无 `.agents`/本机 venv/local config | 安装 dev 依赖后 lint、完整 pytest、simulation 与 PyInstaller 均可运行 | 缺声明依赖或路径耦合即修复并重跑 |
| 快速开发检查 | `scripts/run-ci.ps1 test-fast` | 排除 duration 证明确属 slow 的层，保留高价值 domain/controller/safety 测试 | marker 未注册或空收集即失败 |
| CI 子命令失败 | ruff/pytest/PyInstaller 任一返回非零 | 脚本立即以非零失败，不继续冒充成功 | 输出失败阶段与原始退出码 |
| 本机/生成文件 | local config、BMAD、cache、build、runtime output | 不 tracked；示例不含真实校准或机器私有值 | 已 tracked 项先保留本地内容并仅移除索引 |

</frozen-after-approval>

## Code Map

- `app/main.py`, `app/views/main_window.py`, `pyinstaller.spec` -- 正式 runtime 只构造 Manual/Settings；打包仅需默认配置与用户手册，不能携带整套内部 docs。
- `app/views/{pretest,calibration,cleaning,protocol,session}_view.py`, `tests/legacy_ui_harness.py` -- 不进正式窗口，但仍支撑 Controller regression、session/cleaning 流程及 HIL benchmark；保留 View，重命名 harness 以反映真实职责。
- `app/services/{protocol_parser,protocol_executor,breath_metrics,gating_service,calibration_service}.py`, `app/workers/actuation_worker.py` -- Auto/Breath/Cleaning/Calibration 可复用与安全资产，禁止删除或改语义。
- `scripts/hil_single_line_mapping.py` -- 仅历史归档引用且归档已明确“退役”，无测试/当前 runbook/C.3b 入口；删除代码，保留历史记录。其他 HIL/probe/timing 脚本保留。
- `scripts/run-ci.ps1`, `pytest.ini`, `tests/test_{app,flow_controls,cleaning_view,session_view}.py` -- 当前完整 suite 1168 项、外置 basetemp 下 1168 passed/290.49s；慢项集中在四个 Controller/UI 集成文件，适合单一 `slow` 分层。
- `README.md`, `docs/{index,project-context,prd,architecture,ux-design,project-structure}.md`, `docs/sprint-artifacts/` -- 校正当前 runtime/未来能力、pytest-qt 失真、BMAD v6.12 `bmad-build`、活动/归档索引；完成 spec 归档但 evidence 保持原位。
- `.gitignore`, `config/local_config.example.json`, `requirements*.txt`, `.github/workflows/ci.yml` -- 本地边界、去机器化示例与依赖/CI 一致性；版本保持不升级。

## Tasks & Acceptance

**Execution:**
- [x] `config/__init__.py`, `scripts/hil_single_line_mapping.py` -- 删除无 import 的占位包和已有“退役”证据的一次性真实动作入口；不得删除其他 production/HIL 文件。
- [x] `pyinstaller.spec`, `.gitignore`, `config/local_config.example.json` -- 缩小打包数据到运行必需文件，补齐实际会生成的本地目录/Node/Windows 缓存边界，移除示例中的现场校准值。
- [x] `tests/legacy_ui_harness.py`, `pytest.ini`, 四个慢测试模块, `scripts/run-ci.ps1` -- 重命名为 Controller regression harness，注册最少 slow marker、增加 `test-fast`，修复所有 Python 原生进程退出码传播；不删、不拆、不弱化断言。
- [x] `docs/sprint-artifacts/spec-*.md`, `docs/archive/sprint-artifacts/`, `docs/index.md`, `docs/sprint-artifacts/sprint-status.yaml` -- 将已完成 execution spec 归档并修复索引；C.3b checklist 和全部 safety/HIL evidence 原位保留。
- [x] `README.md` 与六份权威文档 -- 区分普通运行/开发/BMAD/真实硬件，固化当前 Manual+Settings runtime 与未来 Auto/Breath，去除旧 workflow/导航/pytest-qt 等失真。
- [x] 仓库外 clean clone -- Python 3.11 新 venv 安装 `requirements-dev.txt`，执行 lint、完整 pytest、simulation smoke 与 PyInstaller；不得读取当前 `.agents`、`.venv` 或 local config。

**Acceptance Criteria:**
- Given 对全部 246 个 tracked 文件完成分类与交叉引用审计，when 查看最终 diff，then 每个删除/移动均有证据，Auto/Breath/Cleaning/Calibration/安全/HIL 能力和 regression 无损。
- Given 任一 lint/test/build 阶段失败，when 运行 `run-ci.ps1` 对应任务或 `ci`，then 返回非零；成功时 full gate 仍运行全部 pytest 与 PyInstaller。
- Given 当前权威文档与 README，when 搜索产品事实，then A/B/C、三层 mapping、8-port mapping、physical verification、Auto TXT/SuperLab/c-pod/USB-6501/第128行/冲突策略、Breath minimum interval、100 ml/min 与 5 s 均存在且无矛盾。
- Given tracked-only clone，when 不安装 BMAD 且不提供 local config，then simulation 产品可启动；另行执行 `npx bmad-method install` 后可继续使用 `bmad-build`，产品 runtime/CI 不依赖 Node/BMAD。

## Implementation Notes

- 以基线 commit 的全部 246 个 tracked 文件完成分类：`app/` 58、`tests/` 65、`docs/` 103、`scripts/` 8、`config/` 3、`.github/` 1、`_bmad/` 1、根目录 7。逐项检查 import/动态注册、runtime、tests、PyInstaller、scripts、当前文档、未来 Auto/Breath、C.3b 与 evidence 引用。
- 删除范围严格限制为 37-byte 的 `config/__init__.py` 占位文件和已由归档明确标记“退役”、且只有历史引用的 `scripts/hil_single_line_mapping.py`。其余 production、compatibility View、Protocol/Breath/Cleaning/Calibration 与 HIL/probe/timing 文件全部保留。
- 将九份 `done/completed` execution spec 移入 `docs/archive/sprint-artifacts/`，同步修复迁移后相对链接、`docs/index.md`、`sprint-status.yaml` 和 deferred-work 来源；`docs/sprint-artifacts/evidence/` 与 C.3b checklist 未移动、未修改。
- `slow` 仅标记 duration 排名前列的 `test_app.py`、`test_flow_controls.py`、`test_cleaning_view.py` 与 `test_session_view.py`；完整 gate 不应用 marker 过滤。新增仓库合同测试覆盖原生退出码、full/fast 命令、打包数据、本机示例/ignore 边界、活动 spec 和默认 Mock 配置。
- clean clone 位于仓库外，从暂存 diff 生成并提交临时快照；检查确认无 `.agents`、外部 venv 或 `config/local_config.json` 后才安装依赖和执行产品门禁。BMAD 6.12 在产品门禁全部通过后另行安装，并生成 Codex `bmad-build` skill。

## Spec Change Log

- 2026-09-09：完成仓库卫生、测试分层、CI/打包、本机边界、文档归档与 tracked-only clean clone 复现；任务全部勾选，进入 review 前验证。
- 2026-09-09：三层 review 修补完成，主仓与 clean clone 全部门禁通过；规范标记 done 并转入历史归档。

## Review Triage Log

| ID | Finding | Verdict / evidence | Route |
|---|---|---|---|
| VG-1 | 打包后未验证手册与默认配置的实际路径 | **medium**：`run-ci.ps1 build` 只检查 PyInstaller 退出码；现有测试只读 spec 文本，无法阻止 PDF 目的目录与 `manual_path` 脱节。 | patch |
| VG-2 | pytest wrapper 的临时目录 ownership 仅做源码字符串断言 | **medium**：fake Python 始终使用显式 basetemp，未执行 managed cleanup，也未观察成功/失败后的目录状态。 | patch |
| VG-3 | fast path 没有按实际 collection 验证边界 | **low**：命令参数测试不能证明四个 slow 模块被排除且代表性 safety/domain 测试仍被收集；这是可直接补齐的开发门禁。 | patch |
| VG-4 | ignore 规则只做文本检查 | **medium**：后续 negation 可让字符串仍存在但 Git 实际不再 ignore，本机配置或 runtime recovery 文件会重新进入 status。 | patch |
| EC-1 | 显式 basetemp 指向既有共享目录时 pytest 会清空内容 | **medium**：`--basetemp` 会清理既有目录，而 wrapper 当前无“仓库外且不存在”校验，调用者文件可能丢失。 | patch |
| EC-2 | PyInstaller 返回 0 但无 exe 时 build 仍成功 | **low**：真实 spec 通常会产出 exe，但 wrapper 的 `$exe` 分支没有失败路径，fake 构建已能到达该状态。 | patch（与 VG-1 同组） |
| EC-3 | 活动 spec 数量为零时归档测试 vacuous pass | **false**：仓库在所有执行规范完成后允许没有活动 spec；该测试的契约是“活动目录不得含 terminal spec”，不是强制永久保留一个活动项。 | reject |
| EC-4 | terminal status 带 YAML 行尾注释时未识别 | **low**：当前解析会把注释并入值，确会漏报完成规范；移除行尾注释后再比较即可直接修正。 | patch |
| EC-5 | ignore 规则被反向 negation 时文本测试仍通过 | **medium**：与 VG-4 的 `git check-ignore --no-index` 缺口相同。 | patch |
| EC-6 | 文档声称未固定版本的 npx 命令仍安装相同 BMAD 6.12 | **low**：npm 默认 tag 会随发布变化；当前“仍是相同 6.12”表述不成立，应给出显式 6.12.0 命令。 | patch |
| BH-1 | local config 顶层连接值与 HardwareProfile connections 分裂 | **medium**：实际合并结果为 RealHAL `COM_REPLACE_ME`、HardwareProfile `serial_port=None`；NI/单元 ID 的自定义也会被 nested default 遮蔽。 | patch |
| BH-2 | local config template 删除校准/清洗/验证结构后不足以指导现场配置 | **medium**：README 要求用户自行填写，但模板不再给出字段位置；这不满足 clone 后可重建开发/硬件配置说明的验收。 | patch（与 BH-1 同组） |
| BH-3 | README 未区分源码启动 local config 与打包后的用户配置 | **medium**：`build_application()` 在 bundle 中跳过 `config/local_config.json`，改用 `%USERPROFILE%/.olfactorypilot/default_config.json`，现有说明会误导部署者。 | patch |
| BH-4 | BMAD 6.12 文档命令未固定版本 | **low**：与 EC-6 相同，默认 npm tag 不保证 6.12。 | patch |
| BH-5 | 四个 module-level slow marker 违反 fast path 的 safety 保留约束 | **false**：duration 审计显示这些模块的每项 Controller/UI teardown 均属慢层；fast path 仍收集 1061 项，并保留 `test_actuation_worker`、`test_safe_stop`、`test_safety_manager`、Manual/verification controller 等高价值回归，完整 gate 仍运行全部模块。 | reject |
| BH-6 | 仓库父目录只读时 managed basetemp 无法创建 | **low**：该边缘 ACL 状态可能发生，但能创建 clone 的普通开发位置通常可写，且显式仓库外 basetemp 可覆盖；增加多级 fallback 的复杂度超过日常收益。 | reject |
| BH-7 | 显式 basetemp 可为相对路径或仓库内路径并触发重定向/期望不一致 | **medium**：wrapper 原样设置 expected path，conftest 会重定向仓库内路径，`test_actual_pytest_basetemp_contract` 随后必然不一致。 | patch（与 EC-1 同组） |
| BH-8 | managed cleanup 失败会遮蔽原始 pytest 退出码 | **medium**：`$ErrorActionPreference='Stop'` 使 finally 内 `Remove-Item` 异常先于退出码传播，违反 native exit-code contract。 | patch |
| BH-9 | managed temp cleanup 没有行为测试 | **medium**：与 VG-2 相同，现有测试未让 fake Python 创建目录，也未检查 cleanup。 | patch |
| BH-10 | 打包与 ignore 边界只做字符串断言 | **medium**：两个独立缺口均真实；实际构建布局和 Git ignore 结果都没有被现有测试观察。 | patch（分别并入 VG-1 / VG-4） |
| BH-11 | CI 组合任务没有失败短路测试 | **medium**：只覆盖单任务失败及组合成功；pytest/PyInstaller 失败时是否停止后续阶段没有自动证明。 | patch |
| BH-12 | sprint status 的 `last_updated` 仍为 2026-08-25 | **low**：本轮修改了活动规范索引但未同步日期，当前唯一动态状态源因此自相矛盾。 | patch |
| BH-13 | spec Code Map 仍写旧 harness 路径 | **low**：位置确实过时，但 finding 的唯一修复是编辑本 build spec；按 review 规则拒绝以 patch 修改 spec。 | reject |
| BH-14 | spec Verification 记录具体 Windows 用户名 | **low**：当前工件确含机器用户名，但 finding 的修复只能编辑本 build spec；按 review 规则拒绝。最终报告仅描述为宿主 ACL 事实，不把它提升为产品依赖。 | reject |
| BH-15 | 删除 retired HIL 脚本令归档引用不可审计 | **false**：归档引用是历史事实而非当前链接；删除证据、baseline SHA 与完整 Git 历史均保留，可用 `git show 3d23f90:scripts/hil_single_line_mapping.py` 检视原文件，无审计能力损失。 | reject |

## Design Notes

默认不删除旧 View：AST/import 与 runtime 检查证明它们不在正式窗口，但 ProtocolView 被 HIL benchmark 使用，其余 View 由测试 harness 保护 Controller/session/maintenance 回归。后续若要去掉这些 compatibility surfaces，应另立行为重构并先建立无 View 的等价 controller contract tests。

系统默认 `%TEMP%/pytest-of-SesnoryManip` 当前存在宿主 ACL 错误；原始 duration 命令因此得到 776 passed、391 setup errors、1 environment-contract failure。仓库外显式 basetemp 同一代码为 1168 passed。该机器状态不通过删除测试掩盖，最终验证使用新的仓库外临时根并同时记录原始事实。

## Verification

**Commands:**
- `python -m ruff check .`、`git diff --check` -- 0 errors。
- `python -m pytest -q tests/test_repository_hygiene.py --basetemp <repo-external>` -- review 修复后 23 passed；直接使用宿主 `%TEMP%` 时稳定复现下述 ACL setup error。
- `python -m pytest --collect-only -q` -- 最终收集 1191 项；duration 审计为 1191 passed/299.28s，最慢项集中在四个 slow 模块；显式短外部 `TEMP/TMP` 下精确 `python -m pytest` 为 1191 passed/319.02s。
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 test-fast` -- 1075 passed、116 deselected、0 failures，约 155 秒，明显短于完整 suite；实际 collection 合同确认 `test_safe_stop.py` 与 `test_actuation_worker.py` 仍在 fast path。
- `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-ci.ps1 ci` -- 当前仓库 Ruff、1191 项完整 pytest 与 PyInstaller 全通过；门禁确认 onedir exe、默认配置和帮助 PDF 均存在。
- clean-clone simulation smoke -- Mock HAL 启动 Hardware/Actuation/Flow Worker，Qt `aboutToQuit` 触发安全收口，exit 0 且 lifecycle stopped；未连接真实硬件。
- clean-clone Python 3.11 新 venv：`pip install -r requirements-dev.txt`、ruff、完整 pytest、PyInstaller、simulation 全通过；最终结果为 1189 passed、2 个因 tracked-only 环境无本机 HIL 失败 bundle 而按既有条件 skipped、0 failures。随后用 `npx.cmd --yes bmad-method@6.12.0 install` 安装 BMAD 6.12.0，确认 `.agents/skills/bmad-build/SKILL.md` 存在且安装未产生未忽略文件。
