# Sprint Change Proposal: Hardware Simulation Strategy
**Date:** 2025-12-10
**Status:** Approved
**Trigger:** Lack of physical hardware availability for development and testing.

## 1. Issue Summary
The development team currently lacks access to the physical OlfactoryPilot hardware (NI DAQ, Alicat MFCs). This blocks the implementation and verification of critical features including hardware safety interlocks (Epic 1), calibration (Epic 2), and protocol execution (Epic 3). Without a mitigation strategy, the project cannot proceed to verify Functional Requirements.

## 2. Impact Analysis
*   **Epic 1 (Foundations):** `Device Self-Check` and `Safe Start` cannot be verified.
*   **Epic 2 (Calibration):** No breath signal source available for visualization or threshold tuning.
*   **Epic 3 (Protocol):** Cannot verify valve actuation timing or breath-gated triggers.
*   **Risk:** Developing "blind" without hardware feedback leads to high integration risk and potential safety failures when hardware eventually arrives.

## 3. Recommended Approach
**Implement a comprehensive Hardware Simulation Layer (Mock HAL).**

*   **Strategy:** Develop a software-only mode (`--simulation`) that mimics hardware behavior.
*   **Scope:**
    *   Simulates NI USB-6001/6501 digital I/O states.
    *   Generates synthetic breath waveforms (sine/noise) on AI0.
    *   Simulates 3 Alicat MFCs on a single RS232 port with correct state logic.
    *   Validates complex "Compensation Logic" (MFC A/B/C + Master Valve interactions) in software.
*   **Benefit:** Allows full functional testing of the UI, safety logic, and protocol engine without physical devices.

## 4. Detailed Change Proposals

### 4.1 PRD Updates
*   **Added FR8: Hardware Simulation Mode.**
    *   System must support a `--simulation` launch flag.
    *   UI must clearly indicate "[SIMULATION]" status.
    *   Simulation must provide synthetic sensor data and mimic actuator responses.
*   **Updated FR1.2:** Safety interlocks must accept simulated airflow values in simulation mode.

### 4.2 Architecture Updates
*   **Mock HAL Layer:** Added to system diagram.
    *   Intercepts calls from `HardwareWorker`.
    *   Maintains internal state (e.g., "Virtual Airflow", "Valve States").
    *   Returns realistic responses (e.g., `b'A 1000.0 25.0 ...\r'` for Alicat poll).

### 4.3 Epic/Story Updates
*   **New Story 1.5:** Hardware Simulation Layer (Mock HAL) implementation.
*   **New Story 4.4:** Refined Compensation Logic & Automation (implementing the specific A/B/C + Master Valve flow rules).
*   **Updated Stories (2.1, 2.3, 2.4, 3.2):** Acceptance criteria now explicitly include "physical OR simulated" verification.

## 5. Implementation Handoff
*   **Scope Classification:** **Major** (Requires new architectural component).
*   **Route To:** Development Team (Lead Developer / Architect).
*   **Immediate Actions:**
    1.  Implement **Story 1.5** (Mock HAL) immediately to unblock Epic 1 reviews.
    2.  Update **Story 4.4** logic in the Controller/Service layer.
    3.  Ensure CI pipeline runs tests in Simulation Mode.

## 6. Success Criteria
*   The application launches in simulation mode without errors.
*   "Breath" waveform is visible and adjustable in Calibration tab without hardware.
*   Complex flow compensation logic (Rest vs. Stim) can be verified via logs in simulation.
