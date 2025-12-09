---
stepsCompleted:
  - step-01-validate-prerequisites
  - step-02-design-epics
  - step-03-create-stories
  - step-04-final-validation
inputDocuments:
  - docs/prd.md
  - docs/architecture.md
  - docs/ux-design.md
---

# OlfactoryPilot - Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for OlfactoryPilot, decomposing the requirements from the PRD, UX Design if it exists, and Architecture requirements into implementable stories.

## Requirements Inventory

### Functional Requirements

FR1.1: Startup self-check of NI-USB-6001, NI-USB-6501, and RS232 ports  
FR1.2 (CRITICAL): Safe Start interlock requiring Air Flow > Threshold before any odor valve activation or heating  
FR1.3: Auto-reset to close all valves on exit or emergency stop  
FR1.4: Global toolbar with Connect, Reset (hardware recovery), Stop (soft disconnect), and Help buttons  
FR2.1: Auto-generate filenames as `{Timestamp}_{Subject}_{Condition}.raw`  
FR2.2: Parse legacy-compatible experimental protocol files (.txt/.csv)  
FR2.3: Save `.raw` (signal) and `.log` (event) files for every session  
FR3.1: Real-time, auto-scaling breathing waveform at 100Hz  
FR3.2: Visual threshold setting with distinct exhale (red) and inhale (yellow) cues  
FR4.1: Manual toggle matrix for 20 odor channels  
FR4.2: Flow rate control for Air (B), Exhaust (C), and Odor (A)  
FR4.3: Compensation logic to calculate `A_comp = A_target + C_target` during resting phases  
FR5.1: Protocol execution modes: Manual Trigger (UI button) and External Trigger (TTL from SuperLab)  
FR5.2: Breath logic waits for signal > Exhale Threshold before stimulation  
FR5.3: Target <20ms software jitter for valve actuation  
FR6.1: Cleaning module with automated valve cycling and residue flush  
FR7.1: Configurable COM ports and NI device IDs via UI  
FR7.2: Interface language: Simplified Chinese  
FR7.3: Dynamic UI options for hardware variant selection (10 vs 20 channels) to adapt Pre-test UI  

### NonFunctional Requirements

NFR1 (Safety): Hardware safety logic runs in a high-priority thread, independent of UI responsiveness  
NFR2 (Performance): Breathing graph updates at >30 FPS  
NFR3 (Licensing): Use PySide6 (LGPL) to allow for potential closed-source distribution  
NFR4 (Reliability): Robust error handling for USB disconnections with safe shutdown and data save  

### Additional Requirements

