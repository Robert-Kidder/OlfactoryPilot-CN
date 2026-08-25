# Sprint Change Proposal：安全停止与新版手动界面（精简修订版）

**项目：** OlfactoryPilot-CN
**日期：** 2026-08-17
**版本：** Revision 2
**状态：** Approved and Applied
**变更范围：** Moderate
**推荐路径：** 保留 Epic 4，只增加两个必要 Story；默认使用 Quick Dev
**批准：** Jing 于 2026-08-17 明确回复 `Approve`

## 0. 修订说明

上一版把技术研究路线图直接转换成 Epic 4–7 和大量 Story，治理粒度过细，会增加文档、交接、审查和状态维护成本。本版接受 Jing 的反馈：**路线图不是 backlog，技术组件也不等于 Story。**

本版不新建 Epic 5–7，不把自动实验或呼吸传感器 UI 提前拆成 Story，也不要求重新运行一整套 PRD → UX → Architecture → Epic → Story 工作流。当前只处理真正阻断开发的两个交付单元。

## 1. Issue Summary

### 1.1 触发问题

当前 Story 4.1「自动清洗流程」仍以“20 个气味阀 + 1 个可关闭主阀，共 21 个关闭目标”为安全模型。实机证据已确认 `Dev2/P1.0` 是 A 路三通选择阀：低电平选择补偿出口，高电平选择气味阀 1–20 总入口，没有独立全关态。

因此现有全局停止顺序必须先修正为：

1. 阻止新动作并失效旧 epoch；
2. 请求 A 清零；
3. 收到匹配的 A=0 成功 receipt；
4. 才允许把三通阀切到定义的安全路线；
5. 其余气味阀、B/C、owner handoff 和资源释放顺序由一个简短安全 Story 固化并验证。

与此同时，最终技术研究和已确认方案 B V3 要求替换旧 `PreTestView`，增加可持久化气口映射，并把手动刺激计时交给 `ActuationWorker`。这些变化可以作为**一个纵向 UI/业务重构 Story**完成，不需要为每个模型、控件或内部层分别建立 Epic。

### 1.2 本次不做的事情

- 不迁移 Python、PySide6、pyqtgraph、nidaqmx、pyserial 或 PyInstaller。
- 不提供新旧界面切换。
- 不开发呼吸传感器操作 UI。
- 不开发自动实验；只保留“以后复用同一执行核心”的架构约束。
- 不回滚或删除当前未提交工作。
- 本提案阶段不修改产品代码、不操作硬件。

## 2. Epic 4 与 Story 4.1 的处理决定

### Epic 4

**保留 Epic 4，不新增其他 Epic。**

Epic 4 的标题调整为“运行安全、新版手动实验与交付收口”。原有目标中的配置、本地化、补偿和清洗仍属于该 Epic，但不再强制拆成彼此独立的完整流程。

### 当前 Story 4.1

**暂停，不回滚，不按旧 AC 继续 HIL。**

保留：

- Worker/HAL 单 Owner；
- lease、generation/epoch、receipt identity；
- ActuationWorker monotonic deadline 和紧急抢占；
- `possibly_open`、fail-closed、显式 recovery、owner handoff；
- `maintenance-v1` bundle、SessionWriter 单写者和现有自动化测试；
- 探索性 HIL 证据及其“不证明机械动作/出口映射”的边界。

待后续修正：

- 所有 21-target / 第 21 只主阀假设；
- 先 DO 全低、后 A 清零的停止顺序；
- 写死的软件通道—机外标签—NI 线路关系；
- 清洗 UI 对旧通道模型的依赖。

完成安全停止 Story 后，再决定是用一个 Quick Dev 任务收尾 Story 4.1，还是把它保持暂停到新版界面完成。现在不继续拆分清洗 Story。

## 3. 精简后的实施计划

### Story 4.5：全局停止顺序与三通阀模型（P0）

这是唯一必须先单独实施的安全 Story。

验收标准：

1. `Dev2/P1.0` 作为独立二选一路由 selector 建模，不属于气味阀 1–20，也不称为第 21 只普通阀。
2. 所有全局停止、异常停止和 shutdown 路径保证：A 清零成功 receipt 先于 selector 切换安全路线。
3. A 清零失败、超时、迟到/冲突 receipt 或 selector 状态不确定时进入明确的 `RECOVERY_REQUIRED`，不报告“已安全停止”。
4. 保留现有 Worker/HAL/lease/epoch/receipt 和 owner handoff，不重写硬件底层。
5. 自动化测试和经单独授权的真实 HIL 证明顺序；本 Story 之外的 UI 工作不得先接入真实硬件。

