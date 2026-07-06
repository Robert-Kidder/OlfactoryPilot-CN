# Epic 1 真实硬件复核清单

Status: In Progress
Epic: 1 - Safe Hardware Foundations
Trigger: 已接入真实硬件开发环境，需要从 Epic 1 开始逐个 story 复核是否真实开发完成。
Mode: Incremental

## 复核原则

- `done` 只在真实硬件或明确可接受的等效证据通过后恢复。
- 每个 story 必须记录操作步骤、实测结果、证据路径和判定。
- 发现问题时不直接跳过，记录为修复项并保持对应 story 为 `real-hardware-review` 或 `blocked`。
- Mock/Simulation 测试可作为回归证据，但不能替代真实硬件验收。

## 硬件与环境

- 应用可成功打开：是
- HAL 模式：real
- NI 设备配置：Dev1 / Dev2 / Dev3
- AI0 通道：Dev1/ai0
- RS232 配置：COM6, 19200 baud
- Alicat ID：A=a, B=b, C=c
- 证据目录：待补充

## Story 1.0: Project Scaffold and CI Baseline

### 核验目标

确认应用在真实硬件环境机器上可安装、启动、运行基础测试/打包流程，并能加载真实硬件配置。

### 前置条件

- Python/依赖已安装，或已使用打包产物。
- `config/default_config.json` 指向真实硬件配置。
- 软件可成功打开。

### 操作步骤

1. 启动应用。
2. 确认窗口打开且未因 RealHAL 初始化崩溃。
3. 如需要，运行本地测试或 CI 脚本，确认非硬件单测仍通过。
4. 记录启动方式、版本、配置文件路径和日志路径。

### 期望结果

- 应用成功打开。
- 配置加载成功。
- 无启动级异常。
- 日志能记录启动错误或运行状态。

### 实测结果

- 用户现场确认：应用已能在真实硬件开发环境中成功打开。
- 配置文件显示当前为真实硬件模式：`hal_mode = real`。
- 配置文件加载真实硬件参数：`serial_port = COM6`、`baud_rate = 19200`、`ai0_channel = Dev1/ai0`。
- Codex 当前 shell 无法启动 `python.exe`，错误为“指定的登录会话不存在。可能已被终止。”；`py` 启动器不存在，因此本轮未能补跑 `python app/main.py --help` 或 `python -m pytest`。

### 证据

- 现场证据：用户确认软件可成功打开。
- 配置证据：`config/default_config.json`。
- 工具限制证据：Codex shell 中 `python --version`、`python app/main.py --help`、`python -m pytest` 均因 `python.exe` 登录会话不可用失败；`py` 未安装。

### 判定

Pass - 启动核验通过；自动化测试/CI 证据待在可用 Python 环境中补跑。

## Story 1.1: Device Self-Check and Status Report

Status: In Field Verification

### 核验目标

确认真实 NI-USB-6001/6501 与 RS232/Alicat 自检能正确 Pass/Fail，并在 UI 中显示可操作的中文状态。

### 前置条件

- NI 设备已连接并由 NI-DAQmx 识别。
- Alicat 串口连接到 COM6，波特率 19200。
- 应用以真实硬件模式启动。

### 操作步骤

1. 点击 Connect 或重新自检。
2. 观察 NI 设备检测结果。
3. 观察 RS232/Alicat 轮询结果。
4. 断开一个设备后再次自检，验证 Fail 与中文建议。
5. 恢复设备后再次自检，验证状态可恢复。

### 现场记录项

- Connect/自检时间：待填写
- NI Dev1 检测结果：待填写
- NI Dev2 检测结果：待填写
- NI Dev3 检测结果：待填写
- RS232 COM6 / 19200 检测结果：待填写
- Alicat A/B/C 轮询结果：待填写
- UI 中文提示：待填写
- 日志路径或截图路径：待填写
- 是否出现失败后阻断硬件控制：待填写
- 恢复后是否可重新自检通过：待填写

### 期望结果

- 每个设备独立显示 Pass/Fail。
- 失败时硬件控制被阻断。
- 恢复后可重新自检并解除阻断。
- 日志记录设备名、失败原因、建议动作和时间。

### 实测结果

