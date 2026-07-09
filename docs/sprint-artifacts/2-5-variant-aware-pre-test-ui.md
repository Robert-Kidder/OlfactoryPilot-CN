# Story 2.5: Variant-Aware Pre-Test UI

Status: ready-for-review  
Epic: 2 - Calibration & Manual Control  
Story Key: 2-5-variant-aware-pre-test-ui  
Story ID: 2.5

## Story
作为一名实验室技术员，  
我希望预检页面固定支持 20 通道硬件（2x10 阀矩阵，保持现有布局）并正确映射主阀，  
从而让 UI 与实际 20 通道设备一致，避免把指令发到不存在的通道并保持安全。

## Acceptance Criteria
1. **20 通道渲染 (AC1)**：Given 设备配置为 20-channel；When 打开 Pre-test 页面；Then 阀矩阵以 2 行 x 10 列（行优先：第 1 行通道 1-10，第 2 行 11-20）显示 20 个通道且标签顺序与 `config/default_config.json` 中的 20 通道映射一致，未配置的通道隐藏/禁用，流量/状态区可用。[Source: docs/epics.md:257-268, docs/ux-design.md:75-79, config/default_config.json]
2. **主阀常开与安全阻断 (AC2)**：Given 启动设备并切换刺激/非刺激模式；When 写入硬件/Mock；Then 主阀上电常开（不随单个气味通道切换），刺激时主路通向 20 通道，非刺激时通向补偿路径，仍复用 SafetyManager/low_flow 阻断，LOW_FLOW/DATA_STALE/未自检时全部按钮禁用且不发送写入。[Source: docs/sprint-artifacts/2-3-valve-matrix-manual-control.md, docs/sprint-artifacts/1-2-safe-start-airflow-interlock.md]
3. **配置持久化 (AC3)**：Given 使用默认或用户配置；When 重启应用或启用 Simulation Mode；Then 继续使用 20 通道映射与主阀配置，Pre-test UI 与 ValveService 一致渲染，Simulation 模式下同样能显示 20 通道并记录事件。[Source: config/default_config.json, docs/project-context.md:18-44]

## Developer Context (developer_context_section)
- 业务价值：对齐 UI 与实际 20 通道硬件，避免把指令发送到不存在的通道或错误的主阀映射，减少实验前误操作风险。[Source: docs/epics.md:257-268]
- 范围关系：继承 Epic 2 手动控制与 Flow Apply 能力，沿用 FR1.2 安全联锁（低流量阻断）。本故事仅覆盖 20 通道映射，不再支持 10 通道切换。[Source: docs/prd.md:48-51, docs/sprint-artifacts/1-2-safe-start-airflow-interlock.md]
- 现状：PreTestView 初始化时读取 `AppState.get_active_valve_map()`，ValveService 构造时固定 `hardware_variant`；默认配置已含 20 通道映射与主阀。[Source: app/views/main_window.py, app/views/pretest_view.py, app/services/valve_service.py, config/default_config.json]
- 关键风险：映射缺失或与实际布线不符会导致写入失败；低流量或自检未通过时不应触发写入；模拟模式需与真实硬件行为一致；UI 布局（2x10）需要保持现有间距/样式，避免破坏用户已调好的界面；主阀行路需与配置一致（当前默认 master_valve=Dev2/P1.0）。

