# Epic 1 Retrospective — Safe Hardware Foundations
Status: Completed  
Scope: Stories 1.0–1.4 (scaffold/CI, self-check, safe start interlock, global toolbar, safe shutdown)

## What went well
- Unified safety stack: `SafetyManager` now owns threshold validation, hysteresis, data-stale detection, and command guards; shutdown flow centralized via `ShutdownService` with persisted records.
- Global toolbar connects/reset/stop/help buttons are safety-gated with clear Chinese tooltips/status, reflecting hardware readiness, last shutdown, and self-check outcomes.
- Self-check pipeline covers NI/RS232 detection with structured `SelfCheckResult`, feeds status bar and view, and blocks unsafe commands by default.
- Tests are in place and fast: `python -m pytest tests/test_safety_manager.py` passed (12 cases); controller/view/worker integration behaviors covered in `tests/test_app.py`.

## What needs improvement
- Hardware-in-loop coverage is still mocked; need runs with actual NI-USB-6001/6501 and RS232 mass flow hardware to validate detection, airflow telemetry, and shutdown timing.
- Packaging not re-verified after adding shutdown record paths and the local manual PDF; PyInstaller spec may need resource inclusion and path adjustments.
- Telemetry performance (FPS, latency) and low-flow detection under noisy signals are unmeasured; real sensor data may require smoothing or debounce tuning.
- Operator workflows for unresolved/unsafe shutdown banners and manual recovery are not documented in the user manual.

## Risks / unknowns
- NI/serial driver availability and permissions on lab PCs could break self-check; current guidance is only in code/tooltips.
- Persisted shutdown records live outside config; failure to write (permissions/disk) is only logged—UI fallback is minimal.
- Manual PDF path defaults may not exist in packaged builds; Help UX depends on OS association for `.pdf`.

## Next actions
- Run full `python -m pytest` and a hardware smoke test with NI/RS232 attached; capture logs for failures.
- Dry-run `python -m PyInstaller pyinstaller.spec`; confirm inclusion of manual PDF and shutdown record path, adjust spec if missing.
- Add operator playbook snippets (and link in Help/manual) for unsafe shutdown recovery and for re-checking after hardware reconnection.
- After hardware validation/code review, promote stories 1.0–1.4 and close Epic 1.

