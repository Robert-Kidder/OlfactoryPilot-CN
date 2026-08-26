from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, qInstallMessageHandler

from app.controllers import MainController
from app.main import DEFAULT_CONFIG
from app.models import (
    AppState,
    DeviceLeaseKind,
    ManualExperimentIntent,
    ManualExperimentStatus,
    ManualSupplyIntent,
    ProtocolExecutionState,
)
from app.services import FlowApplyResult, MockHAL
from app.views import MainWindow
from app.workers import (
    ActuationInterlockIngress,
    ActuationWorker,
    FlowCommand,
    FlowCommandResult,
    HardwareWorker,
    InterlockSnapshot,
)


def _wait_until(qt_app, predicate, *, timeout: float = 8.0, detail=None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), detail() if detail is not None else "condition did not become true"


def _runtime_controller(tmp_path: Path) -> tuple[MainController, MainWindow, MockHAL]:
    config = json.loads(Path(DEFAULT_CONFIG).read_text(encoding="utf-8"))
    config["telemetry_hz"] = 10
    config["shutdown_record_path"] = str(tmp_path / "last-shutdown.json")
    config["_local_config_path"] = str(tmp_path / "local-config.json")
    state = AppState.from_config(config)
    state.simulation_mode = True
    state.telemetry.connected = True
    state.telemetry.safety_state = "SAFE"
    state.telemetry.timestamp = time.time()
    state.hardware_ready = True
    hal = MockHAL(base_flow_sccm=1000.0)
    hardware = HardwareWorker(
        telemetry_hz=10,
        hal=hal,
        simulation=True,
    )
    hardware._connected = True
    controller = MainController(state, hardware, config=config)
    window = MainWindow(controller, state)
    controller.bind_view(window)
    return controller, window, hal


def test_direct_ingress_copies_receipt_without_consuming_owner_state() -> None:
    worker = ActuationWorker(
        protocol_state=ProtocolExecutionState(),
        writer=lambda _command: None,
        interlock=ActuationInterlockIngress(InterlockSnapshot()),
    )
    command = FlowCommand(
        command_id="copy-test",
        execution_epoch=0,
        sequence=1,
        mode="manual_supply",
        a=250.0,
        b=750.0,
        c=100.0,
        source="manual:experiment",
        operation_id="copy-operation",
        generation=1,
        lease_token="copy-lease",
    )
    original = FlowCommandResult(
        command=command,
        result=FlowApplyResult(True, "ok", 250.0, 750.0, 100.0, 250.0),
    )
    before = worker.manual_snapshot

    assert worker.enqueue_flow_result_from_producer(original)
    original.result.message = "mutated-after-enqueue"

    with worker._flow_result_mailbox_lock:
        copied = worker._flow_result_mailbox[-1].result
    assert copied is not original
    assert copied.command is not original.command
    assert copied.result is not original.result
    assert copied.result.message == "ok"
    assert worker.manual_snapshot == before


