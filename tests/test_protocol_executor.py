from __future__ import annotations

from pathlib import Path

import pytest

from app.models import ProtocolDocument, ProtocolTrial, TriggerMode
from app.models.protocol_execution import ProtocolExecutionReadiness, ProtocolExecutionStatus
from app.services.gating_service import GatingService, GatingState
from app.services.hal import AnalogInputFrame, BreathSampleBatch
from app.services.protocol_executor import ProtocolExecutionConfig, ProtocolExecutor


def _document() -> ProtocolDocument:
    return ProtocolDocument(
        source_path=Path("demo.csv"),
        source_name="demo.csv",
        trials=[
            ProtocolTrial(
                trial_id="trial-1",
                timing_ms=0,
                duration_ms=100,
                valve=3,
                trigger=TriggerMode.MANUAL,
                metadata={"odor": "rose"},
            ),
            ProtocolTrial(
                trial_id="trial-2",
                timing_ms=1000,
                duration_ms=50,
                valve=4,
                trigger=TriggerMode.TTL,
                metadata={},
            ),
        ],
    )


def _executor(*, clock_value: float = 10.0, actions: list | None = None) -> ProtocolExecutor:
    action_log = actions if actions is not None else []

    def valve_writer(channel: int, open_state: bool) -> tuple[bool, str]:
        action_log.append((channel, open_state))
        return True, "ok"

    return ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=valve_writer,
        config=ProtocolExecutionConfig(
            breath_gate_timeout_ms=500,
            breath_gate_timeout_action="skip",
            breath_gate_max_retries=1,
        ),
        clock=lambda: clock_value,
    )


def _readiness(**overrides) -> ProtocolExecutionReadiness:
    values = {
        "connected": True,
        "hardware_ready": True,
        "flow_setpoints_ready": True,
        "safety_state": "SAFE",
        "ttl_input_ready": True,
    }
    values.update(overrides)
    return ProtocolExecutionReadiness(**values)


def _start_manual(
    executor: ProtocolExecutor,
    document: ProtocolDocument | None = None,
    *,
    timestamp: float = 10.0,
) -> None:
    executor.start(document or _document(), readiness=_readiness(), timestamp=timestamp)
    executor.accept_trigger(
        TriggerMode.MANUAL,
        readiness=_readiness(ttl_input_ready=False),
        timestamp=timestamp,
    )


def test_start_without_protocol_stays_idle_and_prompts_user() -> None:
    executor = _executor()

    result = executor.start(None, safety_state="SAFE", timestamp=10.0)

    assert result.state.status == ProtocolExecutionStatus.IDLE
    assert result.events[-1].event == "invalid_protocol"
    assert "请先加载有效协议" in result.events[-1].message


def test_start_prepares_first_trial_without_opening_valve() -> None:
    actions: list[tuple[int, bool]] = []
    executor = _executor(actions=actions)

    result = executor.start(_document(), readiness=_readiness(), timestamp=10.0)

    assert result.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
    assert result.state.trial_index == 0
    assert result.state.current_trial.trial_id == "trial-1"
    assert result.state.declared_mode == TriggerMode.MANUAL
    assert result.state.current_mode == TriggerMode.MANUAL
    assert result.state.waiting_trigger_started_at == 10.0
    assert result.state.waiting_started_at is None
    assert result.events[-1].event == "trigger_wait_start"
    assert actions == []


def test_manual_trigger_advances_once_then_requires_exhale() -> None:
    actions: list[tuple[int, bool]] = []
    executor = _executor(actions=actions)
    executor.start(_document(), readiness=_readiness(), timestamp=10.0)

    accepted = executor.accept_trigger(
        TriggerMode.MANUAL,
        readiness=_readiness(ttl_input_ready=False),
        timestamp=10.1,
    )
    duplicate = executor.accept_trigger(
        TriggerMode.MANUAL,
        readiness=_readiness(ttl_input_ready=False),
        timestamp=10.2,
    )

    assert accepted.state.status == ProtocolExecutionStatus.WAITING_EXHALE
    assert accepted.events[-1].event == "trigger_accepted"
    assert duplicate.events[-1].result == "ignored"
    assert duplicate.state.trial_index == 0
    assert duplicate.state.waiting_started_at == 10.1
    assert actions == []

    executor.process_breath_samples([-0.6], safety_state="SAFE", timestamp_start=10.3)
    assert actions == [(3, True)]


