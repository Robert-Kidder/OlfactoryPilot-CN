from __future__ import annotations

from pathlib import Path

from app.models import ProtocolDocument, ProtocolTrial, TriggerMode
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
