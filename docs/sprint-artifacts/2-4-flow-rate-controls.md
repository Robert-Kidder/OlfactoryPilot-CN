# Story 2.4: Flow Rate Controls（流量控制）

Status: ready-for-review  
Epic: 2 - 校准与手动控制  
Story Key: 2-4-flow-rate-controls  
Story ID: 2.4

## Story Requirements (story_requirements)

**用户故事**  
作为一名实验技术员，  
我想为载气（B）、排空（C）和气味/补偿（A）设置目标流量并点击“应用”，  
从而在气流联锁保护下安全、可重复地完成校准和预检。

**验收标准（来自 epics.md / PRD）**  
- AC1 低流阻断：SafetyState != SAFE（气流 < low_flow_threshold 或 DATA_STALE）时点击 Apply，阻断并提示“LOW FLOW/气流不足”，且不发送任何 RS232/MFC 命令。  
- AC2 应用与反馈：已输入 A/B/C 且 SafetyState=SAFE 时点击 Apply，发送 MFC 指令；成功提示“流量已应用”，失败显示具体错误（串口不可用/写入失败/超时），并禁用按钮期间防抖。  
- AC3 补偿可见：Rest/无刺激时，UI 显示 `A_comp = A_target + C_target`；发送顺序遵循“先设 MFC，再切主阀”，保证出口总流量平衡。

## Developer Context (developer_context_section)

- 业务价值：Pre-test 流量设定安全化，满足 FR4.2/FR4.3，同时继续遵守 FR1.2 安全联锁。  
- 用户流：输入 A/B/C → Apply → 若 SAFE 下发 MFC → 成功/错误反馈 → Rest 显示并应用 A_comp。  
- 关联故事：2.3 完成阀阵列+主阀联动；4.4 定义 Rest/Stim 补偿顺序。保持主阀逻辑不变，仅补充流量控制与补偿。  
- 安全：沿用 SafetyManager/HardwareWorker 气流监测；SafetyState != SAFE 时 Apply 置灰/提示，硬阻断命令。

## Technical Requirements (technical_requirements)

- 控制路径：PreTestView(Apply 信号) → MainController → Flow/MFC 逻辑 → HardwareWorker/HAL.set_flow 或 RS232 写入。  
- 安全门控：Apply 前检查 SafetyState（LOW_FLOW/DATA_STALE 等）；阻断时仅提示、不写硬件，按钮置灰并 Tooltip 原因。  
- 补偿：Rest 状态使用 `A_comp = A_target + C_target`；显示补偿值；执行顺序遵循 4.4：先设 MFC，再切主阀。  
- 反馈与日志：成功/失败均更新状态栏；失败包含错误原因；记录到 `flow_events`（或沿用 `valve_events`）payload：ts, mode(rest/stim), a/b/c/a_comp, result, error。  
- 线程：硬件调用在 Worker/HAL，UI 仅收信号回写状态，避免阻塞。  
- 仿真一致性：物理经 RS232，仿真走 MockHAL，但接口一致（set_flow）。  
- RS232 命令契约（参考 docs/ALICAT-MANUAL.md）：  
  - 串口：19200 8N1，无流控，超时≥100ms，设备 ID 用 A/B/C 对应三路 MFC。  
  - 设定：`{ID}s{float}<CR>`，例 `as500.0\r`；负值前置 `-`。  
  - 读取/确认：可轮询 `{ID}<CR>` 得实时数据行（含设定值/实测流量）。  
  - 失败处理：串口打开/写入/超时需回传错误，不更新“已应用”值，可重试 1 次后提示用户检查连接。

## Architecture Compliance (architecture_compliance)

- MVC：View 发信号，Controller 处理安全/补偿/命令，Worker/HAL 执行。  
- 不绕过 SafetyManager；气流阈值取自 AppState.low_flow_threshold。  
- 主阀联动继续由 ValveService 处理，本故事不更改其策略。  
- Windows 10/11，PySide6 + Worker 线程分离。

## Flow State Machine (architecture_compliance supplement)

- Rest（无刺激）：设定 B=目标、C=目标、A_comp=A+C；主阀旁路/关闭。  
- Stim 开始：先设 B=目标（保持）、设 A=A_target、设 C=0；再打开主阀（P1.0）。  
- Stim 结束：先恢复 A_comp 与 C，再关闭主阀；如写入失败提示并保持安全，可重试 1 次。

## Libraries / Versions (library_framework_requirements)

- PySide6 6.7.2，pyqtgraph 0.13.7，Python 3.11。  
- 硬件/通讯：nidaqmx 0.9.0，pyserial 3.5。  
- 以稳定为主，维持当前版本。

## File & Implementation Plan (file_structure_requirements)