def test_ttl_trigger_rejects_stale_epoch_without_mutating_wait_state() -> None:
    actions: list[tuple[int, bool]] = []
    executor = _executor(actions=actions)
    executor.reset(_document())
    executor.set_trigger_mode(TriggerMode.TTL, readiness=_readiness(), timestamp=9.9)
    executor.start(readiness=_readiness(), timestamp=10.0)
    before = executor.snapshot(timestamp=10.0, readiness=_readiness())

    result = executor.accept_trigger(
        TriggerMode.TTL,
        readiness=_readiness(),
        timestamp=10.1,
        captured_epoch=executor.state.arm_epoch - 1,
        sequence=1,
    )
    after = executor.snapshot(timestamp=10.1, readiness=_readiness())

    assert result.events[-1].result == "ignored"
    assert "陈旧" in result.events[-1].message
    assert after.status == before.status == ProtocolExecutionStatus.WAITING_TRIGGER
    assert after.arm_epoch == before.arm_epoch
    assert result.state.waiting_started_at is None
    assert result.state.retry_count == 0
    assert actions == []


def test_reset_keeps_trigger_epoch_monotonic_across_protocol_replacement() -> None:
    executor = _executor()
    second = ProtocolDocument(
        source_path=Path("replacement.csv"),
        source_name="replacement.csv",
        trials=[
            ProtocolTrial(
                trial_id="replacement-1",
                timing_ms=0,
                duration_ms=100,
                valve=5,
                trigger=TriggerMode.TTL,
            )
        ],
    )
    executor.reset(_document())
    executor.set_trigger_mode(TriggerMode.TTL, readiness=_readiness(), timestamp=9.0)
    executor.start(readiness=_readiness(), timestamp=10.0)
    queued_epoch = executor.state.arm_epoch

    executor.reset(second, timestamp=10.1)
    executor.start(readiness=_readiness(), timestamp=10.2)
    result = executor.accept_trigger(
        TriggerMode.TTL,
        readiness=_readiness(),
        timestamp=10.3,
        captured_epoch=queued_epoch,
        sequence=1,
    )

    assert executor.state.arm_epoch > queued_epoch
    assert result.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
    assert result.events[-1].event == "ttl_pulse_ignored"
    assert result.events[-1].result == "ignored"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"connected": False}, "连接"),
        ({"hardware_ready": False}, "自检"),
        ({"flow_setpoints_ready": False}, "流量"),
        ({"safety_state": "LOW_FLOW"}, "SAFE"),
    ],
)
def test_start_rejects_each_common_readiness_failure_without_state_change(
    overrides: dict,
    reason: str,
) -> None:
    executor = _executor()
    executor.reset(_document())
    before = (
        executor.state.status,
        executor.state.trial_index,
        executor.state.current_mode,
        executor.state.arm_epoch,
        executor.state.waiting_trigger_started_at,
        executor.state.waiting_started_at,
        executor.state.active_valve,
    )

    result = executor.start(readiness=_readiness(**overrides), timestamp=10.0)

    assert result.events[-1].result == "rejected"
    assert reason in result.events[-1].message
    assert before == (
        executor.state.status,
        executor.state.trial_index,
        executor.state.current_mode,
        executor.state.arm_epoch,
        executor.state.waiting_trigger_started_at,
        executor.state.waiting_started_at,
        executor.state.active_valve,
    )


def test_start_with_candidate_document_rejects_readiness_without_replacing_state() -> None:
    executor = _executor()
    original = _document()
    candidate = ProtocolDocument(
        source_path=Path("candidate.csv"),
        source_name="candidate.csv",
        trials=[ProtocolTrial("candidate-ttl", 0, 100, 9, TriggerMode.TTL)],
    )
    executor.reset(original)
    before = (
        executor.state.document,
        executor.state.status,
        executor.state.trial_index,
        executor.state.current_mode,
        executor.state.arm_epoch,
    )

    result = executor.start(
        candidate,
        readiness=_readiness(ttl_input_ready=False),
        timestamp=10.0,
    )

    assert result.events[-1].result == "rejected"
    assert "AI6" in result.events[-1].message
    assert before == (
        executor.state.document,
        executor.state.status,
        executor.state.trial_index,
        executor.state.current_mode,
        executor.state.arm_epoch,
    )


@pytest.mark.parametrize(
    "status",
    [
        ProtocolExecutionStatus.WAITING_TRIGGER,
        ProtocolExecutionStatus.WAITING_EXHALE,
        ProtocolExecutionStatus.TRIGGERED,
        ProtocolExecutionStatus.BLOCKED,
        ProtocolExecutionStatus.STOPPED,
        ProtocolExecutionStatus.COMPLETED,
    ],
)
def test_start_with_candidate_document_rejects_illegal_existing_state_atomically(
    status: ProtocolExecutionStatus,
) -> None:
    executor = _executor()
    original = _document()
    candidate = ProtocolDocument(
        source_path=Path("candidate.csv"),
        source_name="candidate.csv",
        trials=[ProtocolTrial("candidate", 0, 100, 9, TriggerMode.MANUAL)],
    )
    executor.reset(original)
    executor.state.status = status
    before = (
        executor.state.document,
        executor.state.status,
        executor.state.trial_index,
        executor.state.current_mode,
        executor.state.arm_epoch,
        executor.state.active_valve,
    )

    result = executor.start(candidate, readiness=_readiness(), timestamp=10.0)

    assert result.events[-1].event == "start_rejected"
    assert result.events[-1].result == "rejected"
    assert before == (
        executor.state.document,
        executor.state.status,
        executor.state.trial_index,
        executor.state.current_mode,
        executor.state.arm_epoch,
        executor.state.active_valve,
    )

