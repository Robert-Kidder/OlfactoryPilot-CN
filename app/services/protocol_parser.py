from __future__ import annotations

import csv
import math
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import NamedTuple

from app.models.protocol import ProtocolDocument, ProtocolTrial, TriggerMode


class ProtocolParseError(Exception):
    def __init__(self, line_number: int | None, field: str, message: str) -> None:
        self.line_number = line_number
        self.field = field
        self.message = message
        super().__init__(str(self))

    def __str__(self) -> str:
        line = f"第 {self.line_number} 行，" if self.line_number else ""
        return f"{line}字段 {self.field}：{self.message} 请修正后重新加载。"


class _TableLine(NamedTuple):
    line_number: int
    text: str


_CORE_FIELDS = ("trial_id", "timing_ms", "duration_ms", "valve", "trigger")
_FIELD_ALIASES = {
    "trial_id": {"trial", "trial_id", "index", "试次"},
    "timing_ms": {"timing", "timing_ms", "onset_ms", "time_ms"},
    "duration_ms": {"duration", "duration_ms", "stim_ms"},
    "valve": {"valve", "channel", "odor_valve", "气味通道"},
    "trigger": {"trigger", "trigger_mode", "mode"},
}
_SUPPORTED_SUFFIXES = {".csv", ".txt"}


def parse_protocol_file(
    path: str | Path,
    *,
    valve_map: Mapping[int, str] | Iterable[int],
) -> ProtocolDocument:
    source_path = Path(path)
    suffix = source_path.suffix.lower()
    if suffix not in _SUPPORTED_SUFFIXES:
        raise ProtocolParseError(None, "file_extension", "仅支持 .txt 和 .csv 协议文件。")

    try:
        raw_text = source_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ProtocolParseError(None, "file", f"无法读取协议文件：{exc}") from exc
    if not raw_text.strip():
        raise ProtocolParseError(1, "file", "文件为空，至少需要一行表头和一条 trial。")

    allowed_valves = _allowed_channels(valve_map)
    metadata, table_lines = _split_metadata_and_table(raw_text)
    if not table_lines:
        raise ProtocolParseError(1, "trial", "未找到有效 trial，请检查表头和数据行。")

    delimiter = _detect_delimiter(table_lines[0].text, suffix)
    header = _split_table_line(table_lines[0].text, delimiter)
    column_map = _normalize_header(header, table_lines[0].line_number)

    trials: list[ProtocolTrial] = []
    for table_line in table_lines[1:]:
        cells = _split_table_line(table_line.text, delimiter)
        if not any(cell.strip() for cell in cells):
            continue
        trials.append(_parse_trial(table_line, header, cells, column_map, allowed_valves))

    if not trials:
        raise ProtocolParseError(1, "trial", "未找到有效 trial，请检查表头下方是否有数据行。")

    return ProtocolDocument(
        source_path=source_path,
        source_name=source_path.name,
        metadata=metadata,
        trials=trials,
    )


def _allowed_channels(valve_map: Mapping[int, str] | Iterable[int]) -> set[int]:
    if isinstance(valve_map, Mapping):
        return {int(channel) for channel in valve_map}
    return {int(channel) for channel in valve_map}


def _split_metadata_and_table(raw_text: str) -> tuple[dict[str, str], list[_TableLine]]:
    metadata: dict[str, str] = {}
    table_lines: list[_TableLine] = []
    header_seen = False
    for line_number, original in enumerate(raw_text.splitlines(), start=1):
        text = original.strip()
        if not text:
            continue
        if text.startswith("#"):
            _merge_metadata_line(metadata, text[1:].strip())
            continue
        if not header_seen and _merge_metadata_line(metadata, text):
            continue
        header_seen = True
        table_lines.append(_TableLine(line_number=line_number, text=original))
    return metadata, table_lines


def _merge_metadata_line(metadata: dict[str, str], text: str) -> bool:
    match = re.match(r"^([^:=,\t;]+?)\s*[:=]\s*(.+)$", text)
    if not match:
        return False
    key = match.group(1).strip()
    value = match.group(2).strip()
    if key and value:
        metadata[key] = value
        return True
    return False


