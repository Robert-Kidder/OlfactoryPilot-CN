# 故事 1.2: Safe Alicat Flow Command and Valve Interlock
Status: real-hardware-pass
Epic: 1 - Safe Hardware Foundations
Story Key: 1-2-safe-start-airflow-interlock
Story ID: 1.2

## Story
作为实验室技术员，  
我希望系统在硬件自检通过后，能够安全设置 Alicat A/B/C 目标流量，并确保气味阀、总阀和协议动作只在正确的 MFC 设定顺序之后执行，  
从而避免在串口异常、MFC 设定失败或阀门顺序错误时造成错误刺激或硬件风险。

## Acceptance Criteria
1. **自检前阻断（AC1）**：硬件自检未通过时，所有 Alicat MFC、阀门、总阀、加热和协议动作均被阻断；UI 给出中文原因与恢复建议。
2. **Idle 状态合法（AC2）**：设备启动后默认无气流是正常 idle 状态，不应显示为 LOW FLOW 故障，也不应阻止用户设置 Alicat A/B/C 目标流量。
3. **MFC 设定与反馈追踪（AC3）**：用户设置 Alicat A/B/C 目标流量时，系统发送对应 RS232 setpoint 命令，并记录 channel、target、feedback/readback、result、timestamp；失败时显示中文错误。
4. **阀门顺序互锁（AC4）**：气味阀、总阀或协议刺激动作只能在必要 MFC setpoint 已成功建立后放行；若 Alicat 串口失败、setpoint 失败或反馈异常，则阻断阀门/协议动作。
5. **安全恢复（AC5）**：Stop、Reset、退出或硬件异常时，系统将阀门关闭，并将 MFC 恢复到安全 idle 或配置的安全默认值；恢复结果写入日志。
6. **接口兼容性（AC6）**：Pre-test 阀矩阵、Flow Rate Apply、Protocol、Connect/Reset/Stop 等入口均复用统一安全守卫，不允许 UI 或业务流程直接绕过 HAL/Service 发送危险命令。

## Tasks / Subtasks
- [x] 移除“默认无气流 = LOW FLOW 故障”的产品假设（AC2）。
- [x] 调整安全守卫：MFC 目标流量设定在 idle 状态下允许执行，阀门/协议动作仍需严格守卫（AC1/AC4/AC6）。
- [ ] 为 Alicat A/B/C setpoint 增加结果追踪与日志字段（AC3）。
- [x] 增加阀门顺序互锁：MFC setpoint 成功后才能开启对应气味阀/总阀/协议刺激（AC4）。
- [ ] 调整 Stop/Reset/退出恢复策略，明确 MFC 安全 idle 或默认值（AC5）。
- [ ] 更新 UI 文案：idle/未供气不显示 LOW FLOW；串口/setpoint/反馈异常才显示错误或阻断原因（AC2/AC3/AC4）。
- [x] 更新单元/集成测试，覆盖 idle 允许设流量、setpoint 失败阻断阀门、Stop 恢复 MFC/阀门（AC1-AC6）。

## Dev Notes（story_requirements）
- 覆盖 FR1.2（安全互锁）并支撑 FR1.1/FR1.3/FR1.4：所有危险硬件动作必须经过安全检查；MFC 建立流量是从 idle 进入可操作状态的合法动作。
- 真实硬件语义：Alicat 设定多少流量就应输出多少；默认无气流是 idle，不是 LOW FLOW 故障。
- 安全状态应区分 idle、hardware_not_ready、mfc_command_failed、feedback_mismatch、ready_for_valve 等语义，避免用 LOW FLOW 覆盖所有场景。
- 阀门/加热/协议命令需带上调用来源（Protocol/Pre-test/Flow Apply/Reset），以便日志追踪与 UI 提示。
- 容错：Alicat 串口不可用、setpoint 命令失败、反馈异常或超时，应阻断阀门/协议动作并提示恢复步骤。

## Developer Context（developer_context_section）
- 架构：MVC + Worker（docs/architecture.md）；硬件安全逻辑在 Worker/Service，UI 为被动视图。
- 硬件：NI-USB-6001/6501 + RS232 Alicat 质量流量控制器；A/B/C MFC setpoint 与反馈是流量安全的主要依据。
- UI/UX：全中文；全局 Footer 显示连接状态、MFC/阀门安全状态和最近错误；idle 不显示 LOW FLOW 故障。
- 性能：MFC 命令和阀门动作不阻塞 UI；状态更新 5-10Hz；协议/阀门动作必须读取最新安全状态。
- 日志：安全事件写入数据记录通道（含时间戳、MFC channel、target、feedback、阀/总阀目标、命令来源、判定状态、原因）。
- 配置：串口/NI/MFC ID、安全 idle/default flow 配置在 `config/default_config.json`/Options 页面。