def test_ready_mode_override_does_not_mutate_frozen_trial_and_start_preserves_it() -> None:
    document = _document()
    executor = _executor()
    executor.reset(document)

    changed = executor.set_trigger_mode(TriggerMode.TTL, readiness=_readiness(), timestamp=9.0)
    started = executor.start(readiness=_readiness(), timestamp=10.0)

    assert changed.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
    assert started.state.current_mode == TriggerMode.TTL
    assert started.state.mode_override == TriggerMode.TTL
    assert document.trials[0].trigger == TriggerMode.MANUAL
    assert started.state.ttl_armed is False


def test_running_mode_switch_clears_waits_retry_and_invalidates_epoch() -> None:
    executor = _executor()
    _start_manual(executor)
    executor.state.retry_count = 1
    old_epoch = executor.state.arm_epoch

    result = executor.set_trigger_mode(TriggerMode.TTL, readiness=_readiness(), timestamp=10.2)

    assert result.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
    assert result.state.current_mode == TriggerMode.TTL
    assert result.state.arm_epoch > old_epoch
    assert result.state.waiting_trigger_started_at == 10.2
    assert result.state.waiting_started_at is None
    assert result.state.retry_count == 0
    assert result.state.ttl_armed is False


@pytest.mark.parametrize(
    "status",
    [
        ProtocolExecutionStatus.IDLE,
        ProtocolExecutionStatus.BLOCKED,
        ProtocolExecutionStatus.STOPPED,
        ProtocolExecutionStatus.COMPLETED,
    ],
)
def test_mode_switch_rejects_every_illegal_status_without_mutation(
    status: ProtocolExecutionStatus,
) -> None:
    executor = _executor()
    executor.reset(_document())
    executor.state.status = status
    before = (
        executor.state.status,
        executor.state.current_mode,
        executor.state.mode_override,
        executor.state.arm_epoch,
        executor.state.trial_index,
        executor.state.active_valve,
    )

    result = executor.set_trigger_mode(TriggerMode.TTL, readiness=_readiness(), timestamp=10.0)

    assert result.events[-1].result == "rejected"
    assert before == (
        executor.state.status,
        executor.state.current_mode,
        executor.state.mode_override,
        executor.state.arm_epoch,
        executor.state.trial_index,
        executor.state.active_valve,
    )


@pytest.mark.parametrize(
    "status",
    [
        ProtocolExecutionStatus.READY,
        ProtocolExecutionStatus.WAITING_TRIGGER,
        ProtocolExecutionStatus.WAITING_EXHALE,
        ProtocolExecutionStatus.TRIGGERED,
    ],
)
def test_mode_switch_accepts_complete_legal_status_matrix(status: ProtocolExecutionStatus) -> None:
    actions: list[tuple[int, bool]] = []
    executor = _executor(actions=actions)
    executor.reset(_document())
    if status != ProtocolExecutionStatus.READY:
        executor.start(readiness=_readiness(), timestamp=10.0)
    if status in {ProtocolExecutionStatus.WAITING_EXHALE, ProtocolExecutionStatus.TRIGGERED}:
        executor.accept_trigger(
            TriggerMode.MANUAL,
            readiness=_readiness(ttl_input_ready=False),
            timestamp=10.1,
        )
    if status == ProtocolExecutionStatus.TRIGGERED:
        executor.process_breath_samples([-0.6], safety_state="SAFE", timestamp_start=10.2)

    result = executor.set_trigger_mode(TriggerMode.TTL, readiness=_readiness(), timestamp=10.3)

    assert result.events[-1].event == "mode_changed"
    assert result.state.current_mode == TriggerMode.TTL
    assert result.state.mode_override == TriggerMode.TTL
    assert result.state.status == (
        ProtocolExecutionStatus.READY
        if status == ProtocolExecutionStatus.READY
        else ProtocolExecutionStatus.WAITING_TRIGGER
    )
    assert result.state.active_valve is None


def test_triggered_mode_switch_close_failure_preserves_mode_epoch_and_valve() -> None:
    actions: list[tuple[int, bool]] = []

    def writer(channel: int, open_state: bool) -> tuple[bool, str]:
        actions.append((channel, open_state))
        return (True, "ok") if open_state else (False, "关闭失败")

    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=writer,
        clock=lambda: 10.0,
    )
    _start_manual(executor)
    executor.process_breath_samples([-0.6], safety_state="SAFE", timestamp_start=10.1)
    old_epoch = executor.state.arm_epoch

    result = executor.set_trigger_mode(TriggerMode.TTL, readiness=_readiness(), timestamp=10.2)

    assert result.state.status == ProtocolExecutionStatus.BLOCKED
    assert result.state.current_mode == TriggerMode.MANUAL
    assert result.state.arm_epoch == old_epoch
    assert result.state.active_valve == 3
    assert result.events[-1].result == "close_failed"
    assert actions == [(3, True), (3, False)]


