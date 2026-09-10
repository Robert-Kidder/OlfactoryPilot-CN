from __future__ import annotations

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Protocol

from PySide6.QtCore import QLocale, QRect, QSize
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QSizePolicy, QStyle, QStyleOptionSpinBox
from qfluentwidgets import DoubleSpinBox

FLOW_STEP_ML_MIN = 100.0
DURATION_STEP_S = 5
PRODUCT_NUMERIC_DECIMALS = 1
PRODUCT_NUMERIC_TEXT_PADDING = 12
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

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._step_origin = 0.0
        self.setDecimals(PRODUCT_NUMERIC_DECIMALS)
        self.setKeyboardTracking(False)
        self.setWrapping(False)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def setStepOrigin(self, origin: float) -> None:  # noqa: N802 - Qt 风格 API
        origin = float(origin)
        if not math.isfinite(origin):
            raise ValueError("步进原点必须是有限数值。")
        self._step_origin = origin

    def setRange(self, minimum: float, maximum: float) -> None:  # noqa: N802
        safe_minimum = self._quantize_bound(minimum, rounding=ROUND_CEILING)
        safe_maximum = self._quantize_bound(maximum, rounding=ROUND_FLOOR)
        if safe_minimum > safe_maximum:
            raise ValueError("产品数值范围内没有可用一位小数表示的值。")
        super().setRange(safe_minimum, safe_maximum)

    def setMinimum(self, minimum: float) -> None:  # noqa: N802 - Qt override
        super().setMinimum(self._quantize_bound(minimum, rounding=ROUND_CEILING))

    def setMaximum(self, maximum: float) -> None:  # noqa: N802 - Qt override
        super().setMaximum(self._quantize_bound(maximum, rounding=ROUND_FLOOR))

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
        value_text = self._numeric_text(text)
        value, accepted = self.locale().toDouble(value_text.strip())
        if accepted and (
            not math.isfinite(value)
            or value < self.minimum()
            or value > self.maximum()
            or value != self._quantize_value(value)
        ):
            return QValidator.State.Invalid, text, pos
        return state, validated_text, validated_pos

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self._content_aware_size(super().sizeHint())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self._content_aware_size(super().minimumSizeHint())

    def _numeric_text(self, text: str) -> str:
        value_text = str(text)
        prefix = self.prefix()
        suffix = self.suffix()
        if prefix and value_text.startswith(prefix):
            value_text = value_text[len(prefix) :]
        if suffix and value_text.endswith(suffix):
            value_text = value_text[: -len(suffix)]
        return value_text

    def _content_aware_size(self, base: QSize) -> QSize:
        values = [self.minimum(), self.maximum()]
        quantum = 10.0 ** -self.decimals()
        if self.maximum() - self.minimum() >= quantum:
            values.extend((self.minimum() + quantum, self.maximum() - quantum))
        texts = (
            f"{self.prefix()}{self.textFromValue(value)}{self.suffix()}"
            for value in values
        )
        line_edit = self.lineEdit()
        text_width = max(line_edit.fontMetrics().horizontalAdvance(text) for text in texts)
        margins = line_edit.textMargins()
        option = QStyleOptionSpinBox()
        self.initStyleOption(option)
        probe_width = max(1000, base.width())
        option.rect = QRect(0, 0, probe_width, max(1, base.height()))
        edit_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_SpinBox,
            option,
            QStyle.SubControl.SC_SpinBoxEditField,
            self,
        )
        chrome_width = max(0, probe_width - edit_rect.width())
        required_width = (
            text_width
            + margins.left()
            + margins.right()
            + chrome_width
            + PRODUCT_NUMERIC_TEXT_PADDING
        )
        return QSize(max(base.width(), required_width), base.height())

    def _quantize_bound(self, value: float, *, rounding: str) -> float:
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("产品数值边界必须是有限数值。")
        quantum = Decimal(1).scaleb(-self.decimals())
        return float(Decimal(str(numeric)).quantize(quantum, rounding=rounding))

    def _quantize_value(self, value: float) -> float:
        quantum = Decimal(1).scaleb(-self.decimals())
        return float(Decimal(str(float(value))).quantize(quantum))