## Technical Requirements（technical_requirements）
- **安全判定**：重构/扩展安全状态，支持 idle、hardware_not_ready、mfc_ready、mfc_error、valve_allowed、valve_blocked 等真实硬件语义；不再用 LOW FLOW 表示 idle。
- **MFC 命令**：Flow Apply 调用 Alicat setpoint；记录 A/B/C target 与 feedback/readback，失败时返回错误码与中文提示。
- **命令守卫**：MFC setpoint 在硬件自检通过后允许执行；阀门、总阀、加热、协议动作必须确认必要 MFC setpoint 已成功建立。
- **恢复路径**：Stop/Reset/退出关闭阀门，并将 MFC 恢复到 idle 或配置默认安全值；失败时记录并提示人工处理。
- **Protocol/Pre-test 集成**：Protocol Engine 与 Pre-test 阀矩阵/Flow Apply 统一调用安全守卫；禁止直接访问底层发送接口。
- **遥测与 UI**：Worker 经信号/槽推送 MFC/阀门安全状态；UI 显示时间戳，串口或反馈异常时禁用危险动作。
- **日志格式**：记录 `ts, cmd_type, channel, target, feedback, valve_target, state, reason, source`；异常情况下追加建议动作。
- **配置校验**：Options 页面保存 MFC/串口/安全默认流量时进行范围校验；保存失败时提示且不改动现有配置。

## Architecture Compliance（architecture_compliance）
- 安全逻辑在 Worker/Service 层（非 UI）；通过信号/槽将状态广播到控制器/UI。
- 命令经控制器→SafetyManager→HAL/Driver；禁止 UI 直接写硬件。
- 保持现有目录与命名：`app/controllers|workers|services|models`，与 architecture.md 一致。
- 日志沿用现有 Data Logger/应用日志通道，避免新建平行 logger。
- 线程安全：安全状态读写使用线程安全原语/Qt 线程上下文，避免竞态。

## Library & Framework Requirements（library_framework_requirements）
- Python 3.10+；PySide6 当前 6.7.2，最新 6.10.1（pip index）；若评估升级需验证 PyInstaller 打包。
- pyqtgraph 已装 0.13.7，最新 0.14.0，升级需回归 100Hz 绘制与 Qt 兼容性。
- nidaqmx 已装 0.9.0，最新 1.3.0，升级需确认 NI-DAQmx 驱动匹配与 API 变更。
- pyserial 3.5 为最新；保持。

## File Structure Requirements（file_structure_requirements）
- `app/workers/hardware_worker.py`：气流读取、状态机、紧急关断、信号发送。
- `app/services/safety_manager.py`（或新增）：安全守卫接口，集中判定与日志。
- `app/controllers/main_controller.py` / Protocol 引擎：在命令入口统一调用安全守卫；处理错误码并反馈 UI。
- `app/views/...`（Footer/Pre-test/Protocol/Options）：展示 SAFE/LOW FLOW/数据过期状态，禁用按钮，阈值配置入口。
- `config/default_config.json`：阈值与设备配置；持久化与 UI 一致。
- `tests/`：新增安全状态机、命令阻断、跌落恢复相关单测/集成测试。

## Testing Requirements（testing_requirements）
- 单元测试：idle 状态允许 MFC setpoint；硬件未 ready 时阻断所有硬件命令；setpoint 失败/反馈异常时阻断阀门/协议。
- 集成/Mock 测试：模拟 Alicat A/B/C setpoint 成功后阀门放行；模拟串口失败后阀门/协议阻断；Stop/Reset 恢复 MFC/阀门。
- 性能测试：MFC 命令与状态更新不阻塞 UI；遥测推送 5-10Hz。
- 回归：配置读写与 Options 页面互通；日志格式与既有 logger 兼容。

## Previous Story Intelligence（previous_story_intelligence）
- Story 1.0 已建立 PySide6 MVC + Worker 框架、CI/打包；目录结构与命名需复用。
- Story 1.1 完成硬件自检与阻断：已有 `hardware_ready` 保护、NI/RS232 状态信号、中文错误提示。安全互锁应复用同一守卫路径，并在自检未通过或设备缺失时保持阻断。
- 已有日志与 CI 流水线可直接复用；保持中文提示一致性。

## Git Intelligence Summary（git_intelligence_summary）
- 仓库存在 git，最近可见提交仅初始提交（a9283e8），暂无可供参考的实现变更；实现时请创建新提交记录。