def test_safety_block_requires_explicit_rearm_and_rejects_stale_pulse() -> None:
    executor = _executor()
    executor.reset(_document())
    executor.set_trigger_mode(TriggerMode.TTL, readiness=_readiness(), timestamp=9.0)
    executor.start(readiness=_readiness(), timestamp=10.0)
    stale_epoch = executor.state.arm_epoch
    executor.handle_safety_update("DATA_STALE", timestamp=10.1)

    assert executor.handle_safety_update("SAFE", timestamp=10.2).events == []
    assert executor.state.status == ProtocolExecutionStatus.BLOCKED

    rearmed = executor.rearm_current(readiness=_readiness(), timestamp=10.3)
    stale = executor.accept_trigger(
        TriggerMode.TTL,
        readiness=_readiness(),
        timestamp=10.4,
        captured_epoch=stale_epoch,
        sequence=1,
    )

    assert rearmed.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
    assert rearmed.state.arm_epoch > stale_epoch
    assert stale.events[-1].result == "ignored"
    assert stale.state.status == ProtocolExecutionStatus.WAITING_TRIGGER


def test_valid_ttl_pulse_preserves_capture_timestamp_and_identity() -> None:
    executor = _executor()
    executor.reset(_document())
    executor.set_trigger_mode(TriggerMode.TTL, readiness=_readiness(), timestamp=9.0)
    executor.start(readiness=_readiness(), timestamp=10.0)
    executor.state.ttl_armed = True

    result = executor.accept_trigger(
        TriggerMode.TTL,
        readiness=_readiness(),
        timestamp=10.125,
        captured_epoch=executor.state.arm_epoch,
        sequence=42,
    )

    assert result.state.status == ProtocolExecutionStatus.WAITING_EXHALE
    assert result.state.last_ttl_timestamp == 10.125
    event = result.events[-1]
    assert event.timestamp == 10.125
    assert event.arm_epoch == result.state.arm_epoch
    assert event.pulse_sequence == 42
    assert event.trigger_source == "ttl"


def test_matching_ttl_pulse_before_hardware_arm_ack_is_ignored() -> None:
    executor = _executor()
    executor.reset(_document())
    executor.set_trigger_mode(TriggerMode.TTL, readiness=_readiness(), timestamp=9.0)
    executor.start(readiness=_readiness(), timestamp=10.0)

    result = executor.accept_trigger(
        TriggerMode.TTL,
        readiness=_readiness(),
        timestamp=10.1,
        captured_epoch=executor.state.arm_epoch,
        sequence=1,
    )

    assert result.events[-1].event == "ttl_pulse_ignored"
    assert "尚未确认布防" in result.events[-1].message
    assert executor.state.status == ProtocolExecutionStatus.WAITING_TRIGGER


def test_close_failed_blocks_start_reset_and_rearm_until_stop_recovers() -> None:
    outcomes = [False, True]

    def writer(channel: int, open_state: bool) -> tuple[bool, str]:
        if open_state:
            return True, "ok"
        ok = outcomes.pop(0)
        return ok, "ok" if ok else "关闭失败"

    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=writer,
    )
    document = _document()
    _start_manual(executor, document)
    executor.process_breath_samples([-0.6], safety_state="SAFE", timestamp_start=10.1)
    executor.handle_safety_update("LOW_FLOW", timestamp=10.2)
    before = (executor.state.document, executor.state.current_mode, executor.state.arm_epoch)

    started = executor.start(readiness=_readiness(), timestamp=10.3)
    reset = executor.reset(_document(), timestamp=10.4)
    rearm = executor.rearm_current(readiness=_readiness(), timestamp=10.5)

    assert all(result.events[-1].result == "rejected" for result in (started, reset, rearm))
    assert executor.state.active_valve == 3
    assert before == (executor.state.document, executor.state.current_mode, executor.state.arm_epoch)

    stopped = executor.stop(safety_state="LOW_FLOW", timestamp=10.6)
    assert stopped.state.status == ProtocolExecutionStatus.STOPPED
    assert stopped.state.active_valve is None


def test_stopped_restart_returns_to_trial_zero_declared_mode() -> None:
    executor = _executor()
    executor.reset(_document())
    executor.set_trigger_mode(TriggerMode.TTL, readiness=_readiness(), timestamp=9.0)
    executor.start(readiness=_readiness(), timestamp=10.0)
    executor.stop(safety_state="SAFE", timestamp=10.1)

    restarted = executor.start(readiness=_readiness(), timestamp=10.2)

    assert restarted.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
    assert restarted.state.trial_index == 0
    assert restarted.state.mode_override is None
    assert restarted.state.current_mode == TriggerMode.MANUAL