## Technical Requirements (technical_requirements)
- **配置驱动**：使用 `AppState.hardware_variant`（固定 20-channel）+ `AppState.valve_variants['20-channel']`（来自 `config/default_config.json` 的 `valve_mapping.variants`）作为唯一数据源；未找到映射时禁用矩阵并显示中文错误，绝不发送写入。[Source: config/default_config.json, app/models/app_state.py]
- **UI 渲染**：PreTestView 使用 2x10 按钮矩阵（保持现有布局/间距/样式），标签与映射一致；未配置通道隐藏/禁用。保持 Flow Apply、阈值与波形显示不变。
- **服务层**：ValveService 使用 20 通道映射写入，遇到未配置通道返回友好信息并记录日志。主阀保持常开（不随单通道联动），默认行路来自配置（master_valve=Dev2/P1.0），刺激/补偿路径由流量模式决定。
- **安全联锁**：所有阀写入继续复用 `SafetyManager.guard_command`，低流量/数据过期/未自检时阻断；PreTestView 禁用按钮并提示原因。
- **持久化与恢复**：`AppState.from_config` 读取 `hardware_variant` 与 20 通道映射，在窗口初始化时带入 PreTestView/ValveService；重启或 Simulation 模式下保持一致配置。

## Architecture Compliance (architecture_compliance)
- 保持 MVC + Worker 线程；映射解析在 Controller/Service，View 仅展示；硬件写入在 `HardwareWorker`/HAL，UI 不直接调用底层。
- 继续使用 SafetyManager/SafetyState 判定，遵循 5-10Hz 安全状态推送与 LOW_FLOW <500ms 告警；不绕过安全守卫。
- 日志复用现有 logger（如 `valve_events`、状态栏）；不新增全局单例或破坏目录结构（`app/controllers|services|views|models`）。

## Libraries / Versions (library_framework_requirements)
- Python 3.11；PySide6 6.7.2，pyqtgraph 0.13.7；nidaqmx 0.9.0，pyserial 3.5。保持现有版本，20 通道仅涉及 UI/控制逻辑，无需升级依赖。[Source: docs/architecture.md:32-37]

## File & Implementation Plan (file_structure_requirements)
- `app/views/pretest_view.py`：渲染 20 通道矩阵与主阀指示，保留波形/Flow Apply/阈值 UI，继承安全禁用态。
- `app/views/main_window.py`：确保初始化时使用 20 通道映射；如存在 Options，默认仅展示/保存 20 通道，不做切换。
- `app/controllers/main_controller.py`：确保构造与绑定时传递 20 通道映射；必要时在启动/重载配置后重建 PreTestView。
- `app/services/valve_service.py`：使用 20 通道映射写入，master 逻辑保持；未配置通道返回友好信息。
- `app/models/app_state.py`：确保 `hardware_variant` 默认为 20-channel，`valve_variants` 包含完整 20 通道映射。
- `config/default_config.json`：确认 20 通道映射完整且主阀配置正确。
- `tests/`：新增/调整单测覆盖 20 通道写入、master 联动与安全阻断。

## Testing Requirements (testing_requirements)
- **Unit - ValveService**：20 通道写入成功；未映射通道返回阻断；master 联动正确（Dev2/P1.0）；低流量 SafetyState 仍阻断。
- **Unit - PreTestView**：按钮数量/标签匹配 2x10 映射（行优先 1-10 / 11-20，保持现有布局/间距），安全禁用态保持；Flow Apply/阈值显示正常。
- **Integration (MockHAL/QTest)**：Simulation 模式下打开/关闭通道，验证 master 联动、LOW_FLOW 时按钮禁用且不写入。
- **Persistence**：重启/重新加载配置后仍使用 20 通道映射，Flow Apply（Story 2.4）路径不受影响。

## Tasks (tasks_subtasks)

- [x] 配置驱动与持久化（AC1/AC3）：AppState/配置确保 hardware_variant=20-channel，映射缺失时禁用矩阵并提示中文错误；模拟模式沿用同一映射与主阀行路。
- [x] UI 渲染与安全态（AC1/AC2）：PreTestView 以 2x10 渲染映射标签，未配置通道隐藏/禁用；LOW_FLOW/DATA_STALE/未自检时禁用按钮并提示；主阀指示跟随。
- [x] 服务与控制器逻辑（AC2）：ValveService 继续用 SafetyManager.guard_command，未映射通道友好阻断，主阀常开不随单通道切换；MainController 传递映射/主阀并同步 UI 状态。
- [x] 测试覆盖（AC1/AC2/AC3）：新增/调整单测覆盖映射缺失阻断、20 通道渲染、主阀常开与模拟模式持久化。

