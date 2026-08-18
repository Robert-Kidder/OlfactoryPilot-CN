# Sprint Change Proposal：冻结 Epic 4 技术边界

**Date:** 2026-07-31
**Status:** Approved and Applied
**Approval:** Jing 本轮明确要求依据 Epic 3 retrospective 和未完成行动项调整并冻结 Epic 4 技术边界
**Scope Classification:** Moderate
**Recommended Path:** Direct Adjustment

## 1. Issue Summary

Epic 3 已完成软件 Gate 与真实 Windows/NI HIL，但复盘证明原 Epic 4 的业务型验收标准不足以约束硬件并发系统。现有 Story 4.1、4.2 和 4.4 未完整定义 owner、lease、generation、receipt、recording、fail-closed、恢复与 HIL 门禁；Story 4.3 未把中文与乱码要求变成自动化契约。

直接证据：

- Story 3.5 的失败 HIL 曾暴露首动作 backlog、owner dispatch、diagnostic I/O 和全关收敛问题。
- Epic 3 最终依赖 AI/DO/serial/file 四个单写 owner、immutable identity、producer fence 和事务式 publish 才通过。
- `sprint-status.yaml` 中 A3-03、A3-04、A3-05、A3-06、A3-09 尚未闭环，均是 Epic 4 实现前的设计门禁。
- A3-08 和 A3-10 属于后续发布/验收动作，不能因边界冻结而误标为已实现。

## 2. Impact Analysis

### Epic Impact

- Epic 3：不回滚、不重开；其 owner、receipt、session 和 HIL 证据成为 Epic 4 的强制基线。
- Epic 4：Story 数量和产品目标不变，Story 级 AC 与实施顺序调整。
- 后续 Epic：无已规划 Epic 因本次变更失效，也无需新增 Epic。

### Story Impact

- 4.1：新增显式 CLEANING category、maintenance lease、独立 maintenance bundle、状态/receipt/恢复矩阵。
- 4.2：新增断开态原子配置事务、owner handoff、commit point、无副作用失败和显式回滚。
- 4.3：新增 UTF-8、乱码、旧英文和关键用户路径自动化审计。
- 4.4：冻结 flow → master → odor 状态机、跨 owner identity、失败补偿、session event 和 deadline 边界。

### Artifact Conflicts

- PRD：产品目标和 FR 不冲突；Dev1/Dev2 必需、Dev3 可选的漂移已修复，无需再次改 PRD。
- Epics：Epic 4 原 AC 过粗，需要替换为可实施、可测试的技术验收。
- Architecture：需要补充 maintenance bundle、CONFIG_CHANGE 事务、补偿状态机和范围触发式 HIL。
- UX：需要补充清洗/配置状态门禁、三段式错误文本和乱码审计。
- Sprint status：需记录冻结证据，并关闭已由本次设计产物完成的行动项。

### Technical Impact

- 预计新增 maintenance schema、lease/action type、phase identity、配置事务服务和确定性测试夹具。
- 不允许改变现有 owner topology、协议 `timing_ms` 语义、jitter 统计口径或生产硬件基线。
- 命中 owner/queue/interlock/DO/AI/flow/session/shutdown 边界的变更必须触发对应真实 HIL。

## 3. Recommended Approach

采用 **Direct Adjustment**，范围分类为 **Moderate**：

- 保留现有 MVP、四个 Story 和 Epic 4 目标；
- 先冻结跨 Story 技术边界，再按 **4.2 → 4.1 → 4.4 → 4.3** 实施；
- 把 A3-03/04/05/06/09 转化为已批准的规格与测试门禁；
- A3-08 和 A3-10 保持 open，分别在发布候选和 Story 4.3 验收前完成。

备选方案评估：

| 方案 | 结论 | 理由 |
|---|---|---|
| Direct Adjustment | 采用；中等工作量/中等风险 | 不改变产品方向，可把 Epic 3 的已验证不变量直接前移到 AC |
| Rollback Epic 3 | 不采用；高工作量/高风险 | 已通过的软件与 HIL 证据正是 Epic 4 的稳定基线，回滚无收益 |
| MVP Review/缩减 | 不采用；低必要性 | 清洗、配置、本地化和补偿仍是实验室交付范围，问题是技术定义不足而非目标不可行 |

## 4. Detailed Change Proposals

### 4.1 Epics

**OLD**

- Story 4.1 只要求循环阀门、遵守气流联锁、失败全关并记录。
- Story 4.2 只要求保存配置、拒绝无效 ID、中文报错。
- Story 4.3 只要求中文文案和无乱码。
- Story 4.4 只描述静息/刺激 setpoint 与“先流量后主阀”。