def test_stopped_restart_checks_first_trial_mode_not_stopped_trial_mode() -> None:
    executor = _executor()
    executor.start(_document(), readiness=_readiness(), timestamp=10.0)
    executor.skip_current(safety_state="SAFE", readiness=_readiness(), timestamp=10.1)
    assert executor.state.current_mode == TriggerMode.TTL
    executor.stop(safety_state="SAFE", timestamp=10.2)

    restarted = executor.start(readiness=_readiness(ttl_input_ready=False), timestamp=10.3)

    assert restarted.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
    assert restarted.state.trial_index == 0
    assert restarted.state.current_mode == TriggerMode.MANUAL


def test_stopped_restart_rejection_for_first_ttl_trial_is_atomic() -> None:
    document = ProtocolDocument(
        source_path=Path("ttl-first.csv"),
        source_name="ttl-first.csv",
        trials=[
            ProtocolTrial("ttl-first", 0, 100, 1, TriggerMode.TTL),
            ProtocolTrial("manual-second", 100, 100, 2, TriggerMode.MANUAL),
        ],
    )
    executor = _executor()
    executor.start(document, readiness=_readiness(), timestamp=10.0)
    executor.skip_current(safety_state="SAFE", readiness=_readiness(), timestamp=10.1)
    executor.stop(safety_state="SAFE", timestamp=10.2)
    before = (
        executor.state.status,
        executor.state.trial_index,
        executor.state.current_mode,
        executor.state.arm_epoch,
    )

    rejected = executor.start(readiness=_readiness(ttl_input_ready=False), timestamp=10.3)

    assert rejected.events[-1].result == "rejected"
    assert "AI6" in rejected.events[-1].message
    assert before == (
        executor.state.status,
        executor.state.trial_index,
        executor.state.current_mode,
        executor.state.arm_epoch,
    )


def test_exhale_transition_opens_current_valve_and_tick_closes_then_advances() -> None:
    actions: list[tuple[int, bool]] = []
    now = {"value": 10.0}

    def clock() -> float:
        return now["value"]

    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=lambda channel, open_state: (actions.append((channel, open_state)) or True, "ok"),
        config=ProtocolExecutionConfig(breath_gate_timeout_ms=5000),
        clock=clock,
    )
    _start_manual(executor)

    trigger_result = executor.process_breath_samples(
        [-0.6],
        safety_state="SAFE",
        timestamp_start=10.1,
        dt=0.01,
    )

    assert trigger_result.state.status == ProtocolExecutionStatus.TRIGGERED
    assert actions == [(3, True)]
    assert trigger_result.events[-1].event == "exhale_trigger"
    assert trigger_result.events[-1].sample_value == -0.6
    assert trigger_result.events[-1].exhale_threshold == -0.5

    now["value"] = 10.101
    early = executor.tick(safety_state="SAFE", timestamp=now["value"])
    assert early.events == []
    assert actions == [(3, True)]

    now["value"] = 10.2
    closed = executor.tick(safety_state="SAFE", timestamp=now["value"])

    assert actions == [(3, True), (3, False)]
    assert closed.events[0].event == "stimulus_end"
    assert closed.state.status == ProtocolExecutionStatus.WAITING_TRIGGER
    assert closed.state.current_trial.trial_id == "trial-2"
    assert closed.state.current_mode == TriggerMode.TTL


def test_deferred_open_event_preserves_trigger_sample_monotonic_identity() -> None:
    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=lambda _channel, _open_state: (True, "ok"),
        config=ProtocolExecutionConfig(breath_gate_timeout_ms=5000),
        clock=lambda: 10.0,
        deferred_actuation=True,
    )
    _start_manual(executor)
    batch = BreathSampleBatch.from_frames(
        (
            AnalogInputFrame(
                timestamp=10.1,
                ai0=-0.6,
                ai6=0.0,
                monotonic_ns=987_654_321,
                ai_epoch=4,
                sample_sequence=12,
            ),
        )
    )

    result = executor.process_breath_samples(
        batch,
        safety_state="SAFE",
    )

    event = next(item for item in result.events if item.event == "open_requested")
    assert event.monotonic_ns == 987_654_321
    assert result.action_requests[0].expected_ns == 987_654_321


def test_wait_timeout_skips_without_new_breath_samples() -> None:
    now = {"value": 10.0}
    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=lambda channel, open_state: (True, "ok"),
        config=ProtocolExecutionConfig(
            breath_gate_timeout_ms=500,
            breath_gate_timeout_action="skip",
            breath_gate_max_retries=1,
        ),
        clock=lambda: now["value"],
    )
    _start_manual(executor)

    now["value"] = 10.6
    result = executor.tick(safety_state="SAFE", timestamp=now["value"])

    assert [event.event for event in result.events] == ["timeout", "skip", "trigger_wait_start"]
    assert result.state.trial_index == 1
    assert result.state.status == ProtocolExecutionStatus.WAITING_TRIGGER


