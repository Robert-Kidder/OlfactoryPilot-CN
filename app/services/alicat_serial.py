from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn

LOG = logging.getLogger(__name__)

_CR = b"\r"
_FIRMWARE_RE = re.compile(r"^(?:GP|\d+v)\S*$", re.IGNORECASE)


class AlicatSerialError(RuntimeError):
    """Base error for a failed Alicat request/response transaction."""


class AlicatSessionDesynchronized(AlicatSerialError):
    """The current serial session can no longer attribute responses safely."""


class AlicatFrameTimeout(AlicatSessionDesynchronized):
    """No response bytes arrived before the configured frame deadline."""


class AlicatPartialFrame(AlicatSessionDesynchronized):
    """Response bytes arrived without the required CR terminator."""


class AlicatResponseMismatch(AlicatSessionDesynchronized):
    """A complete frame does not belong to the command that was sent."""


class AlicatInitialSyncError(AlicatSessionDesynchronized):
    """A newly opened transport did not reach the bounded quiet condition."""


class AlicatTransportError(AlicatSessionDesynchronized):
    """The serial transport failed while a transaction owned the bus."""


@dataclass(frozen=True, slots=True)
class AlicatFrame:
    transaction_id: int
    unit_id: str
    command_kind: str
    raw: bytes
    text: str
    tx_ns: int
    rx_ns: int


Validator = Callable[[str], None]


def initial_resynchronization_windows(
    *, baud_rate: int, frame_timeout_s: float
) -> tuple[float, float]:
    """Return continuous-quiet and total budgets for a newly opened session.

    A response already observed after one frame deadline proves that one
    deadline of quiet is insufficient.  Until read-only HIL measures the real
    Poll/VE/LSS latency envelope, require two configured frame deadlines of
    continuous quiet and allow the same interval once more for a late frame to
    arrive, be drained, and restart that quiet window.  The 3.5-character idle
    requirement remains only a lower bound, never a response deadline.
    """

    timeout = float(frame_timeout_s)
    if int(baud_rate) <= 0 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("baud_rate and frame_timeout_s must be positive")
    character_time_s = 10.0 / float(baud_rate)  # 8-N-1 framing
    delayed_response_quiet_s = 2.0 * timeout
    quiet_required_s = max(delayed_response_quiet_s, 3.5 * character_time_s)
    sync_budget_s = 2.0 * quiet_required_s
    return quiet_required_s, sync_budget_s


def quantize_setpoint_for_wire(value: float) -> float:
    """Return the exact numeric target represented by the ASCII command."""

    target = float(value)
    if not math.isfinite(target):
        raise ValueError("Alicat setpoint must be finite")
    return float(f"{target:.3f}")


def numeric_data_frame_validator(
    unit_id: str,
    *,
    minimum_numeric_fields: int = 5,
    expected_setpoint: float | None = None,
    setpoint_index: int = 4,
    setpoint_tolerance: float = 0.0,
) -> Validator:
    expected_unit = _normalize_unit_id(unit_id)
    tolerance = float(setpoint_tolerance)
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("setpoint_tolerance must be finite and non-negative")
    if expected_setpoint is not None and not math.isfinite(float(expected_setpoint)):
        raise ValueError("expected_setpoint must be finite")

    def validate(text: str) -> None:
        tokens = text.split()
        _validate_unit(tokens, expected_unit)
        if len(tokens) < minimum_numeric_fields + 1:
            raise AlicatResponseMismatch(
                f"Alicat data frame has {max(0, len(tokens) - 1)} value fields; "
                f"expected at least {minimum_numeric_fields} numeric fields"
            )
        numeric: list[float] = []
        for token in tokens[1 : minimum_numeric_fields + 1]:
            try:
                value = float(token)
            except ValueError as exc:
                raise AlicatResponseMismatch(
                    "Alicat data frame has a non-numeric required field"
                ) from exc
            if not math.isfinite(value):
                raise AlicatResponseMismatch("Alicat data frame contains a non-finite value")
            numeric.append(value)
        if len(tokens) <= minimum_numeric_fields + 1:
            raise AlicatResponseMismatch("Alicat data frame has no gas field")
        try:
            float(tokens[minimum_numeric_fields + 1])
        except ValueError:
            pass
        else:
            raise AlicatResponseMismatch("Alicat data frame gas field is numeric")
        if expected_setpoint is not None:
            if len(numeric) <= setpoint_index or not math.isclose(
                numeric[setpoint_index],
                float(expected_setpoint),
                rel_tol=0.0,
                abs_tol=tolerance,
            ):
                actual = None if len(numeric) <= setpoint_index else numeric[setpoint_index]
                raise AlicatResponseMismatch(
                    f"Alicat setpoint response mismatch: expected {expected_setpoint}, got {actual}"
                )

    return validate


