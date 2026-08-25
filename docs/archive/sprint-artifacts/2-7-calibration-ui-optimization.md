# Story 2.7: Calibration UI Optimization
Status: done
Epic: 2 - Calibration & Manual Control
Story Key: 2-7-calibration-ui-optimization
Story ID: 2.7

## Story
作为研究人员，
我希望校准页面布局直观、反馈清晰，操作区与显示区逻辑分离，
从而减少认知负荷，更专注于呼吸信号的质量。

## Acceptance Criteria
1.  **布局重构 (AC1)**:
    *   Given 校准页面;
    *   Then 界面分为上下两部分：
        *   顶部：操作控制区 (Top Control Bar)，包含校准启动与阈值设置。
        *   下方：左右分栏 (Split View)，左侧为波形与进度，右侧为状态与数据反馈。
    *   And 使用 `QSplitter` 允许调整左右分栏比例。
2.  **进度条反馈 (AC2)**:
    *   Given 校准启动;
    *   When 倒计时进行中;
    *   Then 进度条 (`QProgressBar`) 实时更新 (0-100%)，且位置紧贴波形图下方。
3.  **视觉优化 LED (AC3)**:
    *   Given 状态指示灯 (吸气/呼气/Gating);
    *   Then 样式呈现为圆形 (Circular CSS)，激活时高亮，未激活时暗灰。
4.  **数据面板 (AC4)**:
    *   Given 校准数据更新;
    *   Then 右下角显示独立的分组框 (`QGroupBox`)，包含 Max/Min/Offset/Gain 的网格化数据显示。

## Tasks / Subtasks
- [x] **重构布局结构 (CalibrationView)**:
    - [x] 拆分 `_build_ui` 为 `_build_top_controls` (Top) 和 `_build_split_view` (Bottom)。
    - [x] 引入 `QSplitter` 作为下方主容器。
    - [x] 将按钮与 SpinBox 移至顶部 `QHBoxLayout`。
- [x] **实现右下反馈区 (Feedback Pane)**:
    - [x] 创建 `QGroupBox("状态与数据")`。
    - [x] 将 LED 标签应用 `border-radius: 50%;` 等 CSS 样式。
    - [x] 将 Max/Min/Offset/Gain 标签移入此区域，使用 Grid 布局。
- [x] **实现左下波形区 (Waveform Pane)**:
    - [x] 将 `PlotWidget` 和 `QProgressBar` 放入垂直布局容器。
    - [x] 实现 `set_calibration_progress(int)` 方法更新进度条。
- [x] **控制器适配**:
    - [x] 确保 `MainController` 调用新的 `set_calibration_progress` 接口。

## Developer Context
- 保持现有业务逻辑不变 (`CalibrationSession`, `GatingService`)，仅修改 View 层和 Controller 的 UI 更新调用。
- CSS 样式参考：
    ```css
    QLabel[led="true"] {
        border-radius: 12px; /* assuming 24px height */
        min-width: 24px;
        max-width: 24px;
        min-height: 24px;
        max-height: 24px;
        background-color: #343a40; /* off state */
    }
    ```
- 需要在 `CalibrationView` 中添加 `set_calibration_progress` 方法。

## File List
- app/views/calibration_view.py
- app/controllers/main_controller.py
- tests/test_calibration_ui_optimization.py

## Dev Agent Record
### Context Reference
- Story 2.7 implemented.
### Completion Notes List
- **Layout Refactoring**: Implemented "Top Controls + Split View (Waveform | Feedback)" layout.
- **Visuals**: Added circular LEDs (`_build_circular_led`) and Progress Bar (`_calibration_progress`).
- **Integration**: Updated `MainController` to drive the new progress bar.
- **Cleanup**: Removed obsolete helper methods from `CalibrationView`.
- **Testing**: Added `test_calibration_ui_optimization.py` verifying layout structure.