def test_retry_timeout_is_bounded_by_max_retries() -> None:
    now = {"value": 10.0}
    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=lambda channel, open_state: (True, "ok"),
        config=ProtocolExecutionConfig(
            breath_gate_timeout_ms=500,
            breath_gate_timeout_action="retry",
            breath_gate_max_retries=1,
        ),
        clock=lambda: now["value"],
    )
    _start_manual(executor)

    now["value"] = 10.6
    first = executor.tick(safety_state="SAFE", timestamp=now["value"])
    assert [event.event for event in first.events] == ["timeout", "retry"]
    assert first.state.retry_count == 1
    assert first.state.trial_index == 0

    now["value"] = 11.2
    second = executor.tick(safety_state="SAFE", timestamp=now["value"])
    assert [event.event for event in second.events] == ["timeout", "skip", "trigger_wait_start"]
    assert second.state.trial_index == 1
    assert "已超过最大重试次数" in second.events[1].message


def test_non_safe_state_blocks_and_closes_open_valve() -> None:
    actions: list[tuple[int, bool]] = []
    executor = _executor(actions=actions)
    _start_manual(executor)
    executor.process_breath_samples([-0.6], safety_state="SAFE", timestamp_start=10.1, dt=0.01)

    result = executor.handle_safety_update("LOW_FLOW", timestamp=10.2)

    assert result.state.status == ProtocolExecutionStatus.BLOCKED
    assert actions == [(3, True), (3, False)]
    assert result.events[-1].event == "safety_block"
    assert result.events[-1].safety_state == "LOW_FLOW"
    assert result.events[-1].result == "blocked"
    assert "已关闭活动阀门" in result.events[-1].message


def test_non_safe_close_failure_keeps_active_valve_for_recovery() -> None:
    actions: list[tuple[int, bool]] = []

    def valve_writer(channel: int, open_state: bool) -> tuple[bool, str]:
        actions.append((channel, open_state))
        if not open_state:
            return False, "数字输出失败"
        return True, "ok"

    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=valve_writer,
        clock=lambda: 10.0,
    )
    _start_manual(executor)
    executor.process_breath_samples([-0.6], safety_state="SAFE", timestamp_start=10.1, dt=0.01)

    result = executor.handle_safety_update("DATA_STALE", timestamp=10.2)

    assert result.state.status == ProtocolExecutionStatus.BLOCKED
    assert result.state.active_valve == 3
    assert actions == [(3, True), (3, False)]
    assert result.events[-1].event == "safety_block"
    assert result.events[-1].result == "close_failed"
    assert "关闭活动阀门失败" in result.events[-1].message


def test_blocked_state_with_active_valve_can_retry_safe_close() -> None:
    outcomes = [False, True]
    actions: list[tuple[int, bool]] = []

    def valve_writer(channel: int, open_state: bool) -> tuple[bool, str]:
        actions.append((channel, open_state))
        if not open_state:
            ok = outcomes.pop(0)
            return ok, "ok" if ok else "第一次关闭失败"
        return True, "ok"

    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=valve_writer,
        clock=lambda: 10.0,
    )
    _start_manual(executor)
    executor.process_breath_samples([-0.6], safety_state="SAFE", timestamp_start=10.1, dt=0.01)
    first = executor.handle_safety_update("LOW_FLOW", timestamp=10.2)
    assert first.state.active_valve == 3

    retry = executor.handle_safety_update("LOW_FLOW", timestamp=10.3)

    assert retry.state.status == ProtocolExecutionStatus.BLOCKED
    assert retry.state.active_valve is None
    assert actions == [(3, True), (3, False), (3, False)]
    assert retry.events[-1].event == "safety_block"
    assert retry.events[-1].result == "blocked"
    assert "已关闭活动阀门" in retry.events[-1].message


def test_blocked_state_without_active_valve_does_not_repeat_safety_block() -> None:
    executor = _executor()
    executor.start(_document(), readiness=_readiness(), timestamp=10.0)
    blocked = executor.handle_safety_update("LOW_FLOW", timestamp=10.0)
    assert blocked.state.status == ProtocolExecutionStatus.BLOCKED
    assert blocked.state.active_valve is None
    assert blocked.events[-1].event == "safety_block"

    repeated = executor.handle_safety_update("LOW_FLOW", timestamp=10.1)

    assert repeated.events == []
    assert repeated.state.status == ProtocolExecutionStatus.BLOCKED
    assert repeated.state.active_valve is None