def _detect_delimiter(header_line: str, suffix: str) -> str | None:
    candidates = [",", ";", "\t"]
    if suffix == ".csv":
        try:
            dialect = csv.Sniffer().sniff(header_line, delimiters=",;\t")
            return dialect.delimiter
        except csv.Error:
            pass
    for delimiter in candidates:
        if delimiter in header_line:
            return delimiter
    return None


def _split_table_line(line: str, delimiter: str | None) -> list[str]:
    if delimiter is None:
        return [cell.strip() for cell in re.split(r"\s+", line.strip())]
    return [cell.strip() for cell in next(csv.reader([line], delimiter=delimiter))]


def _normalize_header(header: list[str], line_number: int) -> dict[str, int]:
    normalized = [_normalize_name(cell) for cell in header]
    column_map: dict[str, int] = {}
    for canonical, aliases in _FIELD_ALIASES.items():
        for index, name in enumerate(normalized):
            if name in aliases:
                column_map[canonical] = index
                break
        if canonical not in column_map:
            raise ProtocolParseError(line_number, canonical, f"缺少必填字段 {canonical}。")
    return column_map


def _normalize_name(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _parse_trial(
    table_line: _TableLine,
    header: list[str],
    cells: list[str],
    column_map: dict[str, int],
    allowed_valves: set[int],
) -> ProtocolTrial:
    row = {header[index].strip(): cells[index].strip() if index < len(cells) else "" for index in range(len(header))}
    trial_id = _get_cell(cells, column_map["trial_id"]).strip()
    if not trial_id:
        raise ProtocolParseError(table_line.line_number, "trial_id", "trial_id 不能为空。")

    timing_ms = _parse_number(_get_cell(cells, column_map["timing_ms"]), table_line.line_number, "timing_ms")
    duration_ms = _parse_number(
        _get_cell(cells, column_map["duration_ms"]),
        table_line.line_number,
        "duration_ms",
    )
    valve = _parse_valve(_get_cell(cells, column_map["valve"]), table_line.line_number, allowed_valves)
    trigger = _parse_trigger(_get_cell(cells, column_map["trigger"]), table_line.line_number)
    metadata = {
        key: value
        for key, value in row.items()
        if column_map.get("trial_id") != header.index(key)
        and column_map.get("timing_ms") != header.index(key)
        and column_map.get("duration_ms") != header.index(key)
        and column_map.get("valve") != header.index(key)
        and column_map.get("trigger") != header.index(key)
        and value
    }
    return ProtocolTrial(
        trial_id=trial_id,
        timing_ms=timing_ms,
        duration_ms=duration_ms,
        valve=valve,
        trigger=trigger,
        metadata=metadata,
        line_number=table_line.line_number,
    )


def _get_cell(cells: list[str], index: int) -> str:
    return cells[index] if index < len(cells) else ""


def _parse_number(value: str, line_number: int, field: str) -> int | float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ProtocolParseError(line_number, field, f"{field} 必须是数字，当前值为 {value!r}。") from exc
    if not math.isfinite(number):
        raise ProtocolParseError(line_number, field, f"{field} 必须是有限数字，当前值为 {value!r}。")
    if number.is_integer():
        return int(number)
    return number


def _parse_valve(value: str, line_number: int, allowed_valves: set[int]) -> int:
    number = _parse_number(value, line_number, "valve")
    if not isinstance(number, int):
        raise ProtocolParseError(line_number, "valve", f"valve 必须是整数通道，当前值为 {value!r}。")
    if number not in allowed_valves:
        allowed = f"{min(allowed_valves)}-{max(allowed_valves)}" if allowed_valves else "空映射"
        raise ProtocolParseError(line_number, "valve", f"阀门通道 {number} 不在当前硬件允许范围（{allowed}）。")
    return number


def _parse_trigger(value: str, line_number: int) -> TriggerMode:
    normalized = value.strip().lower()
    try:
        return TriggerMode(normalized)
    except ValueError as exc:
        allowed = "、".join(mode.value for mode in TriggerMode)
        raise ProtocolParseError(line_number, "trigger", f"未知触发模式 {value!r}，仅支持 {allowed}。") from exc
