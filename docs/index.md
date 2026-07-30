# OlfactoryPilot-CN 文档索引

本页是仓库内长期项目资料的入口。当前 Epic/Story 状态只以 [`sprint-artifacts/sprint-status.yaml`](sprint-artifacts/sprint-status.yaml) 为准；历史文档中的旧状态和旧硬件假设不覆盖主线文档。

## 当前主线

- [`project-context.md`](project-context.md)：开发者与 AI 必须遵守的当前项目约束、硬件基线和实现规则。
- [`prd.md`](prd.md)：产品目标、功能需求和非功能需求。
- [`architecture.md`](architecture.md)：MVC + Worker + HAL 架构、线程所有权、安全与数据边界。
- [`ux-design.md`](ux-design.md)：界面结构、交互模式和中文 UX 约定。
- [`epics.md`](epics.md)：Epic 与 Story 的需求拆分；具体实施状态不在此维护。
- [`project-structure.md`](project-structure.md)：目录、工具链、文件放置和 BMAD 工作规则。
- [`bmm-workflow-status.yaml`](bmm-workflow-status.yaml)：BMAD 规划阶段产物与当前实施状态源的指针。

## 参考资料与规划记录

- [`ALICAT-MANUAL.md`](ALICAT-MANUAL.md)：由既有资料整理的 Alicat 串口命令参考，真实操作仍须按现场授权。
- [`ManuelUtilisation_ProgOlfacto.pdf`](ManuelUtilisation_ProgOlfacto.pdf)：原法国 ProgOlfacto 软件说明书。
- [`implementation-readiness-report-2025-12-08.md`](implementation-readiness-report-2025-12-08.md)：2025-12-08 实施就绪规划门禁记录。

## Sprint 状态与跨 Story 记录

- [`sprint-artifacts/sprint-status.yaml`](sprint-artifacts/sprint-status.yaml)：唯一 Epic/Story 实施状态源及复盘行动项清单。
- [`sprint-artifacts/sprint-change-proposal-2025-12-10.md`](sprint-artifacts/sprint-change-proposal-2025-12-10.md)：Mock HAL 与硬件模拟策略的已批准变更提案。
- [`sprint-artifacts/validation-report-1-1-device-self-check-and-status-report.md`](sprint-artifacts/validation-report-1-1-device-self-check-and-status-report.md)：Story 1.1 自检需求验证记录。
- [`sprint-artifacts/epic-1-real-hardware-verification.md`](sprint-artifacts/epic-1-real-hardware-verification.md)：Epic 1 真实硬件复核记录，含 2026-07-30 Dev3 证据作废说明。
- [`sprint-artifacts/epic-1-retrospective.md`](sprint-artifacts/epic-1-retrospective.md)：Epic 1 安全硬件基础复盘。
- [`sprint-artifacts/epic-2-retro-2025-12-14.md`](sprint-artifacts/epic-2-retro-2025-12-14.md)：Epic 2 校准与手动控制复盘。
- [`sprint-artifacts/epic-3-retro-2026-07-30.md`](sprint-artifacts/epic-3-retro-2026-07-30.md)：Epic 3 协议执行、低抖动动作与 session 记录复盘，以及 Epic 4 技术约束。

## Epic 1 Stories

- [`sprint-artifacts/1-0-project-scaffold-and-ci-baseline.md`](sprint-artifacts/1-0-project-scaffold-and-ci-baseline.md)：项目骨架、依赖、CI 与打包基线。
- [`sprint-artifacts/1-1-device-self-check-and-status-report.md`](sprint-artifacts/1-1-device-self-check-and-status-report.md)：配置驱动的 NI/RS232 自检与状态报告。
- [`sprint-artifacts/1-2-safe-start-airflow-interlock.md`](sprint-artifacts/1-2-safe-start-airflow-interlock.md)：安全启动、气流联锁和阀门保护。
- [`sprint-artifacts/1-3-global-safety-toolbar.md`](sprint-artifacts/1-3-global-safety-toolbar.md)：全局连接、复位、停止和帮助动作。
- [`sprint-artifacts/1-4-safe-shutdown-and-valve-reset.md`](sprint-artifacts/1-4-safe-shutdown-and-valve-reset.md)：安全退出、资源释放和阀门复位。
- [`sprint-artifacts/1-5-hardware-simulation-layer-mock-hal.md`](sprint-artifacts/1-5-hardware-simulation-layer-mock-hal.md)：Mock HAL 与无硬件回归测试基础。

