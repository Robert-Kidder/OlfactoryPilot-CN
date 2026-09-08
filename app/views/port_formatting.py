from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontMetrics


def normalize_port_alias(value: object, external_port: int) -> str:
    alias = " ".join(str(value or "").split())
    number = format_port_number(external_port)
    if alias in {f"气口 {int(external_port)}", number}:
        return ""
    return alias


def format_port_number(external_port: int) -> str:
    return f"气口 {int(external_port):02d}"


@dataclass(frozen=True, slots=True)
class PortText:
    number: str
    alias: str
    elided_alias: str
    tooltip: str


def format_port_text(
    external_port: int,
    display_name: object,
    *,
    font: QFont,
    width: int,
) -> PortText:
    number = format_port_number(external_port)
    alias = normalize_port_alias(display_name, external_port)
    elided = (
        QFontMetrics(font).elidedText(
            alias,
            Qt.TextElideMode.ElideRight,
            max(20, int(width)),
        )
        if alias
        else ""
    )
    return PortText(
        number=number,
        alias=alias,
        elided_alias=elided,
        tooltip=alias if alias and elided != alias else "",
    )