- `app/views/pretest_view.py`：Apply 按钮发信号；显示补偿 A_comp；应用中禁用按钮；显示当前气流与“已应用”A/B/C/A_comp；成功/错误提示。  
- `app/controllers/main_controller.py`：处理 Apply（安全门控、补偿计算、调用 Flow/MFC、回写状态/提示/flow_events）。  
- `app/services/hal.py` / `app/services/mock_hal.py`：暴露/实现 set_flow(A/B/C, *, comp=False)；MockHAL 记录调用返回 True。  
- 可选 `app/services/flow_service.py`：封装 pyserial/Mock 分支与日志。  
- 配置：`config/default_config.json`（默认流量、阈值、串口、超时可选）。  
- 测试：`tests/test_app.py`、`tests/test_valve_service.py`，可增 `tests/test_flow_controls.py`。

## Testing (testing_requirements)

- 单测：  
  - 安全阻断：SafetyState != SAFE 时 Apply 不触发 set_flow，返回阻断文案。  
  - 补偿计算：给定 A/B/C，Rest 下生成正确 A_comp 并调用 set_flow。  
  - 成功/失败路径：Mock set_flow True/False/异常/超时，状态消息正确，失败不更新“已应用”值并记录 flow_events。  
- 集成（MockHAL）：模拟 Apply → Controller → Flow 服务 → 回写 UI，验证补偿显示、按钮恢复、flow_events 记录。  
- 回归：Story 2.3 阀阵列与主阀联动不受影响。

## Previous Story Intelligence (previous_story_intelligence)

- 2.3：20/10 通道映射配置化；主阀当前运行配置为 Dev2/P1.0；安全守卫阻断不安全写入；PreTestView 阵列与状态 LED；ValveService 统一走 HardwareWorker.write_digital。  
- PreTestView 已有 MFC A/B/C 输入与 Apply 按钮，但未接入安全/RS232/补偿。

## Tasks (tasks_subtasks)

- [x] UI/交互（AC1/AC2/AC3）：Apply 信号，显示/更新 A_comp=A+C，应用中禁用；安全异常置灰并提示；显示当前气流与已应用 A/B/C/A_comp；成功/错误提示。  
- [x] 安全门控（AC1）：Controller 检查 SafetyState，非 SAFE 直接返回阻断文案，不触发硬件。  
- [x] 流量指令管道（AC2/AC3）：Flow/MFC 服务 set_flow/set_comp；MockHAL 与 pyserial 分支；错误覆盖串口未连/写入异常/超时，不更新已应用值。  
- [x] Rest/Stim 顺序（AC3）：Rest 先设 MFC 后主阀；Stim 结束恢复 Rest 顺序。  
- [x] Mock/测试（AC1/AC2/AC3）：单测安全阻断、补偿计算、成功/失败/超时、flow_events 记录；集成链路含 UI 恢复。  

## Project Context Reference (project_context_reference)

- `docs/epics.md`（Story 2.4 验收）  
- `docs/prd.md`（FR4.2/FR4.3、FR1.2） 
- `docs/architecture.md`（MVC + Worker + MockHAL） 
- `docs/ux-design.md`（Pre-test 布局） 
- `docs/project-context.md`（硬件映射与安全指标） 
- `config/default_config.json`（阈值、设备、串口）

## Completion Status (story_completion_status)

- 状态：ready-for-review  
- 备注：Ultimate context engine analysis completed - comprehensive developer guide created。

## Dev Agent Record

### Debug Log
- 完成任务 1（AC1/AC2/AC3）：补充 Apply 信号流程、A_comp 预览/已应用显示、气流与安全阻断提示，Apply 防抖禁用。
- 完成任务 2（AC1）：安全门控前置，SafetyState 非 SAFE/数据过期直接阻断 Apply 并提示 LOW FLOW/气流不足。
- 完成任务 3（AC2/AC3）：FlowService 封装 A/B/C 顺序写入与补偿，MockHAL 记录通道调用，失败不更新已应用值。
- 完成任务 4/5（AC3）：Rest/Stim 序列落地（先写 MFC 再切主阀），结束恢复 Rest；新增测试覆盖主阀开闭与顺序。

### Completion Notes
- PreTestView 新增 Apply 按钮与 A_comp 计算，安全状态置灰并展示当前气流与已应用流量；MainController 接入 FlowService，安全阻断返回“LOW FLOW/气流不足”；FlowService/MockHAL 支持 A/B/C 写入与日志；tests/test_flow_controls 覆盖低流阻断、成功/失败路径。

## File List
- app/controllers/main_controller.py
- app/models/app_state.py
- app/services/flow_service.py
- app/services/hal.py
- app/services/mock_hal.py
- app/services/__init__.py
- app/views/main_window.py
- app/views/pretest_view.py
- tests/test_flow_controls.py

## Change Log
- 实现 FlowService 封装 MFC 写入与日志，MockHAL/接口兼容 A/B/C 通道与补偿标记。
- PreTestView 增加 Apply 交互、安全禁用、气流/已应用显示，MainController 贯通安全前置与状态反馈。
- Rest/Stim 序列实现（MFC -> 主阀），结束恢复 Rest；新增 tests/test_flow_controls 覆盖主阀开闭顺序；全量 pytest 通过。

