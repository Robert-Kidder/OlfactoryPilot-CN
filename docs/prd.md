# Product Requirements Document (PRD)
**Project:** OlfactoryPilot
**Version:** 1.0
**Status:** Approved

## 1. Goals and Background Context
### 1.1 Goals
* **Hardware Safety Assurance:** Eliminate the risk of hardware overheating by enforcing strict initialization sequences (Air Flow ON before Heating) and safety interlocks.
* **Localization & Usability:** Deliver a 100% Chinese-localized interface that simplifies the workflow for local researchers.
* **Protocol Reproducibility:** Implement a standardized parameter file format (CSV/TXT) that guarantees identical stimulation sequences.
* **High-Precision Control:** Achieve reliable millisecond-level timing for odor delivery synchronized with breathing signals.
* **Seamless Integration:** Support both manual autonomous operation and external triggering (TTL) from paradigm software like SuperLab.

### 1.2 Background Context
The current workflow relies on a legacy LabView system ("ProgOlfacto") with French documentation. This project refactors that system into a modern **Windows 10/11** desktop application using **Python (PySide6)**. A critical driver is the need to protect the Mass Flow Controllers from damage caused by operating without airflow.

## 2. Requirements

### 2.1 Functional Requirements (FR)
**FR1: Hardware Safety & Initialization**
* **FR1.1:** System performs startup self-check of NI-USB-6001, NI-USB-6501, and RS232 ports.
* [cite_start]**FR1.2 (CRITICAL):** "Safe Start" Interlock: The system must verify Air Flow > Threshold before allowing any Odor Valve activation or heating[cite: 1489].
* **FR1.3:** Auto-Reset: On exit or emergency stop, all valves must reset to "Closed".
* **FR1.4:** Global Toolbar: A persistent toolbar must provide Connect, Reset (Hardware Recovery), Stop (Soft Disconnect), and Help (Manual) buttons.

**FR2: File & Parameter Management**
* **FR2.1:** Auto-generate filenames: `{Timestamp}_{Subject}_{Condition}.raw`.
* **FR2.2:** Parse legacy-compatible experimental protocol files (.txt/.csv).
* **FR2.3:** Save `.raw` (Signal) and `.log` (Event) files for every session.

**FR3: Calibration Module**
* **FR3.1:** Real-time, auto-scaling breathing waveform (100Hz).
* **FR3.2:** Visual threshold setting (Red = Exhale, Yellow = Inhale).

**FR4: Pre-test & Manual Control**
* **FR4.1:** Manual toggle matrix for 20 odor channels.
* **FR4.2:** Flow rate control for Air (B), Exhaust (C), and Odor (A).
* **FR4.3:** Compensation Logic: Automatically calculate `A_comp = A_target + C_target` during resting phases.

**FR5: Protocol Execution**
* **FR5.1:** Modes: Manual Trigger (UI Button) and External Trigger (TTL from SuperLab).
* **FR5.2:** Breath Logic: Wait for signal > Exhale Threshold before stimulation.
* **FR5.3:** Precision: Target <20ms software jitter for valve actuation.

**FR6: Cleaning Module**
* **FR6.1:** Automated sequence to cycle valves and flush residue.

**FR7: Options & Configuration**
* **FR7.1:** Configurable COM ports and NI Device IDs via UI.
* **FR7.2:** Interface language: Simplified Chinese.
* **FR7.3:** Dynamic UI: Options to select hardware variant (10 vs 20 channels) to adapt the Pre-test UI.

### 2.2 Non-Functional Requirements (NFR)
* **NFR1 (Safety):** Hardware safety logic runs in a high-priority thread, independent of UI responsiveness.
* **NFR2 (Performance):** Breathing graph updates at >30 FPS.
* **NFR3 (Licensing):** Use PySide6 (LGPL) to allow for potential closed-source distribution.
* **NFR4 (Reliability):** Robust error handling for USB disconnections (attempt safe shutdown and data save).

## 3. Epic List
* **Epic 1:** Foundation, Safety Interlocks, HAL, and Global Toolbar.
* **Epic 2:** Manual Control (Pre-test) and Visual Calibration.
* **Epic 3:** Protocol Engine, File Parsing, and Data Logging.
* **Epic 4:** External Integration, Cleaning Module, and Polish.