def test_stop_after_close_failure_retries_active_valve() -> None:
    outcomes = [False, True]
    actions: list[tuple[int, bool]] = []

    def valve_writer(channel: int, open_state: bool) -> tuple[bool, str]:
        actions.append((channel, open_state))
        if not open_state:
            ok = outcomes.pop(0)
            return ok, "ok" if ok else "关闭失败"
        return True, "ok"

    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=valve_writer,
        clock=lambda: 10.0,
    )
    _start_manual(executor)
    executor.process_breath_samples([-0.6], safety_state="SAFE", timestamp_start=10.1, dt=0.01)
    failed_stop = executor.stop(safety_state="LOW_FLOW", timestamp=10.2)
    assert failed_stop.state.status == ProtocolExecutionStatus.BLOCKED
    assert failed_stop.state.active_valve == 3
    assert failed_stop.events[-1].result == "close_failed"

    recovered = executor.stop(safety_state="LOW_FLOW", timestamp=10.3)

    assert recovered.state.status == ProtocolExecutionStatus.STOPPED
    assert recovered.state.active_valve is None
    assert actions == [(3, True), (3, False), (3, False)]


def test_open_or_close_failure_blocks_successful_trial_progression() -> None:
    def failing_open(channel: int, open_state: bool) -> tuple[bool, str]:
        return False, "MFC 流量设定尚未建立，已阻断阀门打开"

    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=failing_open,
        clock=lambda: 10.0,
    )
    _start_manual(executor)

    result = executor.process_breath_samples(
        [-0.6],
        safety_state="SAFE",
        timestamp_start=10.1,
        dt=0.01,
    )

    assert result.state.status == ProtocolExecutionStatus.BLOCKED
    assert result.state.trial_index == 0
    assert result.events[-1].event == "blocked"
    assert "MFC" in result.events[-1].message


def test_close_failure_does_not_advance_and_allows_stop_recovery() -> None:
    outcomes = [False, True]
    actions: list[tuple[int, bool]] = []
    now = {"value": 10.0}

    def valve_writer(channel: int, open_state: bool) -> tuple[bool, str]:
        actions.append((channel, open_state))
        if not open_state:
            ok = outcomes.pop(0)
            return ok, "ok" if ok else "写入关闭失败"
        return True, "ok"

    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=valve_writer,
        clock=lambda: now["value"],
    )
    _start_manual(executor)
    executor.process_breath_samples([-0.6], safety_state="SAFE", timestamp_start=10.1, dt=0.01)

    now["value"] = 10.2
    failed_close = executor.tick(safety_state="SAFE", timestamp=now["value"])

    assert failed_close.state.status == ProtocolExecutionStatus.BLOCKED
    assert failed_close.state.trial_index == 0
    assert failed_close.state.active_valve == 3
    assert failed_close.events[-1].result == "close_failed"

    recovered = executor.stop(safety_state="SAFE", timestamp=10.3)

    assert recovered.state.status == ProtocolExecutionStatus.STOPPED
    assert recovered.state.active_valve is None
    assert actions == [(3, True), (3, False), (3, False)]


def test_non_safe_skip_current_blocks_without_advancing() -> None:
    executor = _executor()
    executor.start(_document(), safety_state="SAFE", timestamp=10.0)

    result = executor.skip_current(safety_state="LOW_FLOW", timestamp=10.2)

    assert result.state.status == ProtocolExecutionStatus.BLOCKED
    assert result.state.trial_index == 0
    assert result.state.current_trial.trial_id == "trial-1"
    assert result.events[-1].event == "safety_block"
    assert "不能推进" in result.events[-1].message


def test_skip_current_rejects_triggered_trial_without_losing_active_valve() -> None:
    actions: list[tuple[int, bool]] = []
    executor = _executor(actions=actions)
    _start_manual(executor)
    executor.process_breath_samples([-0.6], safety_state="SAFE", timestamp_start=10.1)
    before = (
        executor.state.status,
        executor.state.trial_index,
        executor.state.active_valve,
        executor.state.arm_epoch,
    )

    result = executor.skip_current(safety_state="SAFE", timestamp=10.2)

    assert result.events[-1].event == "skip_rejected"
    assert result.events[-1].result == "rejected"
    assert before == (
        executor.state.status,
        executor.state.trial_index,
        executor.state.active_valve,
        executor.state.arm_epoch,
    )
    assert actions == [(3, True)]


def test_skip_current_safety_blocks_triggered_trial_on_fresh_readiness_loss() -> None:
    actions: list[tuple[int, bool]] = []
    executor = _executor(actions=actions)
    _start_manual(executor)
    executor.process_breath_samples(
        [-0.6],
        safety_state="SAFE",
        readiness=_readiness(),
        timestamp_start=10.1,
    )
    old_epoch = executor.state.arm_epoch

    result = executor.skip_current(
        safety_state="SAFE",
        readiness=_readiness(connected=False),
        timestamp=10.2,
    )

    assert result.state.status == ProtocolExecutionStatus.BLOCKED
    assert result.state.trial_index == 0
    assert result.state.active_valve is None
    assert result.state.arm_epoch > old_epoch
    assert result.events[-1].event == "blocked"
    assert "连接" in result.events[-1].message
    assert actions == [(3, True), (3, False)]


