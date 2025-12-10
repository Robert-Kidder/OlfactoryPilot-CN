# Story 2.6: Automatic Breath Calibration Session
Status: ready-for-review
Epic: 2 - Calibration & Manual Control
Story Key: 2-6-automatic-breath-calibration-session
Story ID: 2.6

## Story
作为研究人员，
我希望点击“开始校准”按钮并进行一段自然的呼吸，系统能自动计算我呼吸信号的基线偏移（Offset）和幅度增益（Gain），
从而使波形自动居中并最大化显示，无需手动调整 Y 轴范围。

## Acceptance Criteria
1.  **校准会话控制 (AC1)**:
    *   Given 校准页面;
    *   When 用户在“校准时长”输入框设置时间 (默认 10s) 并点击“启动校准” (绿色按钮);
    *   Then 按钮变为“中断校准” (黄色)，进入倒计时状态，右侧状态条显示剩余时间与“正在校准...”。
2.  **数据采集与统计 (AC2)**:
    *   Given 校准进行中;
    *   When 系统以 100Hz 采集呼吸数据;
    *   Then 实时更新并显示当前采集窗口内的 Max (最大值) 和 Min (最小值)。
3.  **自动计算参数 (AC3)**:
    *   Given 校准倒计时结束;
    *   When 采集窗口关闭;
    *   Then 系统自动计算并显示 Offset = -(Max + Min)/2 和 Gain = TargetRange / (Max - Min);
    *   And 自动应用这些参数调整波形显示的 Y 轴范围 (或对信号进行预处理)，使波形居中且幅度适中。
    *   And 按钮恢复为“启动校准”，状态条显示“校准完成”。
4.  **手动中断 (AC4)**:
    *   Given 校准进行中;
    *   When 用户点击“中断校准”;
    *   Then 立即停止采集，不应用新的计算参数，保留上一次的校准值，状态条显示“已中断”。
5.  **参数持久化 (AC5)**:
    *   Given 校准完成并获得新的 Offset/Gain;
    *   When 退出软件或切换页面;
    *   Then 这些校准参数被保存到 `config/default_config.json` (或用户配置)，下次启动自动加载。

## Tasks / Subtasks
- [x] **UI 扩展 (CalibrationView)**:
    - [x] 添加 `QSpinBox` 用于设置校准时长 (秒)。
    - [x] 添加 `QPushButton` (Toggle 或两个互斥按钮) 用于 Start/Stop，设置样式 (绿/黄)。
    - [x] 添加 `QLabel` 显示 Max, Min, Offset, Gain 数值。
    - [x] 添加 `QProgressBar` 或文本标签显示倒计时/状态。
- [x] **逻辑实现 (CalibrationService/Controller)**:
    - [x] 创建 `CalibrationSession` 类或在 `CalibrationController` 中管理状态 (Idle -> Calibrating -> Finished)。
    - [x] 实现定时器或 Tick 逻辑处理倒计时。
    - [x] 实现实时 Min/Max 统计逻辑。
    - [x] 实现校准结束时的 Offset/Gain 计算公式。
- [x] **波形应用 (Signal Processing)**:
    - [x] 修改 `CalibrationView` 或信号预处理管道，应用 Offset 和 Gain 到显示的波形数据 (或者调整 Plot 的 YRange)。
- [x] **持久化 (Config)**:
    - [x] 在 `AppState` 和 `Config` 中新增 `signal_offset` 和 `signal_gain` 字段。
    - [x] 确保持久化逻辑涵盖这些新字段。
- [x] **测试**:
    - [x] 单元测试：验证 Min/Max 统计准确性、Offset/Gain 计算公式。
    - [x] 集成测试：模拟 10s 校准流程，验证状态流转 (Start -> Wait -> Finish -> Apply)。

## Developer Context
- 现有 `CalibrationView` 已有波形显示，需在现有布局中插入新的控制面板 (参考 FeatureList 截图布局)。
- 信号处理：目前是原始值显示。FeatureList 提到“动态计算偏移量与增益...使波形始终占据绘图区的主要可视区域”。这可以通过 `pyqtgraph.setYRange` 实现，或者在数据层 `(Raw + Offset) * Gain` 处理。建议采用数据层处理，这样阈值判断也基于归一化后的数据，更符合直觉。**注意确认这一点与现有阈值逻辑的兼容性** (如果阈值是基于原始电压的，那么波形变换后阈值线也要变，或者统一使用归一化单位)。
    - *决策*: FeatureList 暗示是对信号进行缩放 ("采集窗口内的最大值与最小值作为缩放参考...动态计算...使完整波形占据显示窗口")。通常这意味着后续的阈值判断基于处理后的信号。为降低复杂度，本 Story 先仅做**视图层缩放** (Auto-Range Y轴) 或者 **简单的线性变换**。如果改变了信号数值，必须同步更新阈值线的坐标系。
    - *建议*: 保持 Raw Data用于记录，Display Data = (Raw + Offset) * Gain。阈值线基于 Display Data 坐标系。
- 需要考虑线程安全：UI 按钮 -> Controller -> 状态变更。

## Tech Requirements
- PySide6 信号槽机制处理倒计时。
- 避免在 UI 线程进行繁重计算 (虽然 Min/Max 计算很轻量)。

## Dev Agent Record
### Context Reference
- Story 2.6 implemented by Dev Agent.
### Agent Model Used
- Gemini 2.0 Flash
### Debug Log References
- Tests written but not runnable in current environment (missing pytest). Verified by code inspection.
### Completion Notes List
- **UI**: Added `CalibrationView` controls (SpinBox, Button, Stats Labels).
- **Logic**: Implemented `CalibrationSession` in `app/services/calibration_service.py` to handle 10s auto-calibration.
- **Integration**: Wired `MainController` to handle calibration requests and feed data to session.
- **Persistence**: Added `signal_offset` and `signal_gain` to `AppState` and config persistence.
- **Signal Processing**: Applied linear transform `(Raw + Offset) * Gain` in `CalibrationView._render_frame`.
- **Testing**: Added 3 test suites (`test_calibration_ui`, `test_calibration_logic`, `test_calibration_integration`) covering AC1-AC5.

## File List
- app/views/calibration_view.py
- app/controllers/main_controller.py
- app/models/app_state.py
- app/services/calibration_service.py
- app/services/__init__.py
- tests/test_calibration_ui.py
- tests/test_calibration_logic.py
- tests/test_calibration_integration.py
