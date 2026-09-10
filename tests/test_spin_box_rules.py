from __future__ import annotations

import pytest
from PySide6.QtCore import QLocale, Qt
from PySide6.QtGui import QValidator
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit

from app.views.spin_box_rules import (
    PRODUCT_NUMERIC_DECIMALS,
    ProductNumericSpinBox,
    directional_snap_value,
    format_product_number,
)


@pytest.mark.parametrize(
    ("value", "steps", "interval", "expected"),
    (
        (10, 1, 5, 15),
        (10, -1, 5, 5),
        (10.5, 1, 5, 15),
        (10.5, -1, 5, 10),
        (12.9, 1, 5, 15),
        (12.9, -1, 5, 10),
        (5.1, 1, 5, 10),
        (5.1, -1, 5, 5),
        (9.9, 1, 5, 10),
        (9.9, -1, 5, 5),
        (10.5, 2, 5, 20),
        (10.5, -2, 5, 5),
        (1200, 1, 100, 1300),
        (1200, -1, 100, 1100),
        (1225, 1, 100, 1300),
        (1225, -1, 100, 1200),
        (1250, 1, 100, 1300),
        (1250, -1, 100, 1200),
        (1201, 1, 100, 1300),
        (1201, -1, 100, 1200),
        (1299, 1, 100, 1300),
        (1299, -1, 100, 1200),
        (1225, 2, 100, 1400),
        (1225, -2, 100, 1100),
    ),
)
def test_directional_snap_uses_strict_neighbor_then_full_intervals(
    value, steps, interval, expected
) -> None:
    assert directional_snap_value(
        value,
        steps,
        interval=interval,
        minimum=0,
        maximum=5000,
    ) == expected


def test_directional_snap_treats_floating_noise_as_on_grid_and_clamps_bounds() -> None:
    assert directional_snap_value(
        10.0 + 1e-10,
        1,
        interval=5,
        minimum=0,
        maximum=20,
    ) == 15
    assert directional_snap_value(
        10.0 - 1e-10,
        -1,
        interval=5,
        minimum=0,
        maximum=20,
    ) == 5
    assert directional_snap_value(
        0,
        -1,
        interval=5,
        minimum=0,
        maximum=20,
    ) == 0
    assert directional_snap_value(
        0.1,
        -1,
        interval=5,
        minimum=0,
        maximum=20,
    ) == 0
    assert directional_snap_value(
        20,
        1,
        interval=5,
        minimum=0,
        maximum=20,
    ) == 20


@pytest.mark.parametrize(
    ("value", "decimals", "expected"),
    (
        (12.0, 6, "12"),
        (12.5, 6, "12.5"),
        (12.345600, 6, "12.3456"),
        (-0.0, 6, "0"),
    ),
)
def test_product_number_formatter_hides_only_insignificant_zeroes(
    value, decimals, expected
) -> None:
    assert format_product_number(value, decimals=decimals) == expected


def test_product_spin_box_keeps_direct_decimal_input_but_snaps_arrow_steps(qtbot) -> None:
    control = ProductNumericSpinBox()
    qtbot.addWidget(control)
    control.setRange(0, 5000)
    control.setSingleStep(100)
    control.setSuffix(" ml/min")
    control.setValue(1200)

    assert control.cleanText() == "1200"
    assert control.text() == "1200 ml/min"
    assert not control.wrapping()

    control.lineEdit().selectAll()
    QTest.keyClicks(control.lineEdit(), "1225.5")
    QTest.keyClick(control.lineEdit(), Qt.Key.Key_Return)
    assert control.value() == 1225.5
    assert control.cleanText() == "1225.5"

    control.stepUp()
    assert control.value() == 1300
    control.setValue(1225)
    control.stepDown()
    assert control.value() == 1200
    control.setValue(1225)
    control.stepBy(2)
    assert control.value() == 1400
    control.setValue(1225)
    control.stepBy(-2)
    assert control.value() == 1100

    control.setValue(1225)
    QTest.keyClick(control, Qt.Key.Key_Up)
    assert control.value() == 1300
    control.setValue(1225)
    QTest.keyClick(control, Qt.Key.Key_Down)
    assert control.value() == 1200


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("", QValidator.State.Intermediate),
        (".", QValidator.State.Intermediate),
        ("5", QValidator.State.Acceptable),
        ("5.5", QValidator.State.Acceptable),
        ("1500", QValidator.State.Acceptable),
        ("1500.5", QValidator.State.Acceptable),
        ("1500.55", QValidator.State.Invalid),
        ("2000.1", QValidator.State.Invalid),
        ("99999999", QValidator.State.Invalid),
        ("-1", QValidator.State.Invalid),
        ("NaN", QValidator.State.Invalid),
        ("Inf", QValidator.State.Invalid),
        ("-Infinity", QValidator.State.Invalid),
        ("1e-2", QValidator.State.Invalid),
        ("1e309", QValidator.State.Invalid),
    ),
)
def test_product_spin_box_validator_keeps_only_reasonable_intermediate_states(
    qtbot, text, expected
) -> None:
    control = ProductNumericSpinBox()
    qtbot.addWidget(control)
    control.setRange(0, 2000)

    assert PRODUCT_NUMERIC_DECIMALS == 1
    assert control.decimals() == 1
    assert control.validate(text, len(text))[0] is expected