### Story 4.6：方案 B V3 手动实验替换（一个纵向 Story）

本 Story 内部可以按任务逐步提交，但不再拆成多个 Epic/Story。

验收标准：

1. 固定使用 Python 3.11、PySide6 Widgets、pyqtgraph 和现有 Worker/HAL。
2. 产品只保留新版手动实验入口，不提供 legacy 开关。
3. 固定显示机外气口 1–20；未接入位置变灰。可用性来自可持久化 HardwareProfile，不能写死在 View。
4. 设置中可视化配置“机外气口 → 内部控制阀位 → NI 线路”，支持显示名称、单气口验证、自然验证状态和跨启动保存。
5. 初始映射为：机外 2/4/6/8/12/14/16/18 对应内部阀位 2–9；对应 NI target 为 `Dev1/P0.1` 至 `Dev1/P1.0`。`Dev2/P1.0` 单独作为 selector。
6. 手动刺激从全部目标 open receipt 的共同就绪时刻起算，由 `ActuationWorker` monotonic deadline 自动结束；UI `QTimer` 只刷新显示。
7. 手动与未来自动实验共享同一硬件执行核心；当前不实现自动实验。
8. 当前不提供呼吸传感器操作 UI；既有底层能力可保留，不在主界面留下占位入口。
9. 新链通过 Mock、UI 和适用 HIL 后，删除旧 `PreTestView`、旧 View 计时、重复状态、无用弹窗和只服务旧页面的代码。
10. 方案 B V3、设置页和错误状态使用简体中文；当前只有 A 路 telemetry 时，不把曲线或状态误称为 A+B 总流量稳定。

### 现有 Story 4.2–4.4

- 4.2 的配置能力并入 Story 4.6。
- 4.3 的中文和乱码要求作为 Story 4.6 的横切验收，不再单独跑完整开发周期。
- 4.4 的 Owner/receipt/补偿能力并入 Story 4.6；“master valve”改为 selector 语义。
- 三项在 Sprint Status 中标记为 `superseded-by-4-6`，保留历史文本，不删除记录。

### 自动实验与呼吸 UI

只在 PRD 的 Future Scope 中各保留一条，不创建 Epic 或 Story。等真正开始时再写一个清晰需求并直接使用 Quick Dev；如果届时出现新的硬件或安全不变量，再单独走 Correct Course。

## 4. 规划资料的最小更新

不逐份运行大型工作流。批准后用一次文档修改同步以下内容：

| 文件 | 最小修改 |
|---|---|
| `docs/prd.md` | 增加停止顺序、HardwareProfile、V3 手动实验；自动实验和呼吸 UI 标为 Future/Deferred |
| `docs/ux-design.md` | 以方案 B V3 替换旧预实验说明；增加固定 1–20、设置映射、验证状态和无旧 UI 切换 |
| `docs/architecture.md` | 修正 selector/停止顺序；增加 HardwareProfile 与 receipt 起算；删除 21-target 架构表述 |
| `docs/epics.md` | 保留 Epic 4；暂停旧 4.1；增加 4.5、4.6；4.2–4.4 标记并入 4.6 |
| `docs/sprint-artifacts/sprint-status.yaml` | 4.1 暂停；4.2–4.4 superseded；4.5 ready-for-dev；4.6 backlog |

更新顺序：

1. 批准本提案；
2. 一次性同步上述五份文档；
3. 只读一致性检查；
4. 用 Quick Dev 实施 Story 4.5；
5. Story 4.5 测试/HIL 通过后，用 Quick Dev 实施 Story 4.6；
6. 再决定是否恢复并收尾 Story 4.1。

不再要求单独运行 PRD、UX、Architecture、Create Epics、Readiness、Create Story、Validate Story 六轮流程。

## 5. 开发流程调整

### 默认路径：BMAD Quick Dev

本项目后续默认使用 `[QQ] Quick Dev`（`bmad-quick-dev`）：一条明确需求进入，代理完成必要澄清、计划、实现、自检和交付。BMAD 官方把它定义为“Intent in, code changes out”，并明确用于减少人工往返；小修复、局部重构和明确功能可以直接进入 Quick Dev。

执行规则：

