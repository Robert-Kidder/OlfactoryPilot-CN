---
title: '修复 SessionWriter 基线时序测试与 pytest 临时目录隔离'
type: 'bugfix'
created: '2026-08-27'
status: 'done'
review_loop_iteration: 0
baseline_commit: 'ae6a50ade6fb437710b31a29886e15b916fb9ebe'
context:
  - '{project-root}/docs/project-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `tests/test_session_writer.py::test_slow_finalize_timeout_claims_terminal_result_and_never_publishes` 用真实 30ms wall-clock 同时约束 Windows 线程调度及仓库内文件 flush/fsync，导致仓库内 `--basetemp` 下稳定失败；同一临时目录选择还会污染 Git 状态并误触发 HIL evidence gate。

**Approach:** 保留 SessionWriter 的 30ms 逻辑关闭 deadline 和全部 HIL provenance/evidence 安全检查；把超时仲裁测试改为 fake monotonic + Event/condition 驱动，并通过有明确 ordering 的 pytest 官方早期 hook，把解析后确实位于仓库内的 basetemp 重定向到由本次测试会话独占、结束后清理的 OS temp 目录，同时在终端提示一次实际改写。

## Boundaries & Constraints

**Always:** 先保留当前与 `6a8e90b56ddfbbb1e0327d34bbf18d81cd4031d1` 的复现证据；测试使用可控 clock/event，不新增 sleep；重定向必须在 `TempPathFactory`/`tmp_path_factory` 构造前完成，优先采用 `@pytest.hookimpl(tryfirst=True)` 的 `pytest_configure(config)` 或能以公开 hook 证明同等顺序的方案；只处理 resolve 后属于当前 repository root 的 basetemp，并以真实 `tmp_path_factory.getbasetemp()` 验证结果；临时输出不得落入 repository root 或 HIL 扫描范围；最终直接从正常仓库工作区执行全量 pytest。

**Ask First:** 若确定性测试暴露 SessionWriter 实现无法在逻辑 deadline 后抢占发布，或外部临时目录不能由 pytest 生命周期可靠清理，停止并重新确认是否需要最小实现修复。

**Never:** 放宽 30ms、跳过/xfail/删除测试；依赖未指定 hook 顺序或简单字符串 `startswith` 判断路径；静默改写用户参数、使用固定公共临时目录、删除用户指定的仓库外 basetemp、修改 `.gitignore`；弱化 HIL clean-worktree、candidate、evidence-directory 检查；修改 Manual/Settings/Auto UI、A/B/C 模型、Worker 安全语义或产品需求文档；创建 Epic/Story、运行真实 HIL 或 push。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| finalize 内 deadline 到期 | finalizer 已进入 `manifest_write`，fake monotonic 从起点推进 30ms | `close()` 返回同一 `RECOVERY_REQUIRED` 终态，迟到 finalizer 不发布 final dir | staging 保留，writer 可终止，重复 close 幂等 |
| 仓库内 basetemp | `python -m pytest --basetemp=<repo-child>`，含相对路径、`..`、大小写及正常规范化情形 | 在 `TempPathFactory` 构造前改为唯一 OS temp；终端只提示一次“仓库内 --basetemp 已重定向到系统临时目录，以避免污染 Git/HIL evidence gate。”；实际 `getbasetemp()` 位于仓库外 | 正常结束清理本 session 创建的目录；异常遗留仍在仓库外，不污染 HIL |
| 默认 basetemp | 未指定 `--basetemp` | 完全保留 pytest 默认 OS temp 语义 | 不创建或登记自管目录 |
| 仓库外 basetemp | 显式路径 resolve 后不属于 repository root | 完全保留用户指定值和 pytest 原始生命周期 | 不改写、不提示、不删除用户目录 |

</frozen-after-approval>

## Code Map

- `tests/test_session_writer.py:818-846` -- 当前失败节点；`close(timeout_ms=30)` 前需由真实 QThread 完成 log/raw flush、`os.fsync`、close 并进入 `manifest_write`，`entered_finalize.is_set()` 因机器 I/O/调度而漂移。
- `tests/test_session_writer.py:849-899` -- 相邻测试已有 fake monotonic、Event wrapper 与 timeout/late-complete 仲裁模式，可复用而不改产品实现。
- `app/workers/session_writer.py:1028-1084,1603-1727,1846-1889` -- 只读契约证据：30ms 是 `close()` 的总 timeout/deadline；路径含真实文件 I/O，timeout 抢占后设置 publish cancellation 并保留 staging。除非确定性 RED 指向实现错误，否则不修改。
- `tests/conftest.py` -- pytest 全局生命周期入口；用带 `tryfirst=True` ordering 的公开配置 hook 在 tmp_path_factory 首次请求前完成规范化、归属判断、唯一目录创建和单次提示，并仅登记本 session 自建目录供正常 teardown 清理。
- `scripts/hil_story45_safe_stop.py:477-520,1170-1203`、`scripts/hil_cleaning_gate.py:35-75`、`scripts/hil_actuation_benchmark.py:2020-2055` -- 只读 HIL 安全证据；Git 未跟踪临时目录和仓库内 evidence path 应继续被拒绝。
- `pytest.ini` -- 当前仅 `-ra`，默认 tmp_path 已在 OS temp；问题仅在显式仓库内 basetemp，不能写死机器绝对路径。

## Tasks & Acceptance