## Latest Tech Information（latest_tech_information）
- PySide6 最新 6.10.1（已装 6.7.2）：含 Qt bugfix；升级需验证 PyInstaller hidden-import 与字体打包。
- pyqtgraph 最新 0.14.0（已装 0.13.7）：升级需确认 100Hz 绘图性能与 Qt 兼容。
- nidaqmx 最新 1.3.0（已装 0.9.0）：升级前需确认 NI-DAQmx 驱动版本匹配，API 可能有差异。
- pyserial 3.5 为最新。

## Project Context Reference（project_context_reference）
- docs/project-context.md（范围/约束/安全目标）
- docs/epics.md（Epic 1 & Story 1.2 需求与 AC）
- docs/prd.md（FR1.2 安全互锁等需求）
- docs/architecture.md（MVC+Worker、安全/性能约束、目录结构）
- docs/ux-design.md（全局状态区、安全提示、页面布局）

## Story Completion Status（story_completion_status）
- 状态：fix-applied-pending-real-hardware-retest
- 产物：docs/sprint-artifacts/1-2-safe-start-airflow-interlock.md
- 完成说明：真实硬件复核发现 LOW FLOW 阈值模型与 Alicat 设备语义不匹配，已重定义为 MFC setpoint 与阀门顺序互锁。

## Dev Agent Record
### Context Reference
- 本故事文档；如有新增日志/调试记录请补充。

### Agent Model Used
- Codex (GPT-5)

### Debug Log References
- pytest -q（27 passed）
- 2026-06-12：真实硬件复核确认，设备默认无气流是正常 idle，Alicat 设定多少气流就应输出多少，不存在独立 LOW FLOW 故障状态。
- 2026-06-12：`.venv-win\python.exe -m pytest -q` 通过，103 passed。
- 2026-06-12：现场反馈 Flow Apply 显示“主阀切换失败，已阻断写入”；修复为 MFC setpoint 阶段不切换主阀。`.venv-win\python.exe -m pytest -q` 通过，104 passed。
- 2026-06-12：现场反馈 Flow Apply 后启动流量无变化；修复为 Alicat setpoint 写入后立即轮询同一 unit 的 setpoint 回读，匹配才判定成功，不匹配则 Flow Apply 失败并暴露具体通道。`.venv-win\python.exe -m pytest -q` 通过，106 passed。
- 2026-06-12：现场反馈 B 通道 setpoint 未确认；增强诊断日志，setpoint no readback/mismatch 会记录 channel、unit、target、readback 和 Alicat 原始 response。`.venv-win\python.exe -m pytest -q` 通过，106 passed。
- 2026-06-12：Alicat 探测确认返回 setpoint 为 `0.5000/1.0000/0.5000`，而 UI 使用 sccm（500/1000/500）。修复为应用内部保持 sccm，发送 Alicat setpoint 时乘以 `alicat_setpoint_scale=0.001`，读回流量时乘以 `alicat_readback_scale=1000.0`。`.venv-win\python.exe -m pytest -q` 通过，106 passed。
- 2026-06-30：现场反馈重新自检/操作时 COM6 再次拒绝访问；修复为 `HardwareWorker` 在调用 `HardwareCheckService` 自检前先释放 HAL 持有的串口句柄，避免应用内部 RealHAL telemetry 与自检服务争抢 COM6。`.venv-win\python.exe -m pytest -q` 通过，107 passed。
- 2026-06-30：现场反馈 C 通道 setpoint 未确认；增强 Alicat setpoint 校验为 3 次短重试，默认每次等待 0.1s，并将探测脚本的 `--set` 参数改为按 sccm 输入后自动换算设备单位。`.venv-win\python.exe -m pytest -q` 通过，107 passed。
- 2026-06-30：现场反馈流量正常设定后主阀切换失败；修复 RealHAL 数字输出线路格式，将配置中的 `P1.0`/`P0.7` 兼容转换为 NI-DAQmx 需要的 `port1/line0`/`port0/line7`。`.venv-win\python.exe -m pytest -q` 通过，108 passed。

