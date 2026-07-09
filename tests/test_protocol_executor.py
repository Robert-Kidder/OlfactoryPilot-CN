from __future__ import annotations

from pathlib import Path

from app.models import ProtocolDocument, ProtocolTrial, TriggerMode
from app.models.protocol_execution import ProtocolExecutionStatus
from app.services.gating_service import GatingService, GatingState
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


def test_start_without_protocol_stays_idle_and_prompts_user() -> None:
    executor = _executor()

    result = executor.start(None, safety_state="SAFE", timestamp=10.0)

    assert result.state.status == ProtocolExecutionStatus.IDLE
    assert result.events[-1].event == "invalid_protocol"
    assert "请先加载有效协议" in result.events[-1].message


def test_start_prepares_first_trial_without_opening_valve() -> None:
    actions: list[tuple[int, bool]] = []
    executor = _executor(actions=actions)

    result = executor.start(_document(), safety_state="SAFE", timestamp=10.0)

    assert result.state.status == ProtocolExecutionStatus.WAITING_EXHALE
    assert result.state.trial_index == 0
    assert result.state.current_trial.trial_id == "trial-1"
    assert result.events[-1].event == "wait_start"
    assert actions == []


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
    executor.start(_document(), safety_state="SAFE", timestamp=10.0)

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
    assert closed.state.status == ProtocolExecutionStatus.WAITING_EXHALE
    assert closed.state.current_trial.trial_id == "trial-2"


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
    executor.start(_document(), safety_state="SAFE", timestamp=10.0)

    now["value"] = 10.6
    result = executor.tick(safety_state="SAFE", timestamp=now["value"])

    assert [event.event for event in result.events] == ["timeout", "skip", "wait_start"]
    assert result.state.trial_index == 1
    assert result.state.status == ProtocolExecutionStatus.WAITING_EXHALE


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
    executor.start(_document(), safety_state="SAFE", timestamp=10.0)

    now["value"] = 10.6
    first = executor.tick(safety_state="SAFE", timestamp=now["value"])
    assert [event.event for event in first.events] == ["timeout", "retry"]
    assert first.state.retry_count == 1
    assert first.state.trial_index == 0

    now["value"] = 11.2
    second = executor.tick(safety_state="SAFE", timestamp=now["value"])
    assert [event.event for event in second.events] == ["timeout", "skip", "wait_start"]
    assert second.state.trial_index == 1
    assert "已超过最大重试次数" in second.events[1].message


def test_non_safe_state_blocks_and_closes_open_valve() -> None:
    actions: list[tuple[int, bool]] = []
    executor = _executor(actions=actions)
    executor.start(_document(), safety_state="SAFE", timestamp=10.0)
    executor.process_breath_samples([-0.6], safety_state="SAFE", timestamp_start=10.1, dt=0.01)

    result = executor.handle_safety_update("LOW_FLOW", timestamp=10.2)

    assert result.state.status == ProtocolExecutionStatus.BLOCKED
    assert actions == [(3, True), (3, False)]
    assert result.events[-1].event == "safety_block"
    assert result.events[-1].safety_state == "LOW_FLOW"


def test_open_or_close_failure_blocks_successful_trial_progression() -> None:
    def failing_open(channel: int, open_state: bool) -> tuple[bool, str]:
        return False, "MFC 流量设定尚未建立，已阻断阀门打开"

    executor = ProtocolExecutor(
        gating_service=GatingService(inhale_threshold=0.5, exhale_threshold=-0.5),
        valve_writer=failing_open,
        clock=lambda: 10.0,
    )
    executor.start(_document(), safety_state="SAFE", timestamp=10.0)

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
    executor.start(document, safety_state="SAFE", timestamp=10.0)
    executor.process_breath_samples([-0.6], safety_state="SAFE", timestamp_start=10.1, dt=0.01)

    now["value"] = 10.2
    result = executor.tick(safety_state="SAFE", timestamp=now["value"])

    assert result.state.status == ProtocolExecutionStatus.COMPLETED
    assert result.events[-1].event == "completed"
    assert result.state.current_trial is None


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