**NEW**

- 4.1 增加 CLEANING category、独占 lease、maintenance-v1 bundle、状态/identity、stale/uncertain 和 21-target 收敛 AC。
- 4.2 增加断开态前置条件、candidate validation、atomic replace、commit point、owner handoff 和 rollback AC。
- 4.3 增加静态字符串、严格 UTF-8、mojibake、pytest-qt 路径和三段式错误验收。
- 4.4 增加完整 phase state machine、flow/master/odor receipt identity、deadline 隔离和 fail-closed AC。

**Rationale:** 把 Epic 3 中通过 review/HIL 才发现的并发与恢复约束前移，避免 Epic 4 再次在现场发现同类隐含假设。

### 4.2 Architecture

**OLD**

架构已经定义四个 owner、session durability 和 shutdown 顺序，但未定义 Epic 4 maintenance/config/compensation 的落点。

**NEW**

新增 Epic 4 冻结边界章节：maintenance bundle、CONFIG_CHANGE 事务、flow→master→odor phase、确定性并发测试和范围触发式 HIL。

**Rationale:** 防止新功能在 Controller、UI timer 或临时线程中形成旁路。

### 4.3 UX

**OLD**

清洗页只显示步骤和中止；选项页没有事务状态；错误文本只要求“发生了什么”和“下一步”。

**NEW**

清洗页显示 lease/记录/停止收敛/恢复状态；配置页只在断开安全态允许保存并明确回滚；错误文本增加“系统采取的安全动作”；本地化增加自动化审计。

**Rationale:** UI 应准确反映 owner/service 真实状态，不能产生“已关闭/已保存”的过早承诺。

### 4.4 Sprint Status

**NEW**

- 记录 Epic 4 boundary status=`frozen` 与冻结/提案证据。
- A3-03、A3-04、A3-05、A3-06、A3-09 标记 done。
- A3-08、A3-10 保持 open；Epic 4 和全部 Story 仍为 backlog，冻结不等于实现。

## 5. Implementation Handoff

### 责任

- Product Owner / Architect：守护冻结范围；任何 Ask First/Never 变更重新走 Correct Course。
- Developer：按 4.2 → 4.1 → 4.4 → 4.3 创建/实现 Story，不突破 owner、lease、receipt 和记录边界。
- Test Architect：把 CC-01–CC-12 映射到测试，并按变更范围执行 HIL 矩阵。
- Technical Writer：完成 A3-08，并与 4.3 的中文/乱码审计共同关闭用户路径。
- Project Lead：批准真实硬件 HIL 的惰性气体、无气味、无受试者执行窗口。

### 成功标准

- 四个 Story 的 AC 与冻结文档一致。
- 适用的确定性并发测试、软件 Gate 和 HIL Gate 全部有证据。
- stop/LOW_FLOW/disconnect/recorder failure/shutdown 都以 21-target receipt 和 owner handoff 收敛。
- 配置失败无部分副作用，维护/实验数据不混用，用户可见路径无旧英文或乱码。

## 6. Checklist Completion

| Checklist | Status | Finding |
|---|---|---|
| 1.1–1.3 Trigger/Evidence | [x] | 触发来自 Epic 3 retrospective、HIL 失败经验及 open action items |
| 2.1–2.5 Epic Impact | [x] | Epic 3 保持完成；Epic 4 直接调整；不新增/删除 Epic；建议重排实施顺序 |
| 3.1 PRD | [x] | MVP 与 FR 不变，无新增 PRD 冲突 |
| 3.2 Architecture | [x] | maintenance/config/compensation/HIL 边界已补充 |
| 3.3 UX | [x] | 清洗、配置、错误与本地化验收已补充 |
| 3.4 Other Artifacts | [x] | sprint status、测试矩阵、发布文档门禁已纳入 |
| 4.1 Direct Adjustment | [x] Viable | 中等工作量/中等风险 |
| 4.2 Rollback | [N/A] | 无收益且会破坏已验证基线 |
| 4.3 MVP Review | [N/A] | 产品目标仍可实现 |
| 4.4 Recommendation | [x] | Direct Adjustment |
| 5.1–5.5 Proposal/Handoff | [x] | 已形成变更、责任、顺序与成功标准 |
| 6.1–6.2 Review | [x] | 文档一致性校验纳入本次执行 |
| 6.3 Approval | [x] | 用户本轮明确要求调整并冻结 |
| 6.4 Sprint Status | [x] | 同步冻结记录与行动项状态 |
| 6.5 Next Steps | [x] | 路由 Product Owner/Architect/Developer/Test/Writer |
