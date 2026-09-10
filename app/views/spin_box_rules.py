from __future__ import annotations

import math
from decimal import Decimal
from typing import Protocol

from PySide6.QtCore import QLocale, Qt, Signal
from PySide6.QtGui import QFocusEvent, QKeyEvent, QValidator
from qfluentwidgets import DoubleSpinBox

FLOW_STEP_ML_MIN = 100.0
DURATION_STEP_S = 5
PRODUCT_NUMERIC_DECIMALS = 323
SNAP_ABS_TOLERANCE = 1e-9


class _SingleStepSpinBox(Protocol):
    def setSingleStep(self, value: int | float) -> None: ...  # noqa: N802


def apply_user_flow_step(spin_box: _SingleStepSpinBox) -> None:
    """应用产品气流步进，但不量化控件当前值。"""

    spin_box.setSingleStep(FLOW_STEP_ML_MIN)


def apply_user_seconds_step(spin_box: _SingleStepSpinBox) -> None:
    """应用产品秒级时间步进，但不量化控件当前值。"""

    spin_box.setSingleStep(DURATION_STEP_S)


def directional_snap_value(
    value: float,
    steps: int,
    *,
    interval: float,
    minimum: float,
    maximum: float,
    origin: float = 0.0,
    tolerance: float = SNAP_ABS_TOLERANCE,
) -> float:
    """按方向移动到严格相邻档位，并将结果限制在领域边界内。"""

    value = float(value)
    interval = float(interval)
    minimum = float(minimum)
    maximum = float(maximum)
    origin = float(origin)
    tolerance = float(tolerance)
    if not all(
        math.isfinite(item)
        for item in (value, interval, minimum, maximum, origin, tolerance)
    ):
        raise ValueError("方向吸附参数必须是有限数值。")
    if interval <= 0:
        raise ValueError("方向吸附间隔必须大于 0。")
    if tolerance < 0:
        raise ValueError("方向吸附 tolerance 不得为负数。")
    if minimum > maximum:
        raise ValueError("方向吸附最小值不得大于最大值。")
    if type(steps) is not int:
        raise TypeError("方向吸附步数必须是整数。")
    if steps == 0:
        return min(max(value, minimum), maximum)

    position = (value - origin) / interval
    nearest_index = round(position)
    nearest_value = origin + nearest_index * interval
    on_grid = math.isclose(
        value,
        nearest_value,
        rel_tol=0.0,
        abs_tol=tolerance,
    )
    if steps > 0:
        first_index = nearest_index + 1 if on_grid else math.ceil(position)
        target_index = first_index + steps - 1
    else:
        first_index = nearest_index - 1 if on_grid else math.floor(position)
        target_index = first_index + steps + 1

    target = origin + target_index * interval
    return min(max(target, minimum), maximum)


def format_product_number(
    value: float,
    *,
    decimals: int,
    locale: QLocale | None = None,
) -> str:
    """使用定点精度保留输入能力，同时隐藏无意义的末尾零。"""

    value = float(value)
    if not math.isfinite(value):
        raise ValueError("产品数值必须是有限数值。")
    if type(decimals) is not int or decimals < 0:
        raise ValueError("显示精度必须是非负整数。")
    text = format(Decimal(repr(value)), "f")
    fractional_digits = len(text.partition(".")[2])
    if fractional_digits > decimals:
        text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    text = "0" if text in {"-0", ""} else text
    decimal_point = (locale or QLocale.c()).decimalPoint()
    return text.replace(".", decimal_point)


class ProductNumericSpinBox(DoubleSpinBox):
    """产品共享数值控件：方向吸附步进，直接输入不量化。"""

    outOfRangeCommitAttempted = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._step_origin = 0.0
        self.setDecimals(PRODUCT_NUMERIC_DECIMALS)
        self.setKeyboardTracking(False)
        self.setWrapping(False)

    def setStepOrigin(self, origin: float) -> None:  # noqa: N802 - Qt 风格 API
        origin = float(origin)
        if not math.isfinite(origin):
            raise ValueError("步进原点必须是有限数值。")
        self._step_origin = origin

    def stepBy(self, steps: int) -> None:  # noqa: N802 - Qt override
        self.interpretText()
        self.setValue(
            directional_snap_value(
                self.value(),
                steps,
                interval=self.singleStep(),
                minimum=self.minimum(),
                maximum=self.maximum(),
                origin=self._step_origin,
            )
        )

    def textFromValue(self, value: float) -> str:  # noqa: N802 - Qt override
        return format_product_number(
            value,
            decimals=self.decimals(),
            locale=self.locale(),
        )

    def validate(self, text: str, pos: int):  # noqa: ANN201 - Qt override
        state, validated_text, validated_pos = super().validate(text, pos)
        value = self._parse_numeric_text(text)
        if (
            value is not None
            and value >= 0
            and (value < self.minimum() or value > self.maximum())
        ):
            return QValidator.State.Intermediate, text, pos
        return state, validated_text, validated_pos

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            if self._reject_out_of_range_commit():
                event.accept()
                return
        super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802 - Qt override
        self._reject_out_of_range_commit()
        super().focusOutEvent(event)

    def _parse_numeric_text(self, text: str) -> float | None:
        value_text = str(text)
        prefix = self.prefix()
        suffix = self.suffix()
        if prefix and value_text.startswith(prefix):
            value_text = value_text[len(prefix) :]
        if suffix and value_text.endswith(suffix):
            value_text = value_text[: -len(suffix)]
        value, accepted = self.locale().toDouble(value_text.strip())
        if not accepted or not math.isfinite(value):
            return None
        return float(value)

    def _reject_out_of_range_commit(self) -> bool:
        attempted = self._parse_numeric_text(self.lineEdit().text())
        if attempted is None or self.minimum() <= attempted <= self.maximum():
            return False
        if attempted < 0:
            return False
        self.outOfRangeCommitAttempted.emit(attempted)
        self.lineEdit().setText(
            f"{self.prefix()}{self.textFromValue(self.value())}{self.suffix()}"
        )
        self.lineEdit().selectAll()
        return True
