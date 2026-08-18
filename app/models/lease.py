from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from enum import StrEnum


class DeviceLeaseKind(StrEnum):
    IDLE = "idle"
    PROTOCOL = "protocol"
    MAINTENANCE = "maintenance"
    MANUAL = "manual"
    PRETEST = "pretest"
    COMPENSATION = "compensation"
    CONFIG_CHANGE = "config-change"


@dataclass(frozen=True, slots=True)
class DeviceLeaseToken:
    kind: DeviceLeaseKind
    operation_id: str
    generation: int
    token: str

    def replaced(self, **changes) -> DeviceLeaseToken:
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class MaintenanceLeaseReleaseEvidence:
    operation_terminal: bool
    all_targets_closed: bool
    owner_handoff: bool

    @property
    def complete(self) -> bool:
        return (
            self.operation_terminal
            and self.all_targets_closed
            and self.owner_handoff
        )


class ExclusiveDeviceLease:
    """Thread-safe, all-or-nothing device lease with exact-token release."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._holder: DeviceLeaseToken | None = None

    def acquire(
        self,
        kind: DeviceLeaseKind,
        *,
        operation_id: str,
        generation: int,
    ) -> DeviceLeaseToken | None:
        requested_kind = DeviceLeaseKind(kind)
        if requested_kind == DeviceLeaseKind.IDLE:
            raise ValueError("不能 acquire idle lease。")
        if not str(operation_id).strip():
            raise ValueError("lease operation_id 不能为空。")
        requested_generation = int(generation)
        if requested_generation < 0:
            raise ValueError("lease generation 必须为非负整数。")
        with self._lock:
            if self._holder is not None:
                return None
            token = DeviceLeaseToken(
                kind=requested_kind,
                operation_id=str(operation_id),
                generation=requested_generation,
                token=uuid.uuid4().hex,
            )
            self._holder = token
            return token

    def release(self, token: DeviceLeaseToken) -> bool:
        with self._lock:
            if self._holder != token:
                return False
            self._holder = None
            return True

    def renew(
        self,
        token: DeviceLeaseToken,
        *,
        operation_id: str,
        generation: int,
    ) -> DeviceLeaseToken | None:
        requested_generation = int(generation)
        if requested_generation < token.generation:
            return None
        with self._lock:
            if self._holder != token:
                return None
            renewed = DeviceLeaseToken(
                kind=token.kind,
                operation_id=str(operation_id),
                generation=requested_generation,
                token=uuid.uuid4().hex,
            )
            self._holder = renewed
            return renewed

    @property
    def snapshot(self) -> DeviceLeaseToken:
        with self._lock:
            if self._holder is not None:
                return self._holder
            return DeviceLeaseToken(DeviceLeaseKind.IDLE, "", 0, "")

    def matches(
        self,
        *,
        kind: DeviceLeaseKind,
        operation_id: str | None,
        generation: int | None,
        token: str | None,
    ) -> bool:
        with self._lock:
            holder = self._holder
            return bool(
                holder is not None
                and holder.kind == DeviceLeaseKind(kind)
                and holder.operation_id == operation_id
                and holder.generation == generation
                and holder.token == token
            )
