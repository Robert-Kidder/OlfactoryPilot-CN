from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from app.models import normalize_digital_target
from app.services.hal import DigitalWriteAck


class AuthorizationViolation(RuntimeError):
    """Raised before a hardware write that is not present in the manifest."""


@dataclass(frozen=True, slots=True)
class AuthorizedWrite:
    sequence: int
    phase: str
    kind: str
    target: str
    value: float | bool
    comp: bool = False
    optional: bool = False

    def canonical(self) -> dict[str, Any]:
        return asdict(self)


def manifest_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AuthorizedHAL:
    """Deny-by-default proxy that consumes one immutable write intent per call."""

    def __init__(
        self,
        delegate: Any,
        writes: Iterable[AuthorizedWrite],
        *,
        audit_sink: Callable[[dict[str, Any]], None] | None = None,
        digital_ack_transform: Callable[
            [AuthorizedWrite, DigitalWriteAck], DigitalWriteAck
        ]
        | None = None,
    ) -> None:
        self._delegate = delegate
        self._writes = tuple(writes)
        self._cursor = 0
        self._audit_sink = audit_sink
        self._digital_ack_transform = digital_ack_transform
        self._violations: list[dict[str, Any]] = []
        expected_sequences = tuple(range(1, len(self._writes) + 1))
        actual_sequences = tuple(item.sequence for item in self._writes)
        if actual_sequences != expected_sequences:
            raise ValueError("授权写入 sequence 必须从 1 开始连续递增。")
        for item in self._writes:
            if item.kind not in {"flow", "digital"}:
                raise ValueError(f"未知授权写入类型：{item.kind}")

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def violations(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._violations)

    @property
    def remaining(self) -> tuple[AuthorizedWrite, ...]:
        return self._writes[self._cursor :]

    @property
    def serial_resources_in_use(self) -> bool:
        return bool(getattr(self._delegate, "serial_resources_in_use", False))

    def advance_to_phase(self, phase: str) -> None:
        """Skip only optional intents until the requested recovery phase begins."""

        while self._cursor < len(self._writes):
            expected = self._writes[self._cursor]
            if expected.phase == phase:
                return
            if not expected.optional:
                self._deny(
                    kind="phase",
                    target=phase,
                    value="",
                    reason=f"不能跳过必需写入 #{expected.sequence}",
                )
            self._record(
                {
                    "event": "authorization_skipped",
                    "reason": f"advance_to_phase:{phase}",
                    "write": expected.canonical(),
                }
            )
            self._cursor += 1
        raise AuthorizationViolation(f"manifest 中不存在阶段：{phase}")

    def set_flow(
        self,
        channel: str | float,
        value: float | None = None,
        *,
        comp: bool = False,
    ) -> bool:
        if value is None:
            value = float(channel)
            channel = "A"
        target = str(channel).strip().upper()
        numeric = float(value)
        intent = self._consume(
            kind="flow",
            target=target,
            value=numeric,
            comp=bool(comp),
        )
        try:
            result = bool(
                self._delegate.set_flow(target, numeric, comp=bool(comp))
            )
        except Exception as exc:
            self._record_result(intent, False, error=repr(exc))
            raise
        self._record_result(intent, result)
        return result

    def write_digital_ack(
        self,
        *,
        device: str | None,
        line: str,
        state: bool,
        timeout_ms: int,
    ) -> DigitalWriteAck:
        target = normalize_digital_target(
            f"{device}/{line}" if device else str(line)
        )
        intent = self._consume(
            kind="digital",
            target=target,
            value=bool(state),
            comp=False,
        )
        try:
            ack = self._delegate.write_digital_ack(
                device=device,
                line=line,
                state=bool(state),
                timeout_ms=int(timeout_ms),
            )
        except Exception as exc:
            self._record_result(intent, False, error=repr(exc))
            raise
        exposed_ack = (
            ack
            if self._digital_ack_transform is None
            else self._digital_ack_transform(intent, ack)
        )
        self._record_result(
            intent,
            bool(exposed_ack.success),
            uncertain=bool(exposed_ack.uncertain),
            message=str(exposed_ack.message),
            delegate_success=bool(ack.success),
            delegate_uncertain=bool(ack.uncertain),
        )
        return exposed_ack

    def write_digital(
        self,
        *,
        device: str | None,
        line: str,
        state: bool,
    ) -> bool:
        return self.write_digital_ack(
            device=device,
            line=line,
            state=state,
            timeout_ms=100,
        ).success

    def prepare_do_output(self) -> bool:
        result = bool(self._delegate.prepare_do_output())
        self._record({"event": "hardware_lifecycle", "operation": "prepare_do_output", "success": result})
        return result

    def release_do_output(self) -> bool:
        result = bool(self._delegate.release_do_output())
        self._record({"event": "hardware_lifecycle", "operation": "release_do_output", "success": result})
        return result

    def read_flow(self) -> float:
        return float(self._delegate.read_flow())

    def stop_heaters(self) -> bool:
        return bool(self._delegate.stop_heaters())

    def flush_logs(self) -> None:
        self._delegate.flush_logs()

    def reset_ai_input(self) -> bool:
        return bool(self._delegate.reset_ai_input())

    def release_serial_resources(self) -> bool:
        return bool(self._delegate.release_serial_resources())

    def assert_required_consumed(self) -> None:
        missing = [item for item in self.remaining if not item.optional]
        if missing:
            joined = ", ".join(f"#{item.sequence}:{item.target}" for item in missing)
            raise AuthorizationViolation(f"必需写入尚未核销：{joined}")

    def close_all(self) -> bool:
        self._deny(
            kind="bulk",
            target="close_all",
            value="",
            reason="禁止无法逐项审计的批量写入",
        )

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(
            f"AuthorizedHAL 拒绝未显式审计的 HAL 属性或方法：{name}"
        )

    def _consume(
        self,
        *,
        kind: str,
        target: str,
        value: float | bool,
        comp: bool,
    ) -> AuthorizedWrite:
        while self._cursor < len(self._writes):
            expected = self._writes[self._cursor]
            if self._matches(expected, kind, target, value, comp):
                self._cursor += 1
                self._record(
                    {
                        "event": "authorization_consumed",
                        "write": expected.canonical(),
                    }
                )
                return expected
            if not expected.optional:
                break
            self._record(
                {
                    "event": "authorization_skipped",
                    "reason": "optional_branch_not_called",
                    "write": expected.canonical(),
                }
            )
            self._cursor += 1
        self._deny(
            kind=kind,
            target=target,
            value=value,
            reason="写入不匹配下一项 manifest",
        )

    @staticmethod
    def _matches(
        expected: AuthorizedWrite,
        kind: str,
        target: str,
        value: float | bool,
        comp: bool,
    ) -> bool:
        if expected.kind != kind or expected.comp != comp:
            return False
        expected_target = (
            normalize_digital_target(expected.target)
            if kind == "digital"
            else expected.target.upper()
        )
        if expected_target != target:
            return False
        if kind == "digital":
            return type(value) is bool and type(expected.value) is bool and value is expected.value
        return (
            type(value) is not bool
            and type(expected.value) is not bool
            and math.isclose(float(value), float(expected.value), abs_tol=1e-9)
        )

    def _deny(
        self,
        *,
        kind: str,
        target: str,
        value: Any,
        reason: str,
    ) -> None:
        expected = (
            None
            if self._cursor >= len(self._writes)
            else self._writes[self._cursor].canonical()
        )
        violation = {
            "event": "authorization_violation",
            "reason": reason,
            "attempt": {"kind": kind, "target": target, "value": value},
            "expected": expected,
        }
        self._violations.append(violation)
        self._record(violation)
        raise AuthorizationViolation(
            f"{reason}；attempt={kind}:{target}={value!r}；expected={expected!r}"
        )

    def _record_result(
        self,
        intent: AuthorizedWrite,
        success: bool,
        **details: Any,
    ) -> None:
        self._record(
            {
                "event": "hardware_write_result",
                "write": intent.canonical(),
                "success": bool(success),
                **details,
            }
        )

    def _record(self, payload: dict[str, Any]) -> None:
        payload = {
            "wall_timestamp": time.time(),
            "monotonic_ns": time.perf_counter_ns(),
            **payload,
        }
        if self._audit_sink is not None:
            self._audit_sink(payload)