### Completion Notes List
- 2026-06-12：重定义故事范围。原 AC1-AC6 的 LOW FLOW 阈值模型废弃，替换为自检前阻断、idle 合法、MFC 设定/反馈追踪、阀门顺序互锁、安全恢复和统一接口守卫。
- 2026-06-12：实现第一阶段修复：idle/0 flow 不再触发 LOW_FLOW；Flow Apply 只要求硬件 ready 并允许建立 MFC setpoint；成功后标记 `flow_setpoints_ready`；阀门打开前必须等待 MFC setpoint 建立；Stop/Reset/退出后清除 setpoint ready 标记。
- 2026-06-12：修复 Flow Apply 仍尝试切主阀的问题；建立 MFC setpoint 不再调用 master valve，主阀/气味阀留到阀门阶段由互锁控制。
- 2026-06-12：修复 setpoint 写入假阳性：`RealHAL.set_flow()` 不再只以串口 write 成功为成功标准，而是读取 Alicat 数据帧中的 setpoint 字段并按容差确认。
- 2026-06-12：修复 UI sccm 与 Alicat L/min 量级不匹配：1000 sccm 发送为 1.000，500 sccm 发送为 0.500；setpoint 校验按 Alicat 设备单位比较。
- 2026-06-30：修复 COM6 应用内句柄竞争：自检前释放 RealHAL 持有的串口/硬件句柄，再由 `HardwareCheckService` 打开 COM6。
- 2026-06-30：修复 setpoint 回读确认过于激进：Alicat 设定后轮询回读最多重试 3 次，降低 C 通道刷新较慢导致的假失败。
- 2026-06-30：修复主阀数字输出失败：RealHAL 写 NI DO 前规范化线路名，兼容现有 `DevX/PY.Z` 配置格式。
- 2026-06-30：现场确认阀门按钮不应立即通断；修复 Pre-test 阀门交互为“点击仅预选气道，启动/stim_start 后再打开本次选中阀门，rest/中断/完成后关闭本次阀门”。`.venv-win\python.exe -m pytest -q` 通过，111 passed。
- 2026-06-30：现场确认软件启动后默认无气流，移除可见“应用”按钮；`启动` 成为唯一执行入口。未预选阀门时启动写入 Rest 流量（A=A+C, C=C）且不通阀；预选阀门时启动写入 Stim 流量（A=A, C=0）并打开预选阀，持续时间结束/中断后恢复 Rest 流量并关闭本次阀门。`.venv-win\python.exe -m pytest -q` 通过，111 passed。
- 2026-06-30：现场反馈软件打开后未启动已有气流；确认为 Alicat 保留上次 setpoint。修复为自检通过后立即写入 A/B/C=0，并保持 `flow_setpoints_ready=False`，确保打开软件后的默认态为无气流。`.venv-win\python.exe -m pytest -q` 通过，112 passed。
- 2026-06-30：进入 1.3 前复核 1.2 顺序逻辑，修复两个边界：关闭阀门不再依赖主阀写入成功；Stim 流量 setpoint 失败时立即中断本次发送，不打开预选阀门、不产生伪关闭写入。`.venv-win\python.exe -m pytest -q` 通过，114 passed。
- 2026-06-30：明确“预选气道”和“实际打开阀门”是两种状态；持续时间结束/中断只关闭本次实际打开的阀门，不清空用户预选状态，便于下一次直接启动同一气道。`.venv-win\python.exe -m pytest -q` 通过，115 passed。
- 2026-06-30：现场反馈点击启动后 UI 卡顿；确认为 Alicat setpoint/readback 与 NI 数字写入在 UI 线程同步执行。修复为 Pre-test 启动/结束硬件序列在后台线程执行，完成后通过 Qt signal 回写 UI；新增慢硬件回归测试确保启动点击快速返回。`.venv-win\python.exe -m pytest -q` 通过，116 passed。
- Task1（AC1/AC3）：引入 SafetyState（SAFE/LOW_FLOW/DATA_STALE）状态机与滞后恢复；检测读数异常/时间超阈值标记过期；硬件安全上报可覆盖流量判定；控制器/UI 状态文案统一；新增单元测试覆盖阈值、滞后、过期、异常读数与硬件覆盖。
- Task2（AC1/AC2/AC6）：新增统一安全守卫接口（SafetyManager.guard_command + MainController.ensure_safe_command），硬件未就绪/LOW_FLOW/DATA_STALE 时阻断命令，SAFE 且硬件就绪才放行，状态提示统一。
- Task3（AC3）：检测 SAFE→LOW_FLOW/过期/硬件故障转移时记录 last_shutdown_event，并以“紧急关闭”提示，防止后续命令绕过。
- Task4（AC4）：阈值校验规则（必须为正、有限、不过大）+ Options 持久化更新；配置写回 `config/default_config.json`，非法值拒绝且提示。
- Task5（AC1/AC5）：UI Telemetry 标签显示安全状态+原因，对 DATA_STALE 显示“数据过期”；新增数据过期状态提示测试。
- Task6（AC3/AC5）：Telemetry 接口与安全状态更新写入日志（flow/hardware/原因/source），记录 last_shutdown_event 含 source，便于追踪阻断事件。

### File List
- docs/sprint-artifacts/1-2-safe-start-airflow-interlock.md
- docs/sprint-artifacts/sprint-status.yaml
- app/services/safety_manager.py
- app/models/safety_state.py
- app/models/app_state.py
- app/controllers/main_controller.py
- app/views/main_window.py
- app/views/pretest_view.py
- app/workers/hardware_worker.py
- tests/test_flow_controls.py
- tests/test_safety_manager.py
- tests/test_app.py
- app/main.py