- 普通 UI、文案、局部重构、明确 bug：直接 Quick Dev，不创建 Story 文件，不单独跑 Validate Story/Code Review/Retrospective。
- 一般多文件功能：Quick Dev + 自动测试；用户验收一次。
- 安全关键硬件路径：一个简短 Story/Spec + Quick Dev + 一次独立代码审查 + 授权 HIL。
- 审查发现实质问题才进入修复循环；不为“流程完整”进行空转审查。
- 只有产品范围、硬件事实或安全不变量真正变化时才运行 Correct Course。

### 是否替换 BMAD

**当前不建议替换。** 问题主要是使用了完整流程和过细拆分，而不是 BMAD 无法轻量运行。项目已经有 BMAD 文档、状态和 Quick Dev，迁移会增加一套新目录和规则。

如果执行 Story 4.5 和 4.6 后仍觉得流程过重，再考虑 OpenSpec。它以单个 change 文件夹组织 proposal/spec/tasks，定位为 brownfield 的轻量 agreement layer；但现在迁移没有即时收益。

其他候选不更轻：

- GitHub Spec Kit 默认仍是 Spec → Plan → Tasks → Implement，并带评审门禁。
- Superpowers 强调设计、计划、TDD、子代理执行和代码审查。
- GSD 仍有 discuss → plan → execute → verify/review/ship 的阶段链。
- 完全不用框架最轻，但会失去当前硬件安全决策和证据追踪，不适合本项目的关键路径。

官方参考：

- [BMAD Quick Dev](https://docs.bmad-method.org/explanation/quick-dev/)
- [BMAD Quick Fixes](https://docs.bmad-method.org/how-to/quick-fixes/)
- [OpenSpec](https://github.com/Fission-AI/OpenSpec)
- [GitHub Spec Kit](https://github.github.com/spec-kit/)
- [Superpowers](https://github.com/obra/superpowers)
- [GSD](https://github.com/gsd-build/get-shit-done)

## 6. Worktree Protection

- 保留当前所有未提交与未跟踪文件。
- 不执行 `git reset --hard`、`git checkout -- <path>`、`git clean` 或全仓格式化。
- 后续实施前，在用户授权下建立 WIP commit、独立分支或补丁归档中的一种非破坏性基线。
- Story 4.1 的现有实现先由测试和引用清单保护，再决定如何复用；不因规划简化而直接删除。

## 7. Handoff 与成功标准

范围分类从 Major 下调为 **Moderate**。

无需多角色串行交接。建议由同一个开发代理按以下方式完成：

1. 文档同步；
2. Quick Dev：Story 4.5；
3. 一次安全代码审查和授权 HIL；
4. Quick Dev：Story 4.6；
5. 用户验收新版界面；
6. 决定 Story 4.1 的收尾。

成功标准：

- 规划中不再出现 selector 是第 21 只可关闭阀的表述。
- P0 停止顺序有自动化与 HIL 证据。
- 新版 UI 固定显示 1–20，并从持久配置决定可用位置和映射。
- 手动刺激由 ActuationWorker 从 open receipt 起算并自动结束。
- 最终只有新版界面；旧 UI、View 计时和无用弹窗已删除。
- 自动实验和呼吸 UI 没有提前扩大当前工作量。
- 当前工作树未被破坏。

## 8. Correct Course Checklist

| 项目 | 状态 | 结论 |
|---|---|---|
| Trigger / Evidence | [x] | Story 4.1 的 21-target 假设被实机证据推翻 |
| Epic Impact | [x] | 保留 Epic 4，不新增 Epic |
| Story Impact | [x] | 暂停 4.1；新增 4.5/4.6；4.2–4.4 并入 4.6 |
| PRD / UX / Architecture | [x] | 五份文档一次性最小同步 |
| Direct Adjustment | [x] | 采用，避免大规模重排和回滚 |
| MVP Review | [x] | 自动实验、呼吸 UI 延后，但不创建 backlog 噪声 |
| Worktree Protection | [x] | 不执行破坏性 Git 操作 |
| User Approval | [x] | Jing 于 2026-08-17 明确批准 Revision 2 |
| Sprint Status Update | [x] | 4.1 paused；4.2–4.4 superseded；4.5 ready-for-dev；4.6 backlog |

## 9. Approval Record and Next Step

本提案已批准，并已同步 `docs/prd.md`、`docs/ux-design.md`、`docs/architecture.md`、`docs/epics.md` 与 `docs/sprint-artifacts/sprint-status.yaml`。本次没有修改产品代码或操作硬件。

下一步在新的上下文中使用 `[QQ] Quick Dev` 为 Story 4.5 创建精简实施规格并实现软件部分；任何真实 HIL 必须由 Jing 另行明确授权。