def firmware_frame_validator(unit_id: str) -> Validator:
    expected_unit = _normalize_unit_id(unit_id)

    def validate(text: str) -> None:
        tokens = text.split()
        _validate_unit(tokens, expected_unit)
        if len(tokens) < 3 or not _FIRMWARE_RE.fullmatch(tokens[1]):
            raise AlicatResponseMismatch("Alicat firmware response has an unexpected shape")

    return validate


def setpoint_source_frame_validator(unit_id: str) -> Validator:
    expected_unit = _normalize_unit_id(unit_id)

    def validate(text: str) -> None:
        tokens = text.split()
        _validate_unit(tokens, expected_unit)
        if len(tokens) != 2 or tokens[1].upper() not in {"A", "S", "U"}:
            raise AlicatResponseMismatch("Alicat setpoint-source response has an unexpected shape")

    return validate


class AlicatSerialSession:
    """Serialize and attribute one-command/one-response Alicat transactions."""

    def __init__(
        self,
        connection: object,
        *,
        baud_rate: int,
        frame_timeout_s: float,
        lock: threading.RLock | None = None,
        monotonic_ns: Callable[[], int] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if int(baud_rate) <= 0:
            raise ValueError("baud_rate must be positive")
        if not math.isfinite(float(frame_timeout_s)) or float(frame_timeout_s) <= 0:
            raise ValueError("frame_timeout_s must be positive")
        self.connection = connection
        self.baud_rate = int(baud_rate)
        self.frame_timeout_s = float(frame_timeout_s)
        self._lock = lock or threading.RLock()
        self._monotonic_ns = monotonic_ns or time.perf_counter_ns
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleeper or time.sleep
        self._transaction_sequence = 0
        self._synchronized = False
        self._desynchronized = False

    @property
    def desynchronized(self) -> bool:
        return self._desynchronized

    def poll(
        self,
        unit_id: str,
        *,
        expected_setpoint: float | None = None,
        setpoint_tolerance: float = 0.0,
    ) -> AlicatFrame:
        unit = _normalize_unit_id(unit_id)
        return self.transact(
            f"{unit}\r",
            unit_id=unit,
            command_kind="poll",
            validator=numeric_data_frame_validator(
                unit,
                expected_setpoint=expected_setpoint,
                setpoint_tolerance=setpoint_tolerance,
            ),
        )

    def query_firmware(self, unit_id: str) -> AlicatFrame:
        unit = _normalize_unit_id(unit_id)
        return self.transact(
            f"{unit}VE\r",
            unit_id=unit,
            command_kind="firmware",
            validator=firmware_frame_validator(unit),
        )

    def query_setpoint_source(self, unit_id: str) -> AlicatFrame:
        unit = _normalize_unit_id(unit_id)
        return self.transact(
            f"{unit}LSS\r",
            unit_id=unit,
            command_kind="setpoint_source",
            validator=setpoint_source_frame_validator(unit),
        )

    def set_setpoint(
        self,
        unit_id: str,
        value: float,
        *,
        tolerance: float,
    ) -> AlicatFrame:
        unit = _normalize_unit_id(unit_id)
        target = quantize_setpoint_for_wire(value)
        return self.transact(
            f"{unit}s{target:.3f}\r",
            unit_id=unit,
            command_kind="setpoint",
            validator=numeric_data_frame_validator(
                unit,
                expected_setpoint=target,
                setpoint_tolerance=tolerance,
            ),
        )

    def transact(
        self,
        command: str | bytes,
        *,
        unit_id: str,
        command_kind: str,
        validator: Validator,
    ) -> AlicatFrame:
        payload = command.encode("ascii") if isinstance(command, str) else bytes(command)
        if not payload.endswith(_CR) or payload.count(_CR) != 1:
            raise ValueError("Alicat command must contain exactly one trailing CR")
        expected_unit = _normalize_unit_id(unit_id)
        with self._lock:
            if self._desynchronized:
                raise AlicatSessionDesynchronized(
                    "Alicat serial session is desynchronized; close and reopen before retrying"
                )
            if not self._synchronized:
                self._initial_resynchronize_locked()
            try:
                pending = _in_waiting(self.connection)
            except Exception as exc:
                self._fail_desynchronized(
                    AlicatTransportError("Unable to inspect Alicat receive state before TX"),
                    cause=exc,
                )
            if pending:
                self._fail_desynchronized(
                    AlicatResponseMismatch(
                        f"Alicat serial session has {pending} unattributed pending byte(s) before TX"
                    )
                )
            self._transaction_sequence += 1
            transaction_id = self._transaction_sequence
            tx_ns = int(self._monotonic_ns())
            LOG.debug(
                "Alicat transaction TX | id=%s | unit=%s | kind=%s | tx_ns=%s",
                transaction_id,
                expected_unit,
                command_kind,
                tx_ns,
            )
            try:
                written = self.connection.write(payload)
                if written is None or int(written) != len(payload):
                    raise OSError(
                        f"short serial write: wrote {written} of {len(payload)} byte(s)"
                    )
                self.connection.flush()
                self.connection.timeout = self.frame_timeout_s
                receive_started = self._monotonic()
                raw = bytes(self.connection.read_until(_CR))
                receive_elapsed = self._monotonic() - receive_started
            except Exception as exc:
                self._fail_desynchronized(
                    AlicatTransportError(
                        f"Alicat {command_kind} transport failed during TX/RX"
                    ),
                    transaction_id=transaction_id,
                    unit_id=expected_unit,
                    command_kind=command_kind,
                    tx_ns=tx_ns,
                    rx_ns=int(self._monotonic_ns()),
                    cause=exc,
                )
            rx_ns = int(self._monotonic_ns())
            if receive_elapsed > self.frame_timeout_s:
                self._fail_desynchronized(
                    AlicatFrameTimeout(
                        f"Alicat {command_kind} response exceeded "
                        f"{self.frame_timeout_s:.3f}s frame deadline"
                    ),
                    transaction_id=transaction_id,
                    unit_id=expected_unit,
                    command_kind=command_kind,
                    tx_ns=tx_ns,
                    rx_ns=rx_ns,
                    raw=raw,
                )
            if not raw:
                self._fail_desynchronized(
                    AlicatFrameTimeout(
                        f"Alicat {command_kind} response timed out after "
                        f"{self.frame_timeout_s:.3f}s"
                    ),
                    transaction_id=transaction_id,
                    unit_id=expected_unit,
                    command_kind=command_kind,
                    tx_ns=tx_ns,
                    rx_ns=rx_ns,
                    raw=raw,
                )
            if not raw.endswith(_CR):
                self._fail_desynchronized(
                    AlicatPartialFrame(
                        f"Alicat {command_kind} response ended without CR: {raw!r}"
                    ),
                    transaction_id=transaction_id,
                    unit_id=expected_unit,
                    command_kind=command_kind,
                    tx_ns=tx_ns,
                    rx_ns=rx_ns,
                    raw=raw,
                )
            try:
                text = raw[:-1].decode("ascii", errors="strict").strip()
                validator(text)
            except Exception as exc:
                mismatch = (
                    exc
                    if isinstance(exc, AlicatResponseMismatch)
                    else AlicatResponseMismatch(
                        "Alicat response validation did not complete successfully"
                    )
                )
                self._fail_desynchronized(
                    mismatch,
                    transaction_id=transaction_id,
                    unit_id=expected_unit,
                    command_kind=command_kind,
                    tx_ns=tx_ns,
                    rx_ns=rx_ns,
                    raw=raw,
                )
            LOG.debug(
                "Alicat transaction RX | id=%s | unit=%s | kind=%s | rx_ns=%s | "
                "frame_complete=true | classification=matched",
                transaction_id,
                expected_unit,
                command_kind,
                rx_ns,
            )
            return AlicatFrame(
                transaction_id=transaction_id,
                unit_id=expected_unit,
                command_kind=command_kind,
                raw=raw,
                text=text,
                tx_ns=tx_ns,
                rx_ns=rx_ns,
            )

    def _initial_resynchronize_locked(self) -> None:
        # A response deadline is not proven to be the device's maximum latency.
        # It is used here as a configurable bounded quiet window, while 3.5
        # character-times is only the protocol-level lower bound for serial idle.
        character_time_s = 10.0 / float(self.baud_rate)  # 8-N-1 framing
        quiet_required_s, sync_budget_s = initial_resynchronization_windows(
            baud_rate=self.baud_rate,
            frame_timeout_s=self.frame_timeout_s,
        )
        started = self._monotonic()
        deadline = started + sync_budget_s
        quiet_since: float | None = None
        discarded = bytearray()
        while True:
            now = self._monotonic()
            try:
                waiting = _in_waiting(self.connection)
            except Exception as exc:
                self._fail_desynchronized(
                    AlicatInitialSyncError(
                        "Unable to inspect Alicat receive state during initial sync"
                    ),
                    cause=exc,
                )
            if waiting:
                try:
                    chunk = bytes(self.connection.read(waiting))
                except Exception as exc:
                    self._fail_desynchronized(
                        AlicatInitialSyncError(
                            "Unable to drain stale bytes during Alicat initial sync"
                        ),
                        cause=exc,
                    )
                discarded.extend(chunk)
                quiet_since = self._monotonic()
                LOG.debug(
                    "Alicat initial sync discarded stale bytes | count=%s | raw=%r",
                    len(chunk),
                    chunk,
                )
            elif quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= quiet_required_s:
                self._synchronized = True
                LOG.debug(
                    "Alicat initial sync complete | quiet_s=%.6f | budget_s=%.6f | "
                    "discarded_bytes=%s",
                    quiet_required_s,
                    sync_budget_s,
                    len(discarded),
                )
                return
            if now >= deadline:
                self._fail_desynchronized(
                    AlicatInitialSyncError(
                        "Alicat serial transport did not reach bounded quiet before sync deadline"
                    )
                )
            remaining = max(0.0, deadline - now)
            self._sleep(min(character_time_s, remaining))

    def _fail_desynchronized(
        self,
        error: AlicatSessionDesynchronized,
        *,
        transaction_id: int | None = None,
        unit_id: str | None = None,
        command_kind: str | None = None,
        tx_ns: int | None = None,
        rx_ns: int | None = None,
        raw: bytes | None = None,
        cause: Exception | None = None,
    ) -> NoReturn:
        self._desynchronized = True
        LOG.warning(
            "Alicat transaction failed | id=%s | unit=%s | kind=%s | tx_ns=%s | "
            "rx_ns=%s | frame_complete=%s | classification=%s | raw=%r",
            transaction_id,
            unit_id,
            command_kind,
            tx_ns,
            rx_ns,
            bool(raw and raw.endswith(_CR)),
            type(error).__name__,
            raw,
        )
        if cause is not None:
            raise error from cause
        raise error


def _normalize_unit_id(unit_id: str) -> str:
    value = str(unit_id).strip()
    if len(value) != 1 or not value.isascii() or not value.isalpha():
        raise ValueError(f"Invalid Alicat unit ID: {unit_id!r}")
    return value


def _validate_unit(tokens: list[str], expected_unit: str) -> None:
    if not tokens or tokens[0].casefold() != expected_unit.casefold():
        actual = None if not tokens else tokens[0]
        raise AlicatResponseMismatch(
            f"Alicat response unit mismatch: expected {expected_unit}, got {actual}"
        )


def _in_waiting(connection: object) -> int:
    if not hasattr(connection, "in_waiting"):
        raise AttributeError("serial transport does not expose in_waiting")
    waiting = int(connection.in_waiting)
    if waiting < 0:
        raise ValueError("serial transport reported negative in_waiting")
    return waiting