def test_product_spin_box_rejects_the_character_that_would_exceed_range(
    qtbot,
) -> None:
    control = ProductNumericSpinBox()
    qtbot.addWidget(control)
    control.setRange(0, 2000)
    control.setSuffix(" ml/min")
    control.setValue(1500)

    control.lineEdit().selectAll()
    QTest.keyClicks(control.lineEdit(), "19000")

    assert control.cleanText() == "1900"
    QTest.keyClick(control.lineEdit(), Qt.Key.Key_Return)
    assert control.value() == 1900


def test_product_spin_box_rejects_a_second_fractional_digit_immediately(qtbot) -> None:
    control = ProductNumericSpinBox()
    qtbot.addWidget(control)
    control.setRange(0, 2000)

    control.lineEdit().selectAll()
    QTest.keyClicks(control.lineEdit(), "1500.5")
    QTest.keyClicks(control.lineEdit(), "5")

    assert control.cleanText() == "1500.5"
    QTest.keyClick(control.lineEdit(), Qt.Key.Key_Return)
    assert control.value() == 1500.5


@pytest.mark.parametrize(
    "pasted_text",
    (
        "99999999",
        "-1",
        "2000.1",
        "1500.55",
        "NaN",
        "Inf",
        "-Infinity",
        "1e-2",
        "1e309",
    ),
)
def test_product_spin_box_rejects_invalid_paste_without_replacing_legal_value(
    qtbot, qt_app, pasted_text
) -> None:
    control = ProductNumericSpinBox()
    qtbot.addWidget(control)
    control.setRange(0, 2000)
    control.setSuffix(" ml/min")
    control.setValue(1500)
    control.show()
    control.setFocus()
    control.lineEdit().selectAll()
    qt_app.clipboard().setText(pasted_text)

    QTest.keyClick(
        control.lineEdit(),
        Qt.Key.Key_V,
        Qt.KeyboardModifier.ControlModifier,
    )

    assert control.cleanText() == "1500"
    assert control.value() == 1500


@pytest.mark.parametrize(
    ("maximum", "suffix", "value"),
    ((2000, " ml/min", 1999.9), (60, " 秒", 59.9)),
)
def test_product_spin_box_size_hint_fits_legal_value_suffix_and_arrows(
    qtbot, maximum, suffix, value
) -> None:
    control = ProductNumericSpinBox()
    qtbot.addWidget(control)
    control.setRange(0, maximum)
    control.setSuffix(suffix)
    control.setValue(value)
    control.resize(control.sizeHint())
    control.show()

    text_width = control.lineEdit().fontMetrics().horizontalAdvance(control.text())
    margins = control.lineEdit().textMargins()
    available_width = control.lineEdit().contentsRect().width()
    assert text_width + margins.left() + margins.right() <= available_width
    assert control.width() <= 320


def test_time_spin_box_formats_fractional_values_and_keeps_suffix_numeric(qtbot) -> None:
    control = ProductNumericSpinBox()
    qtbot.addWidget(control)
    control.setRange(1, 60)
    control.setSingleStep(5)
    control.setSuffix(" 秒")

    control.setValue(5)
    assert control.text() == "5 秒"
    assert control.cleanText() == "5"
    assert control.value() == 5.0

    control.setValue(5.5)
    assert control.text() == "5.5 秒"
    assert control.cleanText() == "5.5"
    assert control.value() == 5.5


def test_product_spin_box_stops_at_range_edges_without_wrapping(qtbot) -> None:
    control = ProductNumericSpinBox()
    qtbot.addWidget(control)
    control.setRange(5, 15)
    control.setSingleStep(5)

    control.setValue(control.minimum())
    control.stepDown()
    assert control.value() == 5
    control.setValue(control.maximum())
    control.stepUp()
    assert control.value() == 15


def test_product_spin_box_uses_same_comma_locale_for_format_parse_and_step(
    qtbot,
) -> None:
    control = ProductNumericSpinBox()
    qtbot.addWidget(control)
    control.setLocale(QLocale(QLocale.Language.German))
    control.setRange(0, 100)
    control.setSingleStep(5)
    control.setValue(12.5)

    assert control.cleanText() == "12,5"
    control.lineEdit().selectAll()
    QTest.keyClicks(control.lineEdit(), "12,5")
    QTest.keyClick(control.lineEdit(), Qt.Key.Key_Return)
    assert control.value() == 12.5
    control.stepUp()
    assert control.value() == 15


def test_product_spin_box_preserves_one_decimal_value_and_range_endpoint(qtbot) -> None:
    control = ProductNumericSpinBox()
    qtbot.addWidget(control)
    endpoint = 1.2
    value = 1.1
    control.setRange(0, endpoint)
    control.setValue(value)

    assert control.maximum() == endpoint
    assert control.value() == value
    assert float(control.cleanText()) == value


def test_product_spin_box_rounds_business_range_inward_to_input_precision(
    qtbot,
) -> None:
    control = ProductNumericSpinBox()
    qtbot.addWidget(control)

    control.setRange(0.04, 1234.56)

    assert control.minimum() == 0.1
    assert control.maximum() == 1234.5
    assert control.validate("1234.6", 6)[0] is QValidator.State.Invalid


def test_product_spin_box_focus_commit_keeps_legal_off_grid_value(qtbot) -> None:
    control = ProductNumericSpinBox()
    other = QLineEdit()
    qtbot.addWidget(control)
    qtbot.addWidget(other)
    control.setRange(0, 5000)
    control.setSingleStep(100)
    control.show()
    other.show()
    control.setFocus()

    control.lineEdit().selectAll()
    QTest.keyClicks(control.lineEdit(), "1225.5")
    other.setFocus()
    QTest.qWait(1)

    assert control.value() == 1225.5
    assert control.cleanText() == "1225.5"
