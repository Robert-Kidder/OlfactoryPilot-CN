# OlfactoryPilot-CN 实施就绪检查报告

检查日期：2025-12-08  
最近校正：2026-07-09  
项目：OlfactoryPilot-CN

## 结论

项目已具备继续实施的基础条件。PRD、UX、架构、Epic/Story 拆分和 sprint 状态文件已经存在，并已在 2026-07-09 统一为中文主线文档和 Python 3.11 工程基线。

当前建议进入 Epic 3 “协议执行与数据记录”，优先开发 Story 3.1 “协议文件解析 .txt/.csv”。

## 已确认资料

- 产品需求：`docs/prd.md`
- UX 设计：`docs/ux-design.md`
- 架构设计：`docs/architecture.md`
- Epic 与 Story：`docs/epics.md`
- 项目上下文：`docs/project-context.md`
- 项目结构说明：`docs/project-structure.md`
- Sprint 状态：`docs/sprint-artifacts/sprint-status.yaml`
- 原法国软件说明书：`docs/ManuelUtilisation_ProgOlfacto.pdf`

## 技术基线

- Python 版本：3.11。
- 当前开发解释器：`D:\miniconda3\envs\code\python.exe`。
- GUI：PySide6。
- 图形：pyqtgraph。
- 硬件：nidaqmx、pyserial。
- 测试：pytest、pytest-qt。
- 代码检查：ruff，目标版本 `py311`。
- 打包：PyInstaller。
- CI：GitHub Actions，Windows 环境。

## 覆盖情况

- FR1 安全硬件基础：已在 Epic 1 覆盖。
- FR2 文件与会话：由 Epic 3 覆盖，尚待实现。
- FR3 呼吸校准：已在 Epic 2 覆盖。
- FR4 手动阀门与流量控制：已在 Epic 2 覆盖。
- FR5 协议执行：由 Epic 3 覆盖，尚待实现。
- FR6 清洗：由 Epic 4 覆盖，尚待实现。
- FR7 配置与中文本地化：由 Epic 2 和 Epic 4 覆盖，仍需继续完善。

## 主要风险

- 旧法国软件协议文件格式需要从 PDF、样例文件或真实实验文件中进一步确认。
- 真实硬件行为与 Mock HAL 之间可能存在差异，需要继续保留硬件验证记录。
- 协议执行和 TTL 触发涉及时序要求，开发时必须补充自动化测试和日志证据。
- 文档历史记录中仍可能保留旧开发阶段描述，后续修改相关 story 时应同步修正。

## 建议下一步

1. 使用 `bmad-sprint-status` 确认 sprint 状态和下一 story。
2. 使用 `bmad-create-story` 为 `3-1-protocol-file-parsing-txtcsv` 创建或更新开发用 story。
3. 使用 `bmad-dev-story` 实现协议解析、错误定位和测试。
