from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any


class SessionStatus(StrEnum):
    IDLE = "idle"
    PREPARED = "prepared"
    RECORDING = "recording"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_deep_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class SessionViewSnapshot:
    status: SessionStatus = SessionStatus.IDLE
    status_text: str = "尚未建立会话"
    session_id: str = ""
    generation: int = 0
    subject_original: str = ""
    subject_clean: str = ""
    condition_original: str = ""
    condition_clean: str = ""
    stem: str = ""
    staging_path: str = ""
    final_path: str = ""
    raw_path: str = ""
    log_path: str = ""
    can_start: bool = False
    can_end: bool = False
    inputs_enabled: bool = True
    has_protocol: bool = False
    recovery_messages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionPaths:
    output_dir: Path
    staging_dir: Path
    final_dir: Path
    raw_path: Path
    log_path: Path
    manifest_path: Path
    final_raw_path: Path
    final_log_path: Path
    final_manifest_path: Path


@dataclass(frozen=True, slots=True)
class SessionDescriptor:
    session_id: str
    generation: int
    timestamp_text: str
    started_at: float
    started_at_iso: str
    subject_original: str
    subject_clean: str
    condition_original: str
    condition_clean: str
    stem: str
    paths: SessionPaths
    protocol_source: str = ""
    protocol_metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id 不能为空。")
        if self.generation <= 0:
            raise ValueError("session generation 必须为正整数。")
        object.__setattr__(
            self,
            "protocol_metadata",
            MappingProxyType(dict(self.protocol_metadata)),
        )


@dataclass(frozen=True, slots=True)
class MaintenancePaths:
    output_dir: Path
    staging_dir: Path
    final_dir: Path
    log_path: Path
    manifest_path: Path
    final_log_path: Path
    final_manifest_path: Path


@dataclass(frozen=True, slots=True)
class MaintenanceDescriptor:
    operation_id: str
    generation: int
    timestamp_text: str
    started_at: float
    started_at_iso: str
    stem: str
    paths: MaintenancePaths
    plan_snapshot: Mapping[str, Any]
    step_count: int

    def __post_init__(self) -> None:
        if not self.operation_id:
            raise ValueError("maintenance operation_id 不能为空。")
        if self.generation <= 0:
            raise ValueError("maintenance generation 必须为正整数。")
        if self.step_count < 0:
            raise ValueError("maintenance step_count 不得为负数。")
        object.__setattr__(self, "plan_snapshot", _deep_freeze(self.plan_snapshot))


@dataclass(frozen=True, slots=True)
class MaintenanceRecordEnvelope:
    operation_id: str
    operation_generation: int
    producer: str
    producer_sequence: int
    event_id: str
    record_type: str
    payload: Mapping[str, Any]
    timestamp: float | None = None
    monotonic_ns: int | None = None

    def __post_init__(self) -> None:
        if not self.operation_id or self.operation_generation <= 0:
            raise ValueError("maintenance record identity 无效。")
        if not self.producer or self.producer_sequence < 0:
            raise ValueError("maintenance producer identity 无效。")
        if not self.event_id or not self.record_type:
            raise ValueError("maintenance event identity 无效。")
        object.__setattr__(self, "payload", _deep_freeze(self.payload))


@dataclass(frozen=True, slots=True)
class MaintenanceProducerFence:
    operation_id: str
    operation_generation: int
    producer: str
    producer_sequence: int
    final_payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.operation_id or self.operation_generation <= 0 or not self.producer:
            raise ValueError("maintenance producer fence identity 无效。")
        if self.producer_sequence < 0:
            raise ValueError("maintenance producer fence sequence 无效。")
        object.__setattr__(self, "final_payload", _deep_freeze(self.final_payload))

    @property
    def event_id(self) -> str:
        return (
            f"{self.producer}:{self.operation_generation}:"
            f"fence:{self.producer_sequence}"
        )


@dataclass(frozen=True, slots=True)
class SessionRecordEnvelope:
    session_id: str
    session_generation: int
    producer: str
    producer_sequence: int
    event_id: str
    record_type: str
    payload: Mapping[str, Any]
    timestamp: float | None = None
    monotonic_ns: int | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id 不能为空。")
        if self.session_generation <= 0:
            raise ValueError("session_generation 必须为正整数。")
        if not self.producer:
            raise ValueError("producer 不能为空。")
        if self.producer_sequence < 0:
            raise ValueError("producer_sequence 必须为非负整数。")
        if not self.event_id:
            raise ValueError("event_id 不能为空。")
        if not self.record_type:
            raise ValueError("record_type 不能为空。")
        object.__setattr__(self, "payload", _deep_freeze(self.payload))