**Execution:**
- [x] `tests/test_session_writer.py` -- 用 fake monotonic 和 Event wrapper 确定性建立“finalizer 已进入后 deadline 到期”的交错，保留 30ms 值及终态、幂等、目录断言。
- [x] `tests/conftest.py` -- 通过 `@pytest.hookimpl(tryfirst=True)` 的 `pytest_configure(config)`（或有同等公开顺序证明的 hook）在 `TempPathFactory` 构造前处理 basetemp；对相对/绝对输入先做 Windows-aware resolve/规范化，再用路径归属 API 判断，不用字符串前缀；仓库内输入重定向至 `mkdtemp`/等价唯一 OS temp、只提示一次并登记正常清理；默认值和仓库外显式值不改写、不登记删除。
- [x] `tests/test_test_environment.py` -- 直接断言 `tmp_path_factory.getbasetemp()` 的实际路径在 repository root 外；覆盖仓库内相对/规范化输入被改写且提示一次、未指定值保持默认、仓库外显式值保持原值且不会被项目清理。

**Acceptance Criteria:**
- Given 当前提交和 `6a8e90b` 的可比 D: 仓库内 basetemp，when 连续至少 5 次运行旧测试，then 都以 expected `entered_finalize=True`、actual `False` 失败，证明 Build B 前已存在。
- Given 修复后的测试，when 在默认及显式仓库内 basetemp 条件运行，then 不依赖机器在 30ms 内完成 I/O/调度，仍严格验证逻辑 deadline 抢占发布。
- Given `--basetemp` resolve 后位于 repository root 内，when pytest 配置和 tmp path factory 初始化，then 早期 hook 先完成重定向，终端只提示一次，且 `tmp_path_factory.getbasetemp().resolve()` 位于 repository root 外。
- Given 未指定 basetemp 或显式指定仓库外目录，when pytest 初始化及退出，then 项目不改写 pytest 的实际 basetemp、不输出重定向提示，也不删除用户指定的仓库外目录。
- Given 两个并发 pytest session 都收到仓库内 basetemp，when 各自重定向，then 使用不同的 session-owned OS temp，任何一方清理都不影响另一方。
- Given 完整 `python -m pytest`，when 从正常仓库工作区执行，then 0 failures，运行前后无需手工删除测试目录且 Git 不因普通 tmp_path 输出变脏。

## Spec Change Log

## Design Notes

30ms 属于 `SessionWriterWorker.close()` 的有界关闭预算，不是“Windows 必须在 30ms 内调度 finalizer 到 manifest_write”的性能指标。测试应先用 Event 证明 finalizer 已进入阻塞点，再推进 fake monotonic 到 deadline 并让等待返回 timeout；这样验证的仍是 timeout 对迟到 publish 的终态所有权。

pytest 的重定向只处理解析后位于项目根内的 basetemp。相对路径以当前 pytest 进程语义 resolve，路径包含关系使用规范化后的路径 API，正确处理 Windows 大小写、`..` 和符号/规范化路径；不得通过 `.gitignore` 隐藏普通测试输出，也不得让 HIL gate 忽略未知目录。

重定向目录必须由本次 session 唯一创建并记录所有权，正常退出只清理该目录。异常退出可能留下 OS temp，但不得污染 repository；默认 pytest temp 和用户显式指定的仓库外 basetemp 均不归项目所有，不得删除。配置 hook 的单次提示是参数语义发生变化的可见契约，不得依赖 warning 去重的偶然行为。

## Verification

**Commands:**
- `python -m pytest tests/test_session_writer.py::test_slow_finalize_timeout_claims_terminal_result_and_never_publishes -vv` -- 默认环境确定性通过。
- `python -m pytest tests/test_session_writer.py::test_slow_finalize_timeout_claims_terminal_result_and_never_publishes tests/test_test_environment.py -vv --basetemp=.sessionwriter-repro` -- 自动使用仓库外实际 basetemp；`getbasetemp()` 归属断言、单次中文提示、目标节点和仓库内目录不存在断言均通过。
- `python -m pytest tests/test_test_environment.py -vv --basetemp=<明确的仓库外临时路径>` -- 实际 basetemp 保持用户指定路径且项目不删除该目录。
- `python -m ruff check .` -- 通过。
- `python -m pytest` -- `1007 passed, 1 warning in 748.48s`，0 failures。
- `git diff --check` -- 通过。
- `git status --short` -- 提交后无输出。

## Suggested Review Order

**临时目录策略**

- 早期重定向入口
  [`conftest.py:21`](../../../tests/conftest.py#L21)

- 会话所有权清理
  [`conftest.py:53`](../../../tests/conftest.py#L53)

**30ms 时序契约**

- 确定性 deadline 仲裁
  [`test_session_writer.py:818`](../../../tests/test_session_writer.py#L818)

- 隔离 fake clock
  [`test_session_writer.py:841`](../../../tests/test_session_writer.py#L841)

**验收覆盖**

- 验证真实 factory
  [`test_test_environment.py:22`](../../../tests/test_test_environment.py#L22)

- 覆盖重定向清理
  [`test_test_environment.py:44`](../../../tests/test_test_environment.py#L44)

- 拒绝仓库内 temp
  [`test_test_environment.py:73`](../../../tests/test_test_environment.py#L73)

- 保留仓库外目录
  [`test_test_environment.py:88`](../../../tests/test_test_environment.py#L88)

- 隔离并发会话
  [`test_test_environment.py:107`](../../../tests/test_test_environment.py#L107)
