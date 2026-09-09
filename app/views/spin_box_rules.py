from typing import Protocol

FLOW_STEP_ML_MIN = 100.0
DURATION_STEP_S = 5


class _SingleStepSpinBox(Protocol):
    def setSingleStep(self, value: int | float) -> None: ...  # noqa: N802


def apply_user_flow_step(spin_box: _SingleStepSpinBox) -> None:
    """应用产品气流步进，但不量化控件当前值。"""

    spin_box.setSingleStep(FLOW_STEP_ML_MIN)


def apply_user_seconds_step(spin_box: _SingleStepSpinBox) -> None:
    """应用产品秒级时间步进，但不量化控件当前值。"""

    spin_box.setSingleStep(DURATION_STEP_S)
