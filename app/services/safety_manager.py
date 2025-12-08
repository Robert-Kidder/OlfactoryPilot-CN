class SafetyManager:
    """占位安全管理器，后续将落实硬件联锁逻辑。"""

    def __init__(self, low_flow_threshold: float = 0.2, recovery_margin: float = 0.05) -> None:
        self.low_flow_threshold = low_flow_threshold
        self.recovery_margin = recovery_margin

    def is_safe(self, airflow: float) -> bool:
        """Binary check without hysteresis (legacy)."""
        return airflow >= self.low_flow_threshold

    def evaluate(self, airflow: float, previous_state: str = "SAFE") -> str:
        """Apply hysteresis: recover only after airflow is comfortably above the threshold."""
        if airflow < self.low_flow_threshold:
            return "LOW_FLOW"
        if airflow >= self.low_flow_threshold + self.recovery_margin:
            return "SAFE"
        return previous_state