@dataclass(frozen=True, slots=True)
class ProducerFence:
    session_id: str
    session_generation: int
    producer: str
    producer_sequence: int
    final_payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id or not self.producer:
            raise ValueError("producer fence identity 不能为空。")
        if self.session_generation <= 0 or self.producer_sequence < 0:
            raise ValueError("producer fence generation/sequence 无效。")
        object.__setattr__(self, "final_payload", _deep_freeze(self.final_payload))

    @property
    def event_id(self) -> str:
        return (
            f"{self.producer}:{self.session_generation}:"
            f"fence:{self.producer_sequence}"
        )


class SessionState:
    """Controller-owned session lifecycle with idempotent terminal transitions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._status = SessionStatus.IDLE
        self._descriptor: SessionDescriptor | None = None
        self._close_reason = ""
        self._failure_message = ""

    @property
    def status(self) -> SessionStatus:
        with self._lock:
            return self._status

    @property
    def descriptor(self) -> SessionDescriptor | None:
        with self._lock:
            return self._descriptor

    @property
    def close_reason(self) -> str:
        with self._lock:
            return self._close_reason

    @property
    def failure_message(self) -> str:
        with self._lock:
            return self._failure_message

    def begin(self, descriptor: SessionDescriptor) -> bool:
        with self._lock:
            if self._status == SessionStatus.PREPARED:
                if self._descriptor is not descriptor:
                    return False
                self._status = SessionStatus.RECORDING
                self._close_reason = ""
                self._failure_message = ""
                return True
            if self._status not in {
                SessionStatus.IDLE,
                SessionStatus.CLOSED,
                SessionStatus.FAILED,
                SessionStatus.RECOVERY_REQUIRED,
            }:
                return False
            if self._descriptor is descriptor and self._status == SessionStatus.RECORDING:
                return False
            self._descriptor = descriptor
            self._status = SessionStatus.RECORDING
            self._close_reason = ""
            self._failure_message = ""
            return True

    def prepare(self, descriptor: SessionDescriptor) -> bool:
        with self._lock:
            if self._status not in {
                SessionStatus.IDLE,
                SessionStatus.CLOSED,
                SessionStatus.FAILED,
                SessionStatus.RECOVERY_REQUIRED,
            }:
                return False
            self._descriptor = descriptor
            self._status = SessionStatus.PREPARED
            self._close_reason = ""
            self._failure_message = ""
            return True

    def begin_close(self, reason: str) -> bool:
        with self._lock:
            if self._status != SessionStatus.RECORDING:
                return False
            self._status = SessionStatus.CLOSING
            self._close_reason = str(reason)
            return True

    def cancel_prepared(self, message: str) -> bool:
        with self._lock:
            if self._status != SessionStatus.PREPARED or self._descriptor is None:
                return False
            self._failure_message = str(message)
            self._close_reason = "prepared_cancelled"
            self._status = SessionStatus.RECOVERY_REQUIRED
            return True

    def mark_closed(self, final_dir: Path) -> bool:
        with self._lock:
            if self._status == SessionStatus.CLOSED:
                return False
            if self._status != SessionStatus.CLOSING or self._descriptor is None:
                return False
            if Path(final_dir) != self._descriptor.paths.final_dir:
                raise ValueError("最终目录与活动会话不匹配。")
            self._status = SessionStatus.CLOSED
            return True

    def fail(self, message: str, *, recovery_required: bool = False) -> bool:
        with self._lock:
            if self._status in {
                SessionStatus.CLOSED,
                SessionStatus.FAILED,
                SessionStatus.RECOVERY_REQUIRED,
            }:
                return False
            self._failure_message = str(message)
            self._status = (
                SessionStatus.RECOVERY_REQUIRED
                if recovery_required
                else SessionStatus.FAILED
            )
            return True

    def fail_start(
        self,
        descriptor: SessionDescriptor,
        message: str,
        *,
        recovery_required: bool = True,
    ) -> bool:
        """Record a failed new generation before it ever becomes recording."""
        with self._lock:
            if self._status in {SessionStatus.RECORDING, SessionStatus.CLOSING}:
                return False
            self._descriptor = descriptor
            self._close_reason = ""
            self._failure_message = str(message)
            self._status = (
                SessionStatus.RECOVERY_REQUIRED
                if recovery_required
                else SessionStatus.FAILED
            )
            return True
