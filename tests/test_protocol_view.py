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
    assert view._ttl_trigger_button.isEnabled() is False


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
        )
    )

    assert "等待呼气" in view._execution_status_label.text()
    assert "trial-1" in view._execution_trial_label.text()
    assert "3" in view._execution_valve_label.text()
    assert "开始等待呼气" in view._execution_event_label.text()
    assert view._start_button.isEnabled() is False
    assert view._stop_button.isEnabled() is True
    assert view._next_trial_button.isEnabled() is True