def test_skip_current_rejects_blocked_trial_without_discarding_close_failure() -> None:
    executor = _executor()
    executor.reset(_document())
    executor.state.status = ProtocolExecutionStatus.BLOCKED
    executor.state.active_valve = 3
    before = (executor.state.trial_index, executor.state.active_valve, executor.state.arm_epoch)

    result = executor.skip_current(safety_state="SAFE", timestamp=10.0)

    assert result.events[-1].event == "skip_rejected"
    assert result.events[-1].result == "rejected"
    assert executor.state.status == ProtocolExecutionStatus.BLOCKED
    assert before == (executor.state.trial_index, executor.state.active_valve, executor.state.arm_epoch)


def test_enter_waiting_requires_safe_state_after_advance() -> None:
    executor = _executor()
    executor.reset(_document())
    executor.state.trial_index = 1

    result = executor.skip_current(safety_state="DATA_STALE", timestamp=10.0)

    assert result.state.status == ProtocolExecutionStatus.BLOCKED
    assert result.state.trial_index == 1
    assert result.events[-1].event == "safety_block"


@pytest.mark.parametrize(
    "readiness",
    [
        _readiness(connected=False),
        _readiness(hardware_ready=False),
        _readiness(flow_setpoints_ready=False),
        _readiness(ttl_input_ready=False),
    ],
)
def test_skip_never_arms_next_ttl_trial_when_required_readiness_is_missing(
    readiness: ProtocolExecutionReadiness,
) -> None:
    executor = _executor()
    executor.start(_document(), readiness=_readiness(), timestamp=10.0)

    result = executor.skip_current(
        safety_state=readiness.safety_state,
        readiness=readiness,
        timestamp=10.1,
    )

    assert result.state.status == ProtocolExecutionStatus.BLOCKED
    assert result.state.ttl_armed is False
    assert result.state.waiting_trigger_started_at is None
    assert result.events[-1].result == "blocked"


def test_finished_last_trial_marks_completed() -> None:
    now = {"value": 10.0}
    document = ProtocolDocument(
        source_path=Path("single.csv"),
        source_name="single.csv",
        trials=[
            ProtocolTrial(
                trial_id="only",
                timing_ms=0,
                duration_ms=10,
                valve=1,
                trigger=TriggerMode.MANUAL,
            )
        ],
    )
    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=lambda channel, open_state: (True, "ok"),
        config=ProtocolExecutionConfig(breath_gate_timeout_ms=5000),
        clock=lambda: now["value"],
    )
    _start_manual(executor, document)
    executor.process_breath_samples([-0.6], safety_state="SAFE", timestamp_start=10.1, dt=0.01)

    now["value"] = 10.2
    result = executor.tick(safety_state="SAFE", timestamp=now["value"])

    assert result.state.status == ProtocolExecutionStatus.COMPLETED
    assert result.events[-1].event == "completed"
    assert result.state.current_trial is None


def test_skipping_last_ttl_trial_completes_with_epoch_invalidated_and_disarmed() -> None:
    document = ProtocolDocument(
        source_path=Path("single-ttl.csv"),
        source_name="single-ttl.csv",
        trials=[
            ProtocolTrial(
                trial_id="only-ttl",
                timing_ms=0,
                duration_ms=10,
                valve=7,
                trigger=TriggerMode.TTL,
            )
        ],
    )
    executor = _executor()
    executor.start(document, readiness=_readiness(), timestamp=10.0)
    armed_epoch = executor.state.arm_epoch

    result = executor.skip_current(
        safety_state="SAFE",
        readiness=_readiness(),
        timestamp=10.1,
    )

    assert result.state.status == ProtocolExecutionStatus.COMPLETED
    assert result.state.ttl_armed is False
    assert result.state.waiting_trigger_started_at is None
    assert result.state.arm_epoch > armed_epoch


def test_executor_uses_existing_gating_service_for_exhale_detection() -> None:
    executor = _executor()
    executor.start(_document(), safety_state="SAFE", timestamp=10.0)

    result = executor.process_breath_samples(
        [0.0, -0.6],
        safety_state="SAFE",
        timestamp_start=10.0,
        dt=0.01,
    )

    assert result.transitions[-1].state == GatingState.EXHALE
    assert executor.gating_service.current_state == GatingState.EXHALE


def test_snapshot_disables_start_and_advance_when_not_safe() -> None:
    executor = _executor()
    executor.reset(_document())

    ready_snapshot = executor.snapshot(safety_state="LOW_FLOW", timestamp=10.0)

    assert ready_snapshot.can_start is False
    assert ready_snapshot.can_advance is False

    executor.start(_document(), safety_state="SAFE", timestamp=10.0)
    waiting_snapshot = executor.snapshot(safety_state="DATA_STALE", timestamp=10.1)

    assert waiting_snapshot.can_start is False
    assert waiting_snapshot.can_advance is False
    assert waiting_snapshot.can_stop is True