def test_real_qt_three_worker_manual_single_cycle_and_three_cycle_soak(
    tmp_path,
    qt_app,
    monkeypatch,
) -> None:
    ingress_threads: list[int] = []
    consume_threads: list[int] = []
    ingress_commands: list[str] = []
    consume_commands: list[tuple[str, str | None, str]] = []
    presentation_threads: list[int] = []
    notice_contexts: list[tuple[object, ...]] = []
    statuses: list[ManualExperimentStatus] = []
    qt_warnings: list[str] = []
    original_ingress = ActuationWorker.enqueue_flow_result_from_producer
    original_consume = ActuationWorker._consume_manual_flow_result

    def recorded_ingress(self, result):
        ingress_threads.append(threading.get_ident())
        ingress_commands.append(result.command.command_id)
        return original_ingress(self, result)

    def recorded_consume(self, result, *, received_ns=None):
        consume_threads.append(threading.get_ident())
        consume_commands.append(
            (
                result.command.command_id,
                self._manual_pending_flow_id,
                self._manual_pending_flow_role,
            )
        )
        return original_consume(self, result, received_ns=received_ns)

    monkeypatch.setattr(
        ActuationWorker,
        "enqueue_flow_result_from_producer",
        recorded_ingress,
    )
    monkeypatch.setattr(
        ActuationWorker,
        "_consume_manual_flow_result",
        recorded_consume,
    )
    controller, window, _hal = _runtime_controller(tmp_path)
    original_render = window.manual_experiment_view.render_presentation
    original_sync_notice = window.manual_experiment_view._sync_notice_output
    gui_thread_ident = threading.get_ident()

    def recorded_render(presentation):
        presentation_threads.append(threading.get_ident())
        return original_render(presentation)

    window.manual_experiment_view.render_presentation = recorded_render

    def recorded_sync_notice():
        before = window.manual_experiment_view.notice_creation_count
        original_sync_notice()
        if window.manual_experiment_view.notice_creation_count != before:
            notice_contexts.append(
                (
                    window.manual_experiment_view._notice_identity,
                    controller._manual_snapshot.status,
                    controller._manual_snapshot.supply_restored_at,
                    controller._latest_airflow_sample_timestamp,
                    controller.state.telemetry.timestamp,
                )
            )

    window.manual_experiment_view._sync_notice_output = recorded_sync_notice
    controller.actuation_worker.manual_snapshot_ready.connect(
        lambda snapshot: statuses.append(snapshot.status),
        Qt.ConnectionType.DirectConnection,
    )

    def message_handler(_mode, _context, message) -> None:
        text = str(message)
        if "QObject" in text or "different thread" in text:
            qt_warnings.append(text)

    previous_handler = qInstallMessageHandler(message_handler)
    try:
        window.show()
        assert controller.start_worker()
        _wait_until(
            qt_app,
            lambda: all(
                worker.isRunning()
                for worker in (
                    controller.worker,
                    controller.flow_worker,
                    controller.actuation_worker,
                )
            ),
        )
        _wait_until(qt_app, lambda: len(window.manual_experiment_view._flow_history) >= 2)
        _wait_until(
            qt_app,
            lambda: (
                controller._startup_zero_confirmed
                and controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
                and controller.state.telemetry.connected
                and controller.state.hardware_ready
                and controller.state.telemetry.safety_state == "SAFE"
            ),
        )
        assert controller._manual_snapshot.supply_enabled is False
        assert window.manual_experiment_view.apply_flow_button.text() == "开始供气"

        assert controller.handle_manual_supply_requested(
            ManualSupplyIntent(
                enabled=True,
                total_sccm=1000.0,
                sample_a_sccm=250.0,
                vacuum_c_sccm=100.0,
            )
        )
        supply_generation = controller._manual_generation
        _wait_until(
            qt_app,
            lambda: (
                controller._manual_snapshot.identity is not None
                and controller._manual_snapshot.identity.generation == supply_generation
                and controller._manual_snapshot.status
                is ManualExperimentStatus.COMPLETED
            ),
        )
        _wait_until(
            qt_app,
            lambda: (
                window.manual_experiment_view.snapshot.experiment.identity
                == controller._manual_snapshot.identity
                and window.manual_experiment_view.snapshot.experiment.status
                is ManualExperimentStatus.COMPLETED
            ),
        )
        _wait_until(
            qt_app,
            lambda: (
                controller.state.telemetry.safety_state == "SAFE"
                and window.manual_experiment_view.snapshot.can_apply_flow
            ),
        )

        protocol_baseline = (
            controller.actuation_worker.protocol_state.execution_epoch,
            len(controller.actuation_worker.protocol_state.events),
        )
        notice_baseline = window.manual_experiment_view.notice_creation_count
        notice_context_baseline = len(notice_contexts)
        for _cycle in range(3):
            sample_count = len(window.manual_experiment_view._flow_history)
            assert controller.handle_manual_release_requested(
                ManualExperimentIntent(
                    external_ports=(4,),
                    total_sccm=1000.0,
                    sample_a_sccm=250.0,
                    vacuum_c_sccm=100.0,
                    duration_ns=80_000_000,
                )
            )
            expected_generation = controller._manual_generation
            _wait_until(
                qt_app,
                lambda generation=expected_generation: (
                    controller._manual_snapshot.identity is not None
                    and controller._manual_snapshot.identity.generation == generation
                    and controller._manual_snapshot.status
                    is ManualExperimentStatus.COMPLETED
                ),
                detail=lambda generation=expected_generation: (
                    f"expected={generation}; "
                    f"snapshot={controller._manual_snapshot!r}; "
                    f"lease={controller.device_lease.snapshot!r}; "
                    f"mailbox={len(controller.actuation_worker._flow_result_mailbox)}; "
                    f"ingress={ingress_commands!r}; consume={consume_commands!r}"
                ),
            )
            _wait_until(
                qt_app,
                lambda baseline=sample_count: len(
                    window.manual_experiment_view._flow_history
                )
                >= baseline + 2,
            )

            completed = controller._manual_snapshot
            assert completed.close_confirmed == (4,)
            assert completed.possibly_open == ()
            assert completed.flow_zero_confirmed
            assert completed.selector_compensation_confirmed
            assert completed.supply_restored
            assert completed.supply_enabled is True
            assert controller.device_lease.snapshot.kind is DeviceLeaseKind.IDLE
            assert controller.actuation_worker._background_safe_stop_plan is None
            assert controller.actuation_worker.protocol_state.possibly_open_valves == set()

        assert ManualExperimentStatus.RECOVERY_REQUIRED not in statuses
        assert (
            controller.actuation_worker.protocol_state.execution_epoch,
            len(controller.actuation_worker.protocol_state.events),
        ) == protocol_baseline
        assert ingress_threads and consume_threads and presentation_threads
        assert len(set(ingress_threads)) == 1
        assert len(set(consume_threads)) == 1
        assert ingress_threads[0] != consume_threads[0]
        assert set(presentation_threads) == {gui_thread_ident}
        assert gui_thread_ident not in {ingress_threads[0], consume_threads[0]}
        assert window.manual_experiment_view.notice_creation_count - notice_baseline <= 4, (
            "\n".join(map(repr, notice_contexts))
        )
        cycle_notices = notice_contexts[notice_context_baseline:]
        assert not any(
            context[0]
            and context[0][0] == "condition"
            and context[0][1][0] == "safety"
            for context in cycle_notices
        )
        assert window.manual_experiment_view.snapshot.controls_enabled
        assert window.manual_experiment_view.snapshot.can_apply_flow
        assert not qt_warnings
    finally:
        controller.teardown(timeout_ms=3000)
        window.close()
        qt_app.processEvents()
        qInstallMessageHandler(previous_handler)
