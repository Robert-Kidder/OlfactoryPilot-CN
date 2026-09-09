# OlfactoryPilot-CN 文档索引

本页只描述文档层级。当前权威、动态状态、活动执行工件、审计证据和历史资料彼此分离；历史 Story 或旧规划不得覆盖当前权威。

## 当前权威

- [`project-context.md`](project-context.md)：项目定位、技术与硬件基线、执行域和长期规则。
- [`prd.md`](prd.md)：产品目标、用户需求和验收边界。
- [`architecture.md`](architecture.md)：线程所有权、执行域、HAL、安全停止和数据架构。
- [`ux-design.md`](ux-design.md)：当前产品语言、状态反馈和交互原则；不预设未立项页面。
- [`project-structure.md`](project-structure.md)：目录、工具链与文档放置规则。

## 需求拆分历史（非当前权威）

- [`epics.md`](epics.md)：旧需求拆分历史；不维护动态进度，也不得覆盖当前 PRD、Architecture 或活动 execution spec。

## 状态与活动执行工件

- [`sprint-artifacts/sprint-status.yaml`](sprint-artifacts/sprint-status.yaml)：唯一动态 Epic/Story 状态源。
- [`sprint-artifacts/4-1-cleaning-automation.md`](sprint-artifacts/4-1-cleaning-automation.md)：暂停中的清洗 Story。
- [`sprint-artifacts/4-6-manual-experiment-v3-replacement.md`](sprint-artifacts/4-6-manual-experiment-v3-replacement.md)：处于 review gate 的手动实验 Story。

## 审计证据

- [`sprint-artifacts/evidence/`](sprint-artifacts/evidence/)：安全、HIL、气路、极性、ATDD 和发布证据。整理文档不得改变或删除原始证据。
- [`assets/diagrams/`](assets/diagrams/)：现场气路原图与可维护转录。
- [`ALICAT-MANUAL.md`](ALICAT-MANUAL.md)：Alicat 串口命令参考。
- [`ManuelUtilisation_ProgOlfacto.pdf`](ManuelUtilisation_ProgOlfacto.pdf)：ProgOlfacto 历史说明书。

## 历史归档

- [`archive/README.md`](archive/README.md)：归档边界和入口。
- [`archive/sprint-artifacts/`](archive/sprint-artifacts/)：已完成 Story、execution spec、评审、复盘、旧变更提案、旧规划状态与状态快照。
- [`archive/sprint-artifacts/spec-repository-hygiene.md`](archive/sprint-artifacts/spec-repository-hygiene.md)：本轮已完成的仓库卫生规范、review triage 与验证记录。
- [`archive/FeatureList-legacy.md`](archive/FeatureList-legacy.md)：已停止维护的功能清单快照。

删除任何历史候选前必须完成全仓引用检查，并确认其不含独有产品决策、硬件事实、安全/HIL/极性/发布证据，且不被代码、测试、CI、README、BMAD 配置或当前权威文档引用；不确定时继续归档。