## Epic 2 Stories

- [`sprint-artifacts/2-1-real-time-breath-visualization.md`](sprint-artifacts/2-1-real-time-breath-visualization.md)：实时呼吸波形显示。
- [`sprint-artifacts/2-2-threshold-tuning-and-feedback.md`](sprint-artifacts/2-2-threshold-tuning-and-feedback.md)：呼吸阈值调节与反馈。
- [`sprint-artifacts/2-3-valve-matrix-manual-control.md`](sprint-artifacts/2-3-valve-matrix-manual-control.md)：10/20 通道阀门矩阵手动控制。
- [`sprint-artifacts/2-4-flow-rate-controls.md`](sprint-artifacts/2-4-flow-rate-controls.md)：Alicat 流量设定、读取和安全控制。
- [`sprint-artifacts/2-5-variant-aware-pre-test-ui.md`](sprint-artifacts/2-5-variant-aware-pre-test-ui.md)：按设备变体适配的预实验界面。
- [`sprint-artifacts/2-6-automatic-breath-calibration-session.md`](sprint-artifacts/2-6-automatic-breath-calibration-session.md)：自动呼吸校准会话。
- [`sprint-artifacts/2-7-calibration-ui-optimization.md`](sprint-artifacts/2-7-calibration-ui-optimization.md)：校准界面和交互优化。

## Epic 3 Stories

- [`sprint-artifacts/3-1-protocol-file-parsing-txtcsv.md`](sprint-artifacts/3-1-protocol-file-parsing-txtcsv.md)：TXT/CSV 协议文件解析、校验和错误报告。
- [`sprint-artifacts/3-2-breath-gated-stimulation.md`](sprint-artifacts/3-2-breath-gated-stimulation.md)：呼吸门控、TTL、TTL 生存期和 fail-closed 协议执行。
- [`sprint-artifacts/3-3-manual-vs-ttl-trigger-modes.md`](sprint-artifacts/3-3-manual-vs-ttl-trigger-modes.md)：手动与 TTL 触发模式及互斥边界。
- [`sprint-artifacts/3-4-low-jitter-actuation-20ms.md`](sprint-artifacts/3-4-low-jitter-actuation-20ms.md)：owner thread、低抖动动作和真实 NI HIL 基准。
- [`sprint-artifacts/3-5-session-file-naming-and-logging.md`](sprint-artifacts/3-5-session-file-naming-and-logging.md)：session writer、原子发布、recovery 和完整性校验。

## 可审计证据

- [`sprint-artifacts/evidence/story-3-5-hil-closure-20260730.md`](sprint-artifacts/evidence/story-3-5-hil-closure-20260730.md)：Story 3.5 软件 Gate 与真实 NI HIL 最终闭环证据。
- [`sprint-artifacts/evidence/atdd-checklist-3-3-manual-vs-ttl-trigger-modes.md`](sprint-artifacts/evidence/atdd-checklist-3-3-manual-vs-ttl-trigger-modes.md)：Story 3.3 已完成的 ATDD 设计与覆盖映射。
- [`sprint-artifacts/evidence/spec-update-dev3-hardware-inventory-20260730.md`](sprint-artifacts/evidence/spec-update-dev3-hardware-inventory-20260730.md)：Dev3/USB-6501 现场硬件清单校正的冻结规格。
- [`sprint-artifacts/evidence/release-packaging-gate-20260730.md`](sprint-artifacts/evidence/release-packaging-gate-20260730.md)：Windows PyInstaller 干跑、资源审计、哈希和启动烟测证据。

## 归档

- [`archive/FeatureList-legacy.md`](archive/FeatureList-legacy.md)：已停止维护的功能清单历史快照，不作为需求或状态源。
