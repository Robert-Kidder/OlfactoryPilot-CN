from __future__ import annotations

from pathlib import Path

from app.models import (
    ProtocolDocument,
    ProtocolExecutionSnapshot,
    ProtocolExecutionStatus,
    ProtocolTrial,
    TriggerMode,
)
from app.services import ProtocolParseError
from app.views.protocol_view import ProtocolView


def test_protocol_view_renders_summary_and_keeps_actions_disabled(qt_app, qtbot) -> None:
    view = ProtocolView()
    qtbot.addWidget(view)
    document = ProtocolDocument(
        source_path=Path("demo.csv"),
        source_name="demo.csv",
        metadata={"operator": "Jing"},
        trials=[
            ProtocolTrial(
                trial_id="1",
                timing_ms=0,
                duration_ms=100,
                valve=1,
                trigger=TriggerMode.MANUAL,
                metadata={},
            )
        ],
    )

    view.render_protocol(document)

    assert "demo.csv" in view._summary_label.text()
    assert "trial 数量：1" in view._summary_label.text()
    assert "manual=1" in view._trigger_label.text()
    assert "operator=Jing" in view._metadata_label.text()
    assert view._preview_table.rowCount() == 1
    assert view._start_button.isEnabled() is False
    assert view._manual_trigger_button.isEnabled() is False
    assert view._manual_mode_button.isEnabled() is False
    assert view._ttl_mode_button.isEnabled() is False
    assert not hasattr(view, "_ttl_trigger_button")


def test_protocol_view_renders_chinese_parse_error(qt_app, qtbot) -> None:
    view = ProtocolView()
    qtbot.addWidget(view)

    view.render_error(ProtocolParseError(2, "valve", "阀门通道 99 不在当前硬件允许范围。"))

    assert "解析失败" in view._error_label.text()
    assert "第 2 行" in view._error_label.text()
    assert "valve" in view._error_label.text()
    assert "请" in view._error_label.text()


def test_protocol_view_renders_execution_state_and_action_enablement(qt_app, qtbot) -> None:
    view = ProtocolView()
    qtbot.addWidget(view)

    view.render_execution_state(
        ProtocolExecutionSnapshot(
            status=ProtocolExecutionStatus.WAITING_EXHALE,
            status_text="等待呼气",
            has_protocol=True,
            can_start=False,
            can_stop=True,
            can_advance=True,
            trial_label="1/2",
            trial_id="trial-1",
            valve=3,
            trigger="manual",
            wait_elapsed_ms=1200,
            planned_duration_ms=100,
            recent_event="开始等待呼气",
            protocol_mode="manual",
            current_mode="manual",
            can_select_mode=True,
            can_select_manual_mode=True,
            can_select_ttl_mode=True,
            can_manual_trigger=False,
        )
    )

    assert "等待呼气" in view._execution_status_label.text()
    assert "trial-1" in view._execution_trial_label.text()
    assert "3" in view._execution_valve_label.text()
    assert "开始等待呼气" in view._execution_event_label.text()
    assert view._start_button.isEnabled() is False
    assert view._stop_button.isEnabled() is True
    assert view._next_trial_button.isEnabled() is True
    assert view._manual_mode_button.isChecked() is True


def test_protocol_view_emits_mutually_exclusive_mode_and_manual_intents(qt_app, qtbot) -> None:
    view = ProtocolView()
    qtbot.addWidget(view)
    modes: list[str] = []
    manual_requests: list[bool] = []
    view.trigger_mode_requested.connect(modes.append)
    view.manual_trigger_requested.connect(lambda: manual_requests.append(True))
    view.render_execution_state(
        ProtocolExecutionSnapshot(
            status=ProtocolExecutionStatus.WAITING_TRIGGER,
            status_text="等待触发",
            has_protocol=True,
            can_start=False,
            can_stop=True,
            can_advance=True,
            protocol_mode="manual",
            current_mode="manual",
            can_select_mode=True,
            can_select_manual_mode=True,
            can_select_ttl_mode=True,
            can_manual_trigger=True,
        )
    )

    view._ttl_mode_button.click()
    view._manual_trigger_button.click()

    assert modes == ["ttl"]
    assert manual_requests == [True]
    assert view._ttl_mode_button.isChecked() is True
    assert view._manual_mode_button.isChecked() is False


