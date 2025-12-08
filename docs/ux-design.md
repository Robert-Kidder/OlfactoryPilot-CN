# UI/UX Specification
**Project:** OlfactoryPilot
**Version:** 1.0

## 1. UX Overview
* **Design Philosophy:** "Page-as-Subsystem". Each major function is an isolated tab to prevent cognitive overload.
* **Primary Users:** Psychology Researchers (Simplicity) and Lab Techs (Hardware Control).
* **Language:** Simplified Chinese (Zh-CN).

## 2. Information Architecture
### 2.1 Navigation
* **Style:** Top Tab Bar (Linear Workflow).
* **Tabs:**
    1.  **File (文件):** Setup and Params.
    2.  **Calibration (校准):** Signal acquisition.
    3.  **Pre-test (预测试):** Manual hardware check.
    4.  **Protocol (协议):** Automated experiment.
    5.  **Cleaning (清洗):** Maintenance.
    6.  **Options (选项):** System config.

### 2.2 Global Elements (Persistent Footer/Header)
* **Hardware Status:** Connection Icon (Green/Red), Air Flow Value (ml/min).
* **Safety Status:** "SAFE" (Green) vs "LOW FLOW" (Red - Flashing).
* **Status Refresh:** Hardware worker pushes connection/airflow/safety telemetry to UI via signals/slots at ~5-10 Hz; footer shows last-updated timestamp and flashes LOW FLOW within <500ms of unsafe state.
* **Controls:**
    * `[ Connect ]` (Main initialization).
    * `[ Reset ]` (Emergency re-handshake / Close valves).
    * `[ Stop ]` (Release resources).
    * `[ Help ]` (Open PDF Manual).

### 2.3 Performance & Safety Targets
* **Breath Graph Rendering:** ≥30 FPS sustained (measured over 10s window); warn if <30 FPS for >2s.
* **Telemetry Staleness:** LOW FLOW alert flashes within <500ms of unsafe airflow; show last-updated timestamp.
* **Sampling:** Breath waveform stream at 100Hz from hardware worker to UI.

## 3. Screen Specifications

### 3.1 Tab 2: Calibration (校准页面)
* **Visuals:**
    * **Graph:** Black background, White Signal Line (2px).
    * **Thresholds:** Red Dashed Line (Exhale - Draggable), Yellow Dotted Line (Inhale - Draggable).
    * **Feedback:** LED indicators that light up in real-time when signal crosses thresholds.
* **Interactions:**
    * "Auto-Scale" button to fit waveform to view.
    * Numeric spinners to fine-tune threshold voltages.

### 3.2 Tab 3: Pre-test (预测试页面)
* **Layout:** Dashboard Grid.
* **Valve Matrix:** 4x5 Grid of Toggle Buttons (Channels 1-20).
    * **OFF State:** Gray.
    * **ON State:** Green (only allowed if Air Flow > Threshold).
* **Flow Control:** Input fields for Air (B), Exhaust (C), Odor (A). "Apply" button sends RS232 commands.

### 3.3 Tab 4: Protocol (协议模式)
* **Layout:** Focus Mode (Minimalist).
* **Display:** Large text for "Current Trial", "Next Odor", "Time Remaining".
* **Controls:**
    * `[ Start Experiment ]` (Green, big).
    * `[ Pause ]` (Yellow).
    * Toggle Switch: "Manual Trigger" vs "External (TTL) Trigger".
* **Safety Gating:** Start is disabled unless airflow > threshold; inline LOW FLOW banner near controls when unsafe.

### 3.4 Tab 1: File (??????)
* **Inputs:** Subject, Condition, Session folder selector; protocol file picker (.txt/.csv) with validation (line errors surfaced inline).
* **Filename Preview:** Shows `{Timestamp}_{Subject}_{Condition}.raw` and paired `.log`; warning if subject/condition empty.
* **Actions:** `[ Load Protocol ]` (validates and parses), `[ Open Session Folder ]`, recent files list.
* **States:** Error banner for malformed protocol lines; disable Start until protocol is valid.

### 3.5 Tab 5: Cleaning (??????)
* **Controls:** `[ Start Cleaning ]`, `[ Abort ]`, `[ Resume ]` (if supported), progress indicator (step/total, elapsed).
* **Status Panel:** Current step name/duration, airflow/safety indicator (must be SAFE to proceed), log window for each valve action.
* **Failure Handling:** On error/abort, automatically closes valves, shows reason, and writes step index + reason + elapsed to log.

### 3.6 Tab 6: Options (??????)
* **Hardware Config:** COM ports, NI device IDs (6001/6501) with inline validation and save; hardware variant selector (10 vs 20 channels) updates Pre-test UI on save.
* **Thresholds & Safety:** Airflow threshold input; option to enable audible/visual LOW FLOW alert.
* **Localization & UI:** Language toggle (Zh-CN default), font size slider; theme colors locked to spec unless dev mode enabled.
* **Persistence:** `[ Save ]` writes to config; `[ Revert ]` restores last saved; dirty-state indicator.

## 4. Visual Standards
* **Framework:** PySide6 Native Style.
* **Colors:**
    * Background: `#F0F0F0` (Windows Default).
    * Graph BG: `#000000`.
    * Danger/Stop: `#DC3545` (Red).
    * Safety/Go: `#28A745` (Green).
* **Typography:** Microsoft YaHei (Windows Chinese Standard), 12px base size.
