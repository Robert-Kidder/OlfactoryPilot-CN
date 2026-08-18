---
title: 'Story 4.5 真实硬件 HIL 证据收口'
type: 'chore'
created: '2026-08-18'
status: 'done'
review_loop_iteration: 2
context:
  - 'docs/project-context.md'
  - 'docs/sprint-artifacts/epic-4-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Story 4.5 的 `normal` 真实硬件 HIL 已通过，但原始证据仍位于临时目录，Story 状态和现场手册仍写着“真实 HIL 待授权”。

**Approach:** 将原始证据逐字节归档到项目，增加简明索引，并只更新与本次验收直接相关的 Story、运行手册和 sprint 状态记录。

## Boundaries & Constraints

**Always:** 保留原始证据内容与哈希；区分电子 ack、软件 receipt 和操作者机械/气路观察；记录固定候选、最终 A/B/C、selector、气味阀与 owner handoff；明确上游 Air 已关闭。

**Ask First:** 创建新的本地提交；任何新的真实 HIL、NI/Alicat 写入或 push。

**Never:** 重写历史开发证据；把 normal 结果外推为真实故障注入结果；修改生产代码；访问硬件；覆盖或删除原始证据。

</frozen-after-approval>

## Code Map

- `docs/sprint-artifacts/evidence/story-4-5-hil-normal-20260818/` -- 原始 HIL 证据与验收索引
- `docs/sprint-artifacts/4-5-safe-stop-selector-order.md` -- Story 4.5 验收补充
- `docs/sprint-artifacts/evidence/story-4-5-hil-runbook.md` -- 当前现场状态与复跑边界
- `docs/sprint-artifacts/sprint-status.yaml` -- Sprint 完成状态与证据入口
- `docs/sprint-artifacts/epic-4-context.md` -- Epic 4 后续 Story 的精简上下文

## Tasks & Acceptance

**Execution:**
- [x] `docs/sprint-artifacts/evidence/story-4-5-hil-normal-20260818/` -- 归档原始文件并增加索引
- [x] 人工补充记录与 `archive-envelope.json` -- 绑定单次授权、关气确认、运行 ID 与证据哈希，同时标明非 runner 原始输出
- [x] Story、runbook 与 sprint 状态 -- 记录 2026-08-18 normal HIL 结论及复跑授权边界
- [x] 证据完整性 -- 核对 JSON 可解析、原始哈希匹配且关键结论交叉一致

**Acceptance Criteria:**
- Given 上游 Air 已关闭且不再访问硬件，when 查看归档，then 可追溯候选、授权、动作偏序、操作者观察、最终状态、owner handoff 与哈希。
- Given Story 4.5 的 fault 场景未在真实硬件上执行，when 阅读验收结论，then 文档只宣称 normal 动作偏序 HIL 与停止前 `odor` 出口观察通过，明确 `compensation` 机械映射限制，并保留 fault 场景的离线确定性覆盖边界。

## Spec Change Log

- 2026-08-18，盲审 iteration 1：明确 normal 气流观察发生在停止前 `odor` 阶段，避免把它误写为最终 `compensation` 机械确认；补充授权 payload digest 与落盘文件 hash 的区别、49/69 可选 fallback 含义、fault 字段的未触发边界、停止前 airflow 字段和 runner-reported owner handoff 证据等级。
- 2026-08-18，盲审 iteration 1：增加单次人工授权、现场关气确认及 archive envelope；人工记录只使用本会话已有日期和原文，不补造精确时间戳。
- 2026-08-18，盲审 iteration 2：将人工记录降格为 post-run attestation，明确 token 一次性是管理规则、同条消息开 Air/确认 token 是程序偏差；归档 anchor 在本地 commit 前保持 pending，并把 compensation 物理出口验证登记为 Story 4.6 真实硬件前置门禁。

## Verification

**Commands:**
- `Get-FileHash` 对照 `hashes.sha256` -- expected: 原始证据哈希不匹配为 `0`
- `Get-FileHash` 对照 `archive-envelope.json` -- expected: checksum manifest 与人工补充记录哈希不匹配为 `0`
- `ConvertFrom-Json` 解析所有 JSON/JSONL -- expected: 全部可解析
- `git diff --check` -- expected: 无 whitespace error

## 建议复审顺序

1. [证据索引](evidence/story-4-5-hil-normal-20260818/README.md)
2. [Story 4.5 验收补充](4-5-safe-stop-selector-order.md)
3. [现场运行手册](evidence/story-4-5-hil-runbook.md)
4. [Sprint 状态](sprint-status.yaml)
5. [Epic 4 上下文](epic-4-context.md)
