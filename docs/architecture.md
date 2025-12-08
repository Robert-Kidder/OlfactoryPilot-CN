# OlfactoryPilot - Architecture Document

## 1. Introduction
This document outlines the architecture for the **Olfactory Stimulation Control Software**, a Windows desktop application for controlling NI hardware and RS232 mass flow controllers. It prioritizes **Hardware Safety**, **Real-Time Precision**, and **Maintainability**.

## 2. High-Level Architecture

### 2.1 Architectural Pattern
**Model-View-Controller (MVC)** with Worker Threads.
* **View:** PySide6 Widgets (Passive).
* **Controller:** Orchestrates logic and dispatches commands.
* **Model:** Global application state.
* **Hardware Worker:** Dedicated `QThread` for real-time control (Jitter Mitigation).

### 2.2 System Diagram
[UI Thread] <-> [Signals/Slots] <-> [Hardware Worker Thread] <-> [HAL] <-> [Hardware]
                                      ^
                                      | (Queue)
                                      v
                                [Data Logger Thread] <-> [Disk]

### 2.3 Safety & Telemetry Flow
- Hardware Worker emits telemetry (connection status, airflow value, safety state) via signals/slots at ~5-10 Hz; UI footer updates within <500ms for LOW FLOW alerts.
- Breath waveform stream runs at 100Hz to the Calibration view; UI targets >=30 FPS rendering.
- Safety gating lives in worker/HAL; UI commands are no-ops if safety state != SAFE.

## 3. Technology Stack
* **Language:** Python 3.10+
* **GUI:** PySide6 (LGPL)
* **Drivers:** `nidaqmx`, `pyserial`
* **Plotting:** `pyqtgraph`
* **Build:** `poetry` (Deps), `pyinstaller` (Exe)

## 4. Source Tree Structure
```text
olfactory-control/
├── app/
�?  ├── __init__.py
�?  ├── main.py
�?  ├── controllers/            # MainController, ProtocolEngine
�?  ├── models/                 # AppState, ProtocolModel
�?  ├── views/                  # MainWindow, Tabs (File, Calib, Pretest...)
�?  ├── workers/                # HardwareWorker, DataLogger
�?  └── services/               # NiDaqService, SerialService, SafetyManager
├── config/
�?  └── default_config.json
├── docs/
�?  ├── prd.md
�?  └── ux-design.md
└── tests/
## 5. Observability & Logging
- **Jitter metrics:** Each actuation log entry captures `timestamp`, `expected_ms`, `actual_ms`, `jitter_ms`; rolling 30s p95 evaluated for warnings.
- **Cleaning runs:** Log step index, step name, elapsed, success/failure reason; abort/failed runs close all valves before exit.
- **Status heartbeat:** Worker publishes connection/airflow/safety telemetry; UI shows last-updated time and raises warnings if data stale or airflow unsafe.