def test_ai6_not_ready_keeps_manual_actions_but_disables_ttl_mode(qt_app, qtbot) -> None:
    view = ProtocolView()
    qtbot.addWidget(view)

    view.render_execution_state(
        ProtocolExecutionSnapshot(
            status=ProtocolExecutionStatus.WAITING_TRIGGER,
            status_text="等待触发",
            has_protocol=True,
            can_start=False,
            can_stop=True,
            can_advance=True,
            protocol_mode="manual",
            current_mode="manual",
            can_select_mode=True,
            can_select_manual_mode=True,
            can_select_ttl_mode=False,
            can_manual_trigger=True,
            ttl_armed=False,
            readiness_reason="TTL 输入 AI6 尚未就绪",
        )
    )

    assert view._manual_mode_button.isEnabled() is True
    assert view._manual_trigger_button.isEnabled() is True
    assert view._ttl_mode_button.isEnabled() is False
    assert "协议模式：manual" in view._execution_trigger_label.text()
    assert "当前模式：manual" in view._execution_trigger_label.text()
    assert "AI6" in view._execution_arm_label.text()


def test_protocol_view_disables_start_and_next_when_not_safe(qt_app, qtbot) -> None:
    view = ProtocolView()
    qtbot.addWidget(view)

    view.render_execution_state(
        ProtocolExecutionSnapshot(
            status=ProtocolExecutionStatus.WAITING_EXHALE,
            status_text="等待呼气",
            has_protocol=True,
            can_start=False,
            can_stop=True,
            can_advance=False,
            trial_label="1/2",
            trial_id="trial-1",
            valve=3,
            trigger="manual",
            wait_elapsed_ms=1200,
            planned_duration_ms=100,
            recent_event="安全状态 LOW_FLOW，不能推进 trial。",
        )
    )

    assert view._start_button.isEnabled() is False
    assert view._next_trial_button.isEnabled() is False
    assert view._stop_button.isEnabled() is True


def test_protocol_view_renders_actuation_quality_remaining_and_pause_resume(qt_app, qtbot) -> None:
    view = ProtocolView()
    qtbot.addWidget(view)
    paused = []
    resumed = []
    view.pause_requested.connect(lambda: paused.append(True))
    view.resume_requested.connect(lambda: resumed.append(True))

    view.render_execution_state(
        ProtocolExecutionSnapshot(
            status=ProtocolExecutionStatus.TRIGGERED,
            status_text="已触发",
            has_protocol=True,
            can_start=False,
            can_stop=True,
            can_advance=False,
            trial_label="1/2",
            trial_id="trial-1",
            valve=3,
            next_odor="薄荷",
            last_jitter_ms=4.25,
            p95_open_ms=20.0,
            p95_close_ms=21.5,
            p95_combined_ms=19.0,
            sample_count_open=20,
            sample_count_close=20,
            sample_count_combined=40,
            remaining_ms=87.4,
            can_pause=True,
        )
    )
    assert "薄荷" in view._execution_trial_label.text()
    assert "4.25" in view._execution_quality_label.text()
    assert "临界" in view._execution_quality_label.text()
    assert "警告" in view._execution_quality_label.text()
    assert "87" in view._execution_wait_label.text()
    view._pause_button.click()
    assert paused == [True]

    view.render_execution_state(
        ProtocolExecutionSnapshot(
            status=ProtocolExecutionStatus.PAUSED,
            status_text="已暂停",
            has_protocol=True,
            can_start=False,
            can_stop=True,
            can_advance=False,
            can_resume=True,
        )
    )
    view._resume_button.click()
    assert resumed == [True]