- Architecture: MVC with PySide6 views, controllers orchestrating logic, and a dedicated hardware worker thread for jitter mitigation  
- Architecture: Data logger thread writing to disk; signals/slots queue between UI and worker threads  
- Architecture: Tech stack constraints: Python 3.10+, PySide6, `nidaqmx`, `pyserial`, `pyqtgraph`; packaging with poetry and pyinstaller  
- Architecture: Maintain source tree structure with app/controllers/models/views/workers/services and config/docs/tests directories  
- Architecture: HAL between worker thread and hardware, covering NI DAQ and RS232 mass flow controllers  
- Architecture: Windows 10/11 desktop target with emphasis on hardware safety, real-time precision, and maintainability  
- UX: Top tab bar (File, Calibration, Pre-test, Protocol, Cleaning, Options) with "page-as-subsystem" isolation to reduce cognitive load  
- UX: Persistent status/footer showing connection icon, air flow value, and safety status ("SAFE"/"LOW FLOW" flashing red)  
- UX: Global controls available throughout: Connect, Reset, Stop, Help  
- UX: Calibration visuals—black graph background, white signal line, draggable red (exhale) and yellow (inhale) thresholds, LED indicators, auto-scale button, numeric spinners  
- UX: Pre-test valve matrix 4x5 toggle grid with safe-on gating (Air Flow > Threshold) and gray/green states  
- UX: Protocol mode focus layout with current trial/next odor/time remaining, start/pause buttons, and manual vs TTL trigger toggle  
- UX: Chinese localization across UI (Microsoft YaHei, 12px base); color standards (background #F0F0F0, danger #DC3545, go #28A745)  

### FR Coverage Map

FR1.1: Epic 1 - Startup safety and device validation foundation  
FR1.2: Epic 1 - Safe Start interlock for airflow-gated control  
FR1.3: Epic 1 - Safe shutdown and valve reset  
FR1.4: Epic 1 - Global toolbar for safety controls  
FR2.1: Epic 3 - File/session management and logging  
FR2.2: Epic 3 - Protocol file parsing (.txt/.csv)  
FR2.3: Epic 3 - Raw signal and event log output per session  
FR3.1: Epic 2 - Calibration waveform visualization and autoscale  
FR3.2: Epic 2 - Threshold tuning with visual cues  
FR4.1: Epic 2 - Pre-test manual valve matrix  
FR4.2: Epic 2 - Flow rate controls for Air/Exhaust/Odor  
FR4.3: Epic 2 - Compensation logic for resting phases  
FR5.1: Epic 3 - Protocol execution modes (manual vs TTL)  
FR5.2: Epic 3 - Breath-gated stimulation logic  
FR5.3: Epic 3 - <20ms jitter target for actuation  
FR6.1: Epic 4 - Cleaning automation  
FR7.1: Epic 4 - Configurable COM/NI IDs  
FR7.2: Epic 4 - Chinese UI language support  
FR7.3: Epic 2 - Hardware variant selection in Pre-test UI  

## Epic List

### Epic 1: Safe Hardware Foundations
建立安全硬件控制基础，完成上电自检、气流安全联锁、全局安全工具栏，并确保退出/急停时硬件安全复位。
**FRs covered:** FR1.1, FR1.2, FR1.3, FR1.4

#### Story 1.0: Project Scaffold and CI Baseline
As a developer,  
I want a ready-to-run PySide6/MVC scaffold with lint/test/packaging CI,  
So that the team can build and ship safely on day one.

**Acceptance Criteria:**
- **Given** the repo is initialized  
  **When** I run dependency setup (poetry/pip)  
  **Then** the PySide6 MVC skeleton (views/controllers/workers/services) launches a placeholder window without errors.
- **Given** tooling is installed  
  **When** I run lint and tests (ruff/flake8 + pytest)  
  **Then** they pass locally and in CI without manual edits.
- **Given** packaging is configured  
  **When** I run the packaging job (pyinstaller)  
  **Then** it produces a Windows executable artifact and logs size/hash/path.
- **Given** CI is triggered on push/PR  
  **When** the pipeline runs  
  **Then** it executes lint, tests, and packaging, failing the build on any error.

#### Story 1.1: Device Self-Check and Status Report
As a lab technician,  
I want the system to verify NI-USB-6001/6501 and RS232 ports at startup and show a status report,  
So that I know the hardware is connected and safe to proceed.

**Acceptance Criteria:**
- **Given** the app launches  
  **When** startup begins  
  **Then** it checks NI-USB-6001, NI-USB-6501, and RS232 connectivity and baud settings  
  **And** shows a status summary (pass/fail per device) with any detected errors.
- **Given** a device is missing or baud mismatch  
  **When** checks run  
  **Then** the system blocks control actions and shows a clear error with retry guidance.

#### Story 1.2: Safe Start Airflow Interlock
As a lab technician,  
I want airflow > threshold to be required before any valve or heater activation,  
So that hardware cannot overheat or run without proper airflow.

**Acceptance Criteria:**
- **Given** the system is idle  
  **When** airflow is below the configured threshold  
  **Then** valve/heater commands are blocked and a “LOW FLOW” warning is shown.
- **Given** airflow rises above threshold  
  **When** commands are issued  
  **Then** valve/heater operations proceed normally.
- **Given** airflow drops below threshold mid-operation  
  **When** the condition is detected  
  **Then** valves/heater are safely shut down and a warning is surfaced.

#### Story 1.3: Global Safety Toolbar
As a lab technician,  
I want persistent Connect, Reset (hardware recovery), Stop (soft disconnect), and Help controls,  
So that I can safely initialize, recover, halt, or access the manual at any time.

**Acceptance Criteria:**
- **Given** the app is open  
  **When** I click Connect  
  **Then** it runs the device self-check and initializes connections with clear success/fail feedback.
- **Given** a hardware fault or mismatch  
  **When** I click Reset  
  **Then** it re-handshakes NI/RS232, closes valves, and reports status.
- **Given** I need to halt safely  
  **When** I click Stop  
  **Then** it stops active operations, closes valves, and leaves UI responsive.
- **Given** I need documentation  
  **When** I click Help  
  **Then** the local manual/PDF opens in Chinese.

#### Story 1.4: Safe Shutdown and Valve Reset
As a lab technician,  
I want the system to close all valves and stop heaters on exit or emergency stop,  
So that hardware always returns to a safe state.

**Acceptance Criteria:**
- **Given** I exit the app or press emergency stop  
  **When** shutdown is triggered  
  **Then** all valves close, heaters stop, and the action is logged.
- **Given** shutdown completes  
  **When** I relaunch  
  **Then** the system starts from a clean/safe state with prior issues reported.

### Epic 2: Calibration & Manual Control
让用户安全地查看呼吸信号、设定阈值、手动切换阀门与流量，支持不同硬件规格的预检操作。
**FRs covered:** FR3.1, FR3.2, FR4.1, FR4.2, FR4.3, FR7.3

#### Story 2.1: Real-Time Breath Visualization
As a researcher,  
I want a 100Hz breathing waveform with auto-scale,  
So that I can see stable signals for calibration.

**Acceptance Criteria:**
- **Given** sensors stream data  
  **When** the graph renders  
  **Then** it updates at >=30 FPS (measured over a 10s sliding window; avg + p95 FPS logged) with auto-scale toggle and black/white visual standard.
- **Given** FPS drops below 30 for more than 2 seconds  
  **When** detected  
  **Then** a warning surfaces and is recorded in the session log.
- **Given** thresholds are visible  
  **When** I drag red (exhale) or yellow (inhale) lines  
  **Then** values update and LED indicators reflect crossings in real time.

#### Story 2.2: Threshold Tuning and Feedback
As a researcher,  
I want draggable inhale/exhale thresholds with numeric fine-tune,  
So that gating is precise and repeatable.

**Acceptance Criteria:**
- **Given** thresholds are set  
  **When** I adjust via drag or numeric spinners  
  **Then** both the graph and numeric fields stay in sync, persisting across sessions.
- **Given** signal crosses thresholds  
  **When** events occur  
  **Then** LED indicators light and a status label shows current gating state.

#### Story 2.3: Valve Matrix Manual Control
As a lab technician,  
I want a 4x5 toggle matrix for 20 odor valves with safe-on gating,  
So that I can manually test channels without bypassing airflow safety.

**Acceptance Criteria:**
- **Given** airflow < threshold  
  **When** I try to enable a valve  
  **Then** the action is blocked and a “LOW FLOW” message appears.
- **Given** airflow ≥ threshold  
  **When** I toggle a valve  
  **Then** it switches state (gray→green) and logs the change.
- **Given** hardware variant differs (10 vs 20 channels)  
  **When** selected in settings  
  **Then** the matrix displays the appropriate number of toggles.

#### Story 2.4: Flow Rate Controls
As a lab technician,  
I want to set flow rates for Air (B), Exhaust (C), and Odor (A) with apply action,  
So that I can calibrate desired flows safely.

**Acceptance Criteria:**
- **Given** airflow is below threshold  
  **When** I click Apply  
  **Then** the action is blocked, a LOW FLOW warning is shown, and no RS232 command is sent (aligns with FR1.2).
- **Given** numeric inputs are available  
  **When** I enter targets and click Apply  
  **Then** commands send to RS232 with confirmation or error feedback.
- **Given** compensation is needed  
  **When** resting phases occur  
  **Then** `A_comp = A_target + C_target` is applied automatically and displayed.

#### Story 2.5: Variant-Aware Pre-Test UI
As a lab technician,  
I want the Pre-test UI to adapt to 10 or 20 channel hardware,  
So that controls stay aligned with the connected device.

**Acceptance Criteria:**
- **Given** I select hardware variant  
  **When** the UI renders  
  **Then** valve toggles and flow controls reflect the chosen configuration.
- **Given** variant changes mid-session  
  **When** updated  
  **Then** UI refreshes safely without leaving stale valve states.

### Epic 3: Protocol Execution & Data Logging
支持协议文件解析、呼吸门控的实验执行、手动/TTL 触发模式，并输出高精度信号与日志文件，满足 <20ms 抖动要求。
**FRs covered:** FR2.1, FR2.2, FR2.3, FR5.1, FR5.2, FR5.3

#### Story 3.1: Protocol File Parsing (.txt/.csv)
As a researcher,  
I want to load legacy-compatible protocol files,  
So that I can run existing stimulation sequences without re-authoring.

**Acceptance Criteria:**
- **Given** a valid .txt or .csv protocol file  
  **When** I load it  
  **Then** the system parses trials, timing, valves, and metadata; errors are reported with line numbers.
- **Given** malformed rows  
  **When** detected  
  **Then** loading fails with a clear message and no partial run state.

#### Story 3.2: Breath-Gated Stimulation
As a researcher,  
I want stimulation to wait for exhale threshold before triggering,  
So that delivery aligns with the breathing cycle.

**Acceptance Criteria:**
- **Given** a running protocol  
  **When** a trial is ready  
  **Then** the system waits for exhale signal > threshold before actuating valves.
- **Given** threshold not reached within timeout  
  **When** condition occurs  
  **Then** it logs a skip or retry per configuration and notifies the user.

#### Story 3.3: Manual vs TTL Trigger Modes
As a researcher,  
I want to switch between manual trigger button and external TTL trigger,  
So that experiments integrate with SuperLab or run standalone.

**Acceptance Criteria:**
- **Given** mode = Manual  
  **When** I press Start  
  **Then** trials advance per protocol timing with UI progress (current/next/time remaining).
- **Given** mode = TTL  
  **When** TTL pulses arrive  
  **Then** trials advance on pulse, and UI shows received pulses; missing pulses are logged.
- **Given** mode changes  
  **When** switched  
  **Then** the system safely transitions without leaving stale state.

#### Story 3.4: Low-Jitter Actuation (<20ms)
As a lab technician,  
I want valve actuation jitter under 20ms,  
So that timing is reliable for experiments.

**Acceptance Criteria:**
- **Given** hardware worker thread handles actuation  
  **When** trials run  
  **Then** measured software jitter (expected vs actual command time delta) stays under 20ms, logged per actuation (timestamp, expected_ms, actual_ms, jitter_ms).
- **Given** jitter exceeds 20ms in a rolling 30s window (p95 > 20ms or any single >30ms)  
  **When** detected  
  **Then** the system logs the incident, surfaces a warning with suggested mitigations, and pauses new actuations or falls back to a safe-degraded mode until jitter recovers.

#### Story 3.5: Session File Naming and Logging
As a researcher,  
I want auto-generated filenames and paired signal/log outputs,  
So that data is consistent and traceable.

**Acceptance Criteria:**
- **Given** subject/condition inputs  
  **When** a session starts  
  **Then** it creates `{Timestamp}_{Subject}_{Condition}.raw` plus `.log` in the session folder.
- **Given** a session completes  
  **When** saving  
  **Then** both signal and event logs are stored with run metadata (mode, thresholds, variant).
- **Given** a write failure occurs (disk full/permission)  
  **When** saving  
  **Then** the app shows an error, rolls back partial files, logs the failure (path, error, timestamp), and offers retry after the issue is fixed.

### Epic 4: Operations, Cleaning & Localization
提供清洗流程、设备配置（串口/NI ID）、中文界面与整体运维要素，便于安全运行与本地化交付。
**FRs covered:** FR6.1, FR7.1, FR7.2

#### Story 4.1: Cleaning Automation
As a lab technician,  
I want an automated cleaning sequence,  
So that residue is flushed without manual valve scripting.

**Acceptance Criteria:**
- **Given** cleaning mode is available  
  **When** I start it  
  **Then** valves cycle through the prescribed pattern and durations, respecting airflow safety.
- **Given** a cleaning step fails or the user aborts  
  **When** the run stops  
  **Then** all valves close safely and the log records step index, failure reason, and elapsed time.

#### Story 4.2: Configurable COM and NI IDs
As a lab technician,  
I want to set COM ports and NI device IDs via the UI,  
So that the system matches our hardware wiring.

**Acceptance Criteria:**
- **Given** settings UI is open  
  **When** I enter COM/NI IDs and save  
  **Then** values persist and are used on next Connect.
- **Given** invalid IDs are entered  
  **When** saved  
  **Then** validation errors show and connections are blocked until fixed.

#### Story 4.3: Chinese UI Localization
As a researcher,  
I want the interface fully in Simplified Chinese,  
So that local users can operate without language friction.

**Acceptance Criteria:**
- **Given** UI renders  
  **When** I navigate tabs  
  **Then** labels, buttons, statuses, and help content are in Chinese (微软雅黑 12px).
- **Given** errors/warnings occur  
  **When** displayed  
  **Then** messages are Chinese-localized, concise, and use consistent phrasing (e.g., “协议文件无效，第 {line} 行格式错误”; “气流不足，请检查管路后重试”; “写入失败，请释放磁盘空间或检查权限”).
