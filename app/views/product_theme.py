from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QWidget


@dataclass(frozen=True, slots=True)
class ProductColors:
    page: str = "#101613"
    primary_surface: str = "#171E1B"
    secondary_surface: str = "#1D2521"
    border: str = "#343C38"
    amber: str = "#E2AD50"
    text_primary: str = "#F4F6F5"
    text_secondary: str = "#858F8B"
    success: str = "#77C99D"
    warning: str = "#E7C46A"
    error: str = "#FF918A"

    @property
    def surface(self) -> str:
        return self.primary_surface

    @property
    def secondary(self) -> str:
        return self.text_secondary

    @property
    def text(self) -> str:
        return self.text_primary


COLORS = ProductColors()


def apply_page_palette(widget: QWidget) -> None:
    palette = widget.palette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLORS.page))
    widget.setPalette(palette)
    widget.setAutoFillBackground(True)


def make_viewport_transparent(viewport: QWidget) -> None:
    viewport.setAutoFillBackground(False)
    viewport.setStyleSheet("background: transparent;")