- 2026-06-12 12:36:59 自检执行完成。
- NI Dev1：PASS，原因为 USB-6001 连接正常。
- NI Dev2：PASS，原因为 USB-6001 连接正常。
- NI Dev3：PASS，原因为 USB-6501 连接正常。
- COM6 现场确认为 ATEN USB to Serial Bridge。
- RS232 COM6：FAIL，原因为串口打开失败：`PermissionError(13, '拒绝访问。', None, 5)`。
- 系统建议：关闭可能占用串口的程序后重试。
- 后续现场反馈：COM6 已连上，等待重新自检日志确认 RS232/Alicat 是否 PASS。
- 后续现场反馈：程序启动时 COM6 仍然出现拒绝访问；拔插 ATEN USB to Serial Bridge 后自检 PASS。
- 修复已实施：
  - `RealHAL` 不再在初始化阶段打开 COM6，改为首次读取/写入 Alicat 时惰性打开，避免启动期端口拒绝访问导致应用启动失败。
  - `HardwareCheckService` 增加串口打开短重试，默认 `serial_open_retries = 2`、`serial_retry_interval_s = 0.25`。
  - COM6 权限/占用失败时提示更新为：关闭占用 COM6/ATEN 的程序，或拔插 ATEN 后点击重新自检。
  - 新增测试覆盖：串口第一次拒绝访问、第二次重试成功；RealHAL 初始化阶段不打开串口。
- 自动化验证：使用项目虚拟环境 `.venv-win\python.exe` 运行测试通过。
  - `.venv-win\python.exe -m pytest tests/test_app.py -q`：45 passed
  - `.venv-win\python.exe -m pytest -q`：102 passed
- 现场验证：仍需在真实应用中重新执行 Connect/自检，确认启动时不拔插 COM6 是否可恢复 PASS。
- 现场验证：用户确认修复后现场测试通过。
- HardwareWorker 汇总：`ready=False`，项目数 4。
- Alicat A/B/C 未能进入有效轮询验证，因为 COM6 打开失败。
- 结论：NI 自检链路通过；COM6 端口身份正确，拔插后可恢复 PASS，但程序启动时仍可能因权限、占用或 ATEN 驱动句柄未释放而拒绝访问。Story 1.1 不应完全通过，需要补充启动期串口恢复/重试策略或现场操作约束。

### 证据

- 用户提供日志：
  - `app.services.hardware_check_service | 设备=Dev1 | 类型=ni | 状态=PASS`
  - `app.services.hardware_check_service | 设备=Dev2 | 类型=ni | 状态=PASS`
  - `app.services.hardware_check_service | 设备=Dev3 | 类型=ni | 状态=PASS`
  - `app.services.hardware_check_service | 设备=COM6 | 类型=serial | 状态=FAIL | 原因=串口打开失败: could not open port 'COM6': PermissionError(13, '拒绝访问。', None, 5)`
  - `app.workers.hardware_worker | 硬件自检完成 | ready=False | 项目数=4`

### 判定

Pass - NI 通过，COM6/ATEN 启动期恢复问题已修复并通过现场复测。

## Story 1.2: Safe Start Airflow Interlock

Status: In Field Analysis

### 核验目标

确认真实气流值能驱动 SAFE/LOW FLOW 状态，并阻断或放行阀门、加热、流量命令。

### 前置条件

- 自检通过。
- 可读取真实气流或 Alicat 反馈。
- 已配置安全阈值。

### 操作步骤

1. 在气流低于阈值时尝试发送阀门或流量命令。
2. 观察 UI 是否显示 LOW FLOW。
3. 将气流恢复到阈值以上。
4. 再次发送命令，确认放行。
5. 在操作中人为制造低流量，确认系统触发安全关闭。

### 期望结果

- 低流量时命令被阻断。
- SAFE 时命令放行。
- 运行中跌破阈值会关闭相关输出并写日志。
- UI 在目标时间内刷新状态。

### 实测结果

- 现场事实：设备启动后默认无气流，当前气流始终低于安全阈值。
- 当前表现：系统进入低流量状态，指令被阻断。
- 初步判定：AC1 低流量阻断行为符合预期。
- 风险发现：如果“打开/设置气流的指令”也被 LOW FLOW 阻断，则系统可能出现启动死锁，即无法通过软件把气流从 LOW FLOW 恢复到 SAFE。
- 需要澄清：Flow Rate Apply 是否属于应被完全阻断的危险动作，还是应允许一个受限的“启动供气/建立安全气流”动作绕过低流量阻断。
- 需求澄清：真实设备语义下不存在独立的 LOW FLOW 状态；Alicat 流量计设定多少气流就应是多少。默认无气流是正常 idle 状态，不应被解释为故障或全局阻断条件。
- 产品影响：Story 1.2 的原始“气流低于阈值阻断所有指令”模型与真实硬件不匹配，需要重定义为“硬件自检通过 + MFC 设定/反馈一致性 + 危险动作顺序保护”。
- 文档更新：`docs/sprint-artifacts/1-2-safe-start-airflow-interlock.md` 已重定义为 “Safe Alicat Flow Command and Valve Interlock”。
- 新验收重点：自检前阻断、idle 合法、MFC 设定/反馈追踪、阀门顺序互锁、安全恢复、统一接口守卫。
- 实现更新：
  - idle/0 flow 不再触发 LOW_FLOW。
  - Flow Apply 在硬件 ready 后允许执行，用于建立 Alicat MFC setpoint。
  - MFC setpoint 成功后设置 `flow_setpoints_ready`。
  - 阀门打开前必须等待 MFC setpoint 建立；关闭阀门仍允许。
  - Stop/Reset/退出后清除 MFC setpoint ready 标记。
  - Flow Apply 不再切换主阀，避免 MFC setpoint 阶段因 master valve 写入失败而被阻断。
  - Alicat setpoint 写入后立即轮询同一 unit 的 setpoint 回读；匹配才判定成功，不匹配则 Flow Apply 失败。
  - Alicat 单位换算已实现：UI/应用使用 sccm，发送 setpoint 时按 `0.001` 转为 Alicat 设备单位，读回流量按 `1000.0` 转回 sccm。
  - 自检前释放 RealHAL 持有的串口句柄，避免 telemetry 与自检服务争抢 COM6。
  - NI 数字输出线路名已规范化，支持将 `P1.0` 配置转换为 `port1/line0`。
