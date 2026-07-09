# Story 1.5: Hardware Simulation Layer (Mock HAL)

Status: Ready for Review

## Story

As a developer,
I want a software-only simulation mode that mimics hardware behavior,
so that I can verify UI, safety logic, and protocols without physical devices.

## Acceptance Criteria

1. **Simulation Flag Support**:
   - Given I launch the app with `--simulation` flag
   - When the app starts
   - Then it bypasses physical hardware checks (NI/RS232)
   - And loads the Mock HAL implementation
   - And the UI Title Bar displays "[SIMULATION MODE]" (or similar clear indicator)

2. **Synthetic Breath Signal**:
   - Given Simulation Mode is active
   - When I navigate to Calibration or Protocol views
   - Then the breath waveform graph displays a synthetic signal (e.g., sine wave at ~0.2-0.3Hz)
   - And the signal updates at real-time rates (100Hz)

3. **Virtual Hardware Response**:
   - Given Simulation Mode is active
   - When I toggle valves or change flow rates
   - Then the system logs the "virtual" state changes
   - And returns success responses immediately (or with simulated delay)
   - And simulated airflow readings reflect the expected changes (e.g., flow drops slightly when valve opens, or just remains stable > threshold to keep system "Safe")

4. **Safety Logic Verification**:
   - Given Simulation Mode is active
   - When I set the "Virtual Airflow" below threshold (if control exists, or just by default)
   - Then the system enters LOW FLOW state and blocks commands (validating logic works with Mock data)

## Tasks / Subtasks

- [x] **Infrastructure & CLI** (AC1)
  - [x] Add `--simulation` argument parser in `main.py`
  - [x] Propagate simulation flag to `MainController` and `AppState`
  - [x] Update Main Window title to show status

- [x] **Architecture Refactoring** (Foundational)
  - [x] Define `HalInterface` protocol/ABC in `app/services/hal.py` (methods: `read_ai0`, `write_digital`, `read_flow`, `set_flow`, `close_all`, etc.)
  - [x] Refactor `HardwareWorker` to remove hardcoded sine-wave logic and instead delegate to `self.hal` instance.

- [x] **Mock HAL Implementation** (AC2, AC3)
  - [x] Create `app/services/mock_hal.py` implementing `HalInterface`
  - [x] **Extract** the existing placeholder sine-wave logic from `HardwareWorker` into `MockHAL.read_ai0()`
  - [x] Implement `write_digital()` and `set_flow()` to store internal state (simulating valve open/close)
  - [x] Implement `read_flow()` to return safe values (e.g., 1000 sccm)

- [x] **Worker Integration** (AC1)
  - [x] Update `HardwareWorker` to instantiate `MockHAL` when `--simulation` is set (or by default for now if Real HAL isn't ready)
  - [x] Ensure `device_self_check` uses the HAL interface (Mock should always pass self-check)

- [x] **Testing** (AC4)
  - [x] Verify safety logic triggers correctly with mock data
  - [x] Verify protocols run without hardware errors

## Dev Notes

### Current Codebase State
- **Refactoring Target**: The current `app/workers/hardware_worker.py` contains **hardcoded placeholder logic** (generating sine waves in `_emit_breath_sample`). This MUST be removed from the worker and moved into `MockHAL`.
- **Goal**: `HardwareWorker` should become a clean orchestrator that simply calls `self.hal.read_ai0()`, `self.hal.write_digital()`, etc., without knowing if it's real or mock.

### Architecture Compliance
- **HAL Pattern**: Ensure strict separation. `HardwareWorker` should talk to an interface, not concrete drivers directly.
- **Factory Pattern**: Use a simple factory or conditional logic in `HardwareWorker.__init__` to select the driver.
- **No Production Code Leaks**: Simulation logic should be contained in `mock_hal.py` and not pollute the main logic with excessive `if simulation:` checks, except for the initial injection.

### Technical Requirements
- **Synthetic Signal**: Reuse/Move the existing `math.sin` logic from worker to Mock HAL.
- **State Preservation**: The Mock HAL should remember the state of valves (Open/Closed) so UI feedback is consistent (e.g., if I open Valve 1, it stays Open in the "Virtual" state).

### File Structure Requirements
- `app/services/mock_hal.py`: New file.
- `app/services/hal.py`: New file (Interface definition).
- `app/main.py`: Update entry point.

### References
- [Proposal]: `docs/sprint-artifacts/sprint-change-proposal-2025-12-10.md`
- [Epics]: `docs/epics.md` Story 1.5

## Dev Agent Record

### Context Reference
`docs/epics.md`, `docs/architecture.md`, `docs/sprint-artifacts/sprint-change-proposal-2025-12-10.md`

### Agent Model Used
Gemini 2.0 Flash

### Debug Log
- 新增 `HalInterface` 协议与 `MockHAL`，由 `HardwareWorker` 通过注入的 HAL 读取波形/气流并执行数字输出，自检优先走模拟 HAL。
- `--simulation` 参数会跳过物理自检、载入 Mock HAL，并在窗口标题显示“[模拟模式]”；默认无 HAL 时仍使用 MockHAL。
- 所有变更均配套新测试 `tests/test_simulation_mode.py`，覆盖参数解析、模拟模式窗口标题、MockHAL 状态保持以及 HAL 调用路径。

### Completion Notes
- 接受准入标准：CLI 模拟开关、窗口提示、波形与气流由 MockHAL 输出、数字输出/自检走 HAL 接口，安全逻辑在模拟数据下仍可工作。
- 测试：`pytest`（全部 86 项通过）。

### File List
- `app/main.py`
- `app/models/app_state.py`
- `app/controllers/main_controller.py`
- `app/services/__init__.py`
- `app/services/hal.py`
- `app/services/mock_hal.py`
- `app/workers/hardware_worker.py`
- `tests/test_simulation_mode.py`
- `docs/sprint-artifacts/1-5-hardware-simulation-layer-mock-hal.md`

## Change Log
- 2025-12-11: 支持模拟模式 CLI 开关，接入 Mock HAL（波形/气流/数字输出），Worker 自检与状态更新走 HAL 接口，并补充全量测试通过。

