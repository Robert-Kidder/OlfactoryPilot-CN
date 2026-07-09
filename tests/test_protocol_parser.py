from __future__ import annotations

from pathlib import Path

import pytest

from app.controllers import MainController
from app.models import AppState, TriggerMode
from app.services import MockHAL, ProtocolParseError, SafetyManager, parse_protocol_file
from app.workers import HardwareWorker

FIXTURES = Path(__file__).parent / "fixtures" / "protocols"


def _valve_map(size: int = 10) -> dict[int, str]:
    return {channel: f"Dev1/P0.{channel}" for channel in range(1, size + 1)}


def _state() -> AppState:
    return AppState.from_config(
        {
            "language": "zh-CN",
            "window_title": "测试窗口",
            "log_level": "INFO",
            "hardware_variant": "10-channel",
            "safety_state": "SAFE",
            "valve_mapping": {"variants": {"10-channel": _valve_map(10)}},
        }
    )


def test_parse_valid_csv_preserves_order_and_metadata() -> None:
    document = parse_protocol_file(FIXTURES / "valid_protocol.csv", valve_map=_valve_map())

    assert document.source_name == "valid_protocol.csv"
    assert document.metadata == {"operator": "Jing", "session": "demo"}
    assert [trial.trial_id for trial in document.trials] == ["1", "2"]
    assert document.trials[0].timing_ms == 0
    assert document.trials[1].duration_ms == 200
    assert document.trials[1].valve == 10
    assert document.trials[0].trigger == TriggerMode.MANUAL
    assert document.trials[1].trigger == TriggerMode.TTL
    assert document.trials[0].metadata == {"odor": "rose"}


def test_parse_valid_txt_supports_aliases_comments_and_whitespace() -> None:
    document = parse_protocol_file(FIXTURES / "valid_protocol.txt", valve_map=_valve_map())

    assert document.metadata == {"operator": "Jing", "experiment": "txt demo"}
    assert [trial.trial_id for trial in document.trials] == ["3", "4"]
    assert [trial.timing_ms for trial in document.trials] == [0, 1000]
    assert [trial.valve for trial in document.trials] == [2, 3]
    assert document.trials[0].metadata["note"] == "lemon"


@pytest.mark.parametrize(
    ("fixture_name", "line_number", "field", "message_part"),
    [
        ("empty_protocol.csv", 1, "file", "文件为空"),
        ("missing_field.csv", 1, "timing_ms", "缺少必填字段"),
        ("invalid_number.csv", 2, "timing_ms", "必须是数字"),
        ("unknown_trigger.csv", 2, "trigger", "未知触发模式"),
        ("valve_out_of_range.csv", 2, "valve", "不在当前硬件"),
        ("no_trials.txt", 1, "trial", "未找到有效 trial"),
    ],
)
def test_parse_errors_include_line_field_and_chinese_action(
    fixture_name: str,
    line_number: int,
    field: str,
    message_part: str,
) -> None:
    with pytest.raises(ProtocolParseError) as exc_info:
        parse_protocol_file(FIXTURES / fixture_name, valve_map=_valve_map())

    error = exc_info.value
    assert error.line_number == line_number
    assert error.field == field
    assert message_part in error.message
    assert "请" in str(error)


def test_unsupported_extension_returns_chinese_error(tmp_path: Path) -> None:
    path = tmp_path / "protocol.xlsx"
    path.write_text("trial,timing_ms,duration_ms,valve,trigger\n", encoding="utf-8")

    with pytest.raises(ProtocolParseError) as exc_info:
        parse_protocol_file(path, valve_map=_valve_map())

    assert exc_info.value.field == "file_extension"
    assert "仅支持 .txt 和 .csv" in exc_info.value.message


def test_controller_load_failure_keeps_previous_protocol(qt_app) -> None:
    state = _state()
    worker = HardwareWorker(telemetry_hz=1, hal=MockHAL(), simulation=True)
    controller = MainController(
        state,
        worker,
        safety_manager=SafetyManager(low_flow_threshold=0.2),
    )

    assert controller.handle_protocol_file_selected(FIXTURES / "valid_protocol.csv") is True
    previous = state.loaded_protocol
    assert previous is not None

    assert controller.handle_protocol_file_selected(FIXTURES / "valve_out_of_range.csv") is False

    assert state.loaded_protocol is previous
    assert "解析失败" in state.status_message
    assert "valve" in state.status_message