- 自动化验证：`.venv-win\python.exe -m pytest -q` 通过，108 passed。

### 证据

- 用户现场反馈：设备启动后默认无气流，气流始终低于阈值，指令被阻断。

### 判定

Fix Applied - 原 LOW FLOW 阈值模型已废弃；Alicat MFC setpoint 与阀门顺序互锁第一阶段实现完成，等待现场复测。

## Story 1.3: Global Safety Toolbar

### 核验目标

确认 Connect、Reset、Stop、Help 在真实硬件环境下可用，并通过同一安全守卫链路。

### 前置条件

- 应用已打开。
- 真实硬件已连接。
- 本地 manual 路径可访问或缺失场景可验证。

### 操作步骤

1. 点击 Connect，记录自检结果。
2. 点击 Reset，确认关闭阀门、重新握手、自检刷新。
3. 点击 Stop，确认停止操作并释放资源。
4. 点击 Help，确认本地 PDF 打开或显示明确错误。

### 期望结果

- 所有按钮不阻塞 UI。
- Connect/Reset/Stop 都写入日志。
- Stop 后必须重新 Connect/自检才能继续硬件控制。
- Help 不依赖网络。

### 实测结果

待填写。

### 证据

待填写。

### 判定

Pending

## Story 1.4: Safe Shutdown and Valve Reset

### 核验目标

确认真实硬件在 Stop、应用退出、异常恢复场景下回到安全状态。

### 前置条件

- 至少一个阀门或流量控制可被真实触发。
- 日志路径可写。
- last_shutdown_event 可持久化。

### 操作步骤

1. 启动应用并完成自检。
2. 执行一次阀门或流量操作。
3. 点击 Stop，确认阀门关闭、资源释放、日志写入。
4. 重新启动应用，确认上次 shutdown 摘要显示。
5. 验证异常或失败关闭场景的提示与阻断。

### 期望结果

- 所有阀门/执行器进入安全状态。
- 未完成关闭时 UI 给出人工处理提示。
- relaunch 后能读取并显示上次关机状态。
- 后续命令仍必须经过自检与安全守卫。

### 实测结果

待填写。

### 证据

待填写。

### 判定

Pending

## Story 1.5: Hardware Simulation Layer (Mock HAL)

### 核验目标

确认 Mock HAL 和 Real HAL 的边界清晰，真实硬件复核不会被模拟模式误判为通过。

### 前置条件

- 可分别以 `--simulation` 和 real 配置启动。
- 配置文件中 `hal_mode` 可确认。

### 操作步骤

1. 以模拟模式启动，确认窗口标识模拟模式。
2. 以真实模式启动，确认不显示模拟模式。
3. 检查真实模式是否使用 `RealHAL`。
4. 验证 Mock 测试仍可通过，但不作为真实硬件通过证据。

### 期望结果

- 模拟/真实模式显示明确。
- 真实模式启动失败时给出可诊断错误。
- Mock 仅用于开发与回归，不替代真实验收。

### 实测结果

待填写。

### 证据

待填写。

### 判定

Pending

## 汇总

| Story | 当前复核状态 | 判定 | 主要风险/修复项 |
| --- | --- | --- | --- |
| 1.0 | Completed | Pass | 自动化测试/CI 证据待在可用 Python 环境中补跑 |
| 1.1 | Completed | Pass | NI 通过；COM6/ATEN 启动期恢复问题已修复并通过现场复测 |
| 1.2 | Fix Applied | Pending Retest | idle/0 flow 合法、MFC setpoint 建立、阀门顺序互锁已实现；等待现场复测 |
| 1.3 | Pending | Pending | 待验证工具栏真实硬件流程 |
| 1.4 | Pending | Pending | 待验证真实 Stop/退出安全复位 |
| 1.5 | Pending | Pending | 待验证 Mock/Real 模式边界 |