## Previous Story Intelligence (previous_story_intelligence)
- **2.3 Valve Matrix**：已将阀映射配置化、引入 master valve 联动与 SafetyManager.guard_command；UI 使用 `get_active_valve_map()` 构建矩阵。[Source: docs/sprint-artifacts/2-3-valve-matrix-manual-control.md, app/services/valve_service.py]
- **2.4 Flow Rate Controls**：Flow Apply 复用 PreTestView 与 SafetyManager；Apply 按钮在 LOW_FLOW/DATA_STALE 下禁用，需保持该阻断行为。[Source: docs/sprint-artifacts/2-4-flow-rate-controls.md, app/controllers/main_controller.py]
- **1.2 Safe Start Interlock**：所有 Pre-test/Protocol/Flow Apply 入口必须经统一安全校验，低流量时禁止写入；20 通道写入应延续同一路径。[Source: docs/sprint-artifacts/1-2-safe-start-airflow-interlock.md]

## Git Intelligence Summary (git_intelligence_summary)
- 最近提交集中在校准/阈值与 UI 优化（Story 2.2/2.7），并更新了项目上下文与模拟策略；保持现有 MVC + SafetyManager + MockHAL 结构，20 通道映射应复用这些模式。[Source: git log -5]

## Latest Tech Information (latest_tech_information)
- 当前库版本满足需求；若评估升级 PySide6/pyqtgraph/nidaqmx，需要验证 PyInstaller 打包与 100Hz 绘制性能，默认不升级。

## Project Context Reference (project_context_reference)
- docs/epics.md:257-268（Story 2.5 AC）  
- docs/prd.md:48-58（FR7.3/模拟模式约束）  
- docs/ux-design.md:75-79（Options 页相关交互）  
- docs/project-context.md:18-44（硬件映射与架构约束）  
- config/default_config.json（硬件变体/阀映射示例）

## Completion Status (story_completion_status)
- 状态：ready-for-review  
- 备注：Ultimate context engine analysis completed - comprehensive developer guide created

## Dev Agent Record

### Context Reference
- app/models/app_state.py  
- app/services/valve_service.py  
- app/controllers/main_controller.py  
- app/views/pretest_view.py  
- config/default_config.json  
- docs/epics.md

### Agent Model Used
- OpenAI GPT-4.1

### Debug Log
- 修正配置驱动：AppState 缺少映射时回退 20-channel 并告警，PreTestView 在无映射时禁用控件并提示。
- 完善阀门安全链路：ValveService 缺少映射直接阻断，主阀改为常开（不随单通道切换），MainController 阻断无映射开关请求并同步 master LED。
- UI/测试：预检视图保持 2x10 渲染与警示文案，新增/扩展单测覆盖缺失映射、20 通道按钮数量与主阀常开状态，pytest 全量通过。

### Completion Notes
- 运行 `python -m pytest` 全部 96 项通过。
- 预检 UI 在配置缺失时安全退化且提示中文错误；正常配置下渲染 20 通道并保持主阀常开可视状态。

## File List
- app/models/app_state.py
- app/controllers/main_controller.py
- app/services/valve_service.py
- app/views/pretest_view.py
- tests/test_valve_service.py
- tests/test_pretest_view.py
- docs/sprint-artifacts/2-5-variant-aware-pre-test-ui.md
- docs/sprint-artifacts/sprint-status.yaml

## Change Log
- 为缺失的阀门映射增加 20-channel 回退与前端禁用提示，避免误写入。
- 阀门服务在无映射时阻断，主阀保持常开并同步到 UI，控制器阻断无映射的开关请求。
- 预检视图 2x10 渲染保持、中文提示完善，新增单测覆盖映射缺失与主阀常开状态；全量 pytest 通过。

