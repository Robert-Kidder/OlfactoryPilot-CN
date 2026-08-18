from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from app.models import (
    ActuationCategory,
    ActuationQualitySnapshot,
    ActuationReceipt,
    CleaningStatus,
    MaintenanceDescriptor,
    MaintenanceProducerFence,
    MaintenanceRecordEnvelope,
    ProtocolGateEvent,
)
from app.models.session import (
    ProducerFence,
    SessionDescriptor,
    SessionRecordEnvelope,
    SessionStatus,
)
from app.services.hal import BreathSampleBatch
from app.services.session_file_service import (
    MAX_STREAM_LINE_BYTES,
    PUBLISH_INCOMPLETE_MARKER,
    _receipt_contract_reason,
)

LOG = logging.getLogger(__name__)
_RECEIPT_TIMING_FIELDS = {
    "expected_ns",
    "started_ns",
    "actual_ns",
    "offset_ms",
    "jitter_ms",
    "measurement_point",
    "actual_duration_ms",
    "target_device",
    "target_line",
    "action_sequence",
    "action_category",
    "action",
}
_CANONICAL_LOG_FIELDS = {
    "schema",
    "schema_version",
    "session_id",
    "session_generation",
    "session_sequence",
    "record_type",
    "event",
    "timestamp",
    "monotonic_ns",
    "source",
    "result",
    "message",
    "producer",
    "producer_sequence",
    "event_id",
}


class _FinalizationCancelled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SessionWriterConfig:
    queue_capacity: int = 4096
    flush_every_records: int = 100
    close_timeout_ms: int = 5000

    @classmethod
    def from_mapping(cls, values: dict[str, Any] | None) -> SessionWriterConfig:
        config = values or {}
        try:
            candidate = cls(
                queue_capacity=int(config.get("session_writer_queue_capacity", 4096)),
                flush_every_records=int(
                    config.get("session_writer_flush_every_records", 100)
                ),
                close_timeout_ms=int(
                    config.get("session_writer_close_timeout_ms", 5000)
                ),
            )
        except (TypeError, ValueError, OverflowError):
            LOG.warning("会话 writer 配置无效，已使用安全默认值。")
            return cls()
        if (
            candidate.queue_capacity <= 0
            or candidate.flush_every_records <= 0
            or candidate.close_timeout_ms <= 0
        ):
            LOG.warning("会话 writer 配置无效，已使用安全默认值。")
            return cls()
        return candidate


@dataclass(frozen=True, slots=True)
class RecorderReadinessSnapshot:
    session_id: str = ""
    generation: int = 0
    recording_ready: bool = False
    failed: bool = False
    message: str = ""


class RecorderReadinessLatch:
    """Producer-safe readiness/generation latch updated before owner wake-up."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot = RecorderReadinessSnapshot()

    def bind(self, descriptor: SessionDescriptor) -> None:
        with self._lock:
            self._snapshot = RecorderReadinessSnapshot(
                session_id=descriptor.session_id,
                generation=descriptor.generation,
            )

    def mark_ready(self, descriptor: SessionDescriptor) -> bool:
        with self._lock:
            if (
                self._snapshot.session_id != descriptor.session_id
                or self._snapshot.generation != descriptor.generation
                or self._snapshot.failed
            ):
                return False
            self._snapshot = RecorderReadinessSnapshot(
                session_id=descriptor.session_id,
                generation=descriptor.generation,
                recording_ready=True,
            )
            return True

    def fail(
        self,
        message: str,
        *,
        session_id: str,
        generation: int,
    ) -> bool:
        with self._lock:
            if (
                self._snapshot.session_id != str(session_id)
                or self._snapshot.generation != int(generation)
                or self._snapshot.failed
            ):
                return False
            self._snapshot = RecorderReadinessSnapshot(
                session_id=self._snapshot.session_id,
                generation=self._snapshot.generation,
                recording_ready=False,
                failed=True,
                message=str(message),
            )
            return True

    def close(self, *, session_id: str, generation: int) -> bool:
        with self._lock:
            if (
                self._snapshot.session_id != str(session_id)
                or self._snapshot.generation != int(generation)
            ):
                return False
            self._snapshot = RecorderReadinessSnapshot(
                session_id=self._snapshot.session_id,
                generation=self._snapshot.generation,
            )
            return True

    def read(self) -> RecorderReadinessSnapshot:
        with self._lock:
            return self._snapshot


@dataclass(frozen=True, slots=True)
class MaintenanceReadinessSnapshot:
    operation_id: str = ""
    generation: int = 0
    recording_ready: bool = False
    failed: bool = False
    message: str = ""


class MaintenanceReadinessLatch:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot = MaintenanceReadinessSnapshot()

    def bind(self, descriptor: MaintenanceDescriptor) -> None:
        with self._lock:
            self._snapshot = MaintenanceReadinessSnapshot(
                operation_id=descriptor.operation_id,
                generation=descriptor.generation,
            )

    def mark_ready(self, descriptor: MaintenanceDescriptor) -> bool:
        with self._lock:
            if (
                self._snapshot.operation_id != descriptor.operation_id
                or self._snapshot.generation != descriptor.generation
                or self._snapshot.failed
            ):
                return False
            self._snapshot = MaintenanceReadinessSnapshot(
                operation_id=descriptor.operation_id,
                generation=descriptor.generation,
                recording_ready=True,
            )
            return True

    def fail(
        self,
        message: str,
        *,
        operation_id: str,
        generation: int,
    ) -> bool:
        with self._lock:
            if (
                self._snapshot.operation_id != operation_id
                or self._snapshot.generation != int(generation)
                or self._snapshot.failed
            ):
                return False
            self._snapshot = MaintenanceReadinessSnapshot(
                operation_id=operation_id,
                generation=int(generation),
                failed=True,
                message=str(message),
            )
            return True

    def close(self, *, operation_id: str, generation: int) -> bool:
        with self._lock:
            if (
                self._snapshot.operation_id != operation_id
                or self._snapshot.generation != int(generation)
            ):
                return False
            self._snapshot = MaintenanceReadinessSnapshot(
                operation_id=operation_id,
                generation=int(generation),
            )
            return True

    def read(self) -> MaintenanceReadinessSnapshot:
        with self._lock:
            return self._snapshot


@dataclass(frozen=True, slots=True)
class SessionWriterFailure:
    session_id: str
    session_generation: int
    stage: str
    path: str
    message: str
    timestamp: str


@dataclass(frozen=True, slots=True)
class SessionFinalizationResult:
    session_id: str
    complete: bool
    status: SessionStatus
    final_dir: Path | None
    message: str


@dataclass(frozen=True, slots=True)
class MaintenanceFinalizationResult:
    operation_id: str
    complete: bool
    status: CleaningStatus
    outcome: str
    final_dir: Path | None
    message: str


@dataclass(frozen=True, slots=True)
class MaintenanceWriterTerminalSnapshot:
    operation_id: str
    generation: int
    status: CleaningStatus
    outcome: str
    persisted_log_event_count: int
    unpersisted_count: int
    reason: str


class SessionRecorderIngress:
    """O(1) producer endpoint: identity validation plus queue.put_nowait only."""

    def __init__(
        self,
        writer: SessionWriterWorker,
        readiness_latch: RecorderReadinessLatch,
    ) -> None:
        self.writer = writer
        self.readiness_latch = readiness_latch
        self._lock = threading.RLock()
        self._last_sequence: dict[str, int] = {}
        self._last_raw_identity: tuple[int, int, int] | None = None
        self._fenced: set[str] = set()

    def post_raw_batch(
        self,
        batch: BreathSampleBatch,
        *,
        producer_sequence: int,
        generation: int | None = None,
    ) -> bool:
        return self._post(
            producer="hardware",
            producer_sequence=producer_sequence,
            generation=generation,
            record_type="raw_batch",
            payload={"batch": batch},
            timestamp=(
                batch.samples[-1].timestamp
                if batch.samples
                else None
            ),
            monotonic_ns=(
                batch.samples[-1].monotonic_ns
                if batch.samples
                else None
            ),
            raw_batch=batch,
        )

    def _raw_batch_identity_ready(self, batch: BreathSampleBatch) -> bool:
        if not batch.samples:
            return False
        first = batch.samples[0]
        last = batch.samples[-1]
        if (
            first.monotonic_ns <= 0
            or first.ai_epoch < 0
            or first.sample_sequence < 0
            or last.monotonic_ns < first.monotonic_ns
            or last.ai_epoch < first.ai_epoch
            or (
                last.ai_epoch == first.ai_epoch
                and last.sample_sequence < first.sample_sequence
            )
        ):
            return False
        if self._last_raw_identity is None:
            return True
        previous_epoch, previous_sample, previous_monotonic = (
            self._last_raw_identity
        )
        return (
            first.monotonic_ns > previous_monotonic
            and first.ai_epoch >= previous_epoch
            and (
                first.ai_epoch != previous_epoch
                or first.sample_sequence > previous_sample
            )
        )

    def post_protocol_event(
        self,
        event: ProtocolGateEvent,
        *,
        producer_sequence: int,
        generation: int | None = None,
    ) -> bool:
        return self._post(
            producer="actuation",
            producer_sequence=producer_sequence,
            generation=generation,
            record_type="protocol_event",
            payload={"event": event},
            timestamp=event.timestamp,
            monotonic_ns=event.monotonic_ns,
        )

    def post_receipt(
        self,
        receipt: ActuationReceipt,
        *,
        producer_sequence: int,
        generation: int | None = None,
    ) -> bool:
        return self._post(
            producer="actuation",
            producer_sequence=producer_sequence,
            generation=generation,
            record_type="receipt",
            payload={"receipt": receipt},
            timestamp=receipt.wall_timestamp,
            monotonic_ns=receipt.actual_ns,
        )

    def post_quality_event(
        self,
        *,
        event: str,
        snapshot: ActuationQualitySnapshot,
        producer_sequence: int,
        command_id: str | None,
        message: str,
        transitions: tuple[dict[str, Any], ...] = (),
        timestamp: float | None = None,
        monotonic_ns: int | None = None,
        generation: int | None = None,
    ) -> bool:
        return self._post(
            producer="actuation",
            producer_sequence=producer_sequence,
            generation=generation,
            record_type="quality_event",
            payload={
                "event": str(event),
                "snapshot": snapshot,
                "command_id": command_id,
                "message": str(message),
                "transitions": tuple(dict(item) for item in transitions),
            },
            timestamp=time.time() if timestamp is None else float(timestamp),
            monotonic_ns=monotonic_ns,
        )

    def post_session_event(
        self,
        *,
        event: str,
        producer_sequence: int,
        source: str,
        result: str,
        message: str,
        payload: dict[str, Any] | None = None,
        generation: int | None = None,
    ) -> bool:
        values = {
            key: value
            for key, value in dict(payload or {}).items()
            if key not in _CANONICAL_LOG_FIELDS
        }
        values.update(
            {
                "event": str(event),
                "source": str(source),
                "result": str(result),
                "message": str(message),
            }
        )
        return self._post(
            producer="controller",
            producer_sequence=producer_sequence,
            generation=generation,
            record_type="session_event",
            payload=values,
            timestamp=time.time(),
            monotonic_ns=None,
        )

    def post_fence(
        self,
        producer: str,
        *,
        producer_sequence: int,
        final_payload: dict[str, Any] | None = None,
    ) -> bool:
        notify_failure = False
        with self._lock:
            if not self._identity_ready(
                producer=producer,
                producer_sequence=producer_sequence,
                generation=None,
                fence=True,
            ):
                notify_failure = True
                accepted = False
            else:
                fence = ProducerFence(
                    session_id=self.writer.descriptor.session_id,
                    session_generation=self.writer.descriptor.generation,
                    producer=producer,
                    producer_sequence=producer_sequence,
                    final_payload=final_payload or {},
                )
                accepted = self.writer.put_nowait(fence, notify_failure=False)
                if accepted:
                    self._fenced.add(producer)
                else:
                    notify_failure = self.writer.failure is not None
        if notify_failure:
            self.writer.notify_failure()
        return accepted

    def _post(
        self,
        *,
        producer: str,
        producer_sequence: int,
        generation: int | None,
        record_type: str,
        payload: dict[str, Any],
        timestamp: float | None,
        monotonic_ns: int | None,
        raw_batch: BreathSampleBatch | None = None,
    ) -> bool:
        notify_failure = False
        with self._lock:
            if raw_batch is not None and not self._raw_batch_identity_ready(
                raw_batch
            ):
                self.writer.fail_from_producer(
                    stage="raw_identity",
                    message="raw sample identity 或 monotonic_ns 重复/倒退。",
                    notify=False,
                )
                notify_failure = True
                accepted = False
            elif not self._identity_ready(
                producer=producer,
                producer_sequence=producer_sequence,
                generation=generation,
                fence=False,
            ):
                notify_failure = True
                accepted = False
            else:
                envelope = SessionRecordEnvelope(
                    session_id=self.writer.descriptor.session_id,
                    session_generation=self.writer.descriptor.generation,
                    producer=producer,
                    producer_sequence=producer_sequence,
                    event_id=(
                        f"{producer}:{self.writer.descriptor.generation}:"
                        f"{producer_sequence}"
                    ),
                    record_type=record_type,
                    payload=payload,
                    timestamp=timestamp,
                    monotonic_ns=monotonic_ns,
                )
                accepted = self.writer.put_nowait(envelope, notify_failure=False)
                if accepted:
                    self._last_sequence[producer] = producer_sequence
                    if raw_batch is not None:
                        last = raw_batch.samples[-1]
                        self._last_raw_identity = (
                            int(last.ai_epoch),
                            int(last.sample_sequence),
                            int(last.monotonic_ns),
                        )
                else:
                    notify_failure = self.writer.failure is not None
        if notify_failure:
            self.writer.notify_failure()
        return accepted

    def _identity_ready(
        self,
        *,
        producer: str,
        producer_sequence: int,
        generation: int | None,
        fence: bool,
    ) -> bool:
        snapshot = self.readiness_latch.read()
        target_generation = (
            self.writer.descriptor.generation
            if generation is None
            else int(generation)
        )
        if (
            not snapshot.recording_ready
            or snapshot.failed
            or target_generation != snapshot.generation
        ):
            self.writer.fail_from_producer(
                stage="identity",
                message=(
                    "recorder generation 不匹配或 recording 未就绪；"
                    "已拒绝旧会话记录。"
                ),
                notify=False,
            )
            return False
        if producer in self._fenced:
            self.writer.fail_from_producer(
                stage="producer_fence",
                message=f"{producer} 已提交 fence，禁止继续提交记录。",
                notify=False,
            )
            return False
        previous = self._last_sequence.get(producer, 0)
        if fence:
            if int(producer_sequence) != previous:
                self.writer.fail_from_producer(
                    stage="producer_sequence",
                    message=(
                        f"{producer} producer sequence 与 fence 不一致："
                        f"expected={previous}, actual={producer_sequence}。"
                    ),
                    notify=False,
                )
                return False
        elif int(producer_sequence) != previous + 1:
            self.writer.fail_from_producer(
                stage="producer_sequence",
                message=(
                    f"{producer} producer sequence 不连续："
                    f"expected={previous + 1}, actual={producer_sequence}。"
                ),
                notify=False,
            )
            return False
        return True


class MaintenanceRecorderIngress:
    """Producer-safe ingress for a log-only maintenance bundle."""

    def __init__(
        self,
        writer: SessionWriterWorker,
        readiness_latch: MaintenanceReadinessLatch,
    ) -> None:
        if not isinstance(writer.descriptor, MaintenanceDescriptor):
            raise TypeError("maintenance ingress 只能绑定 maintenance descriptor。")
        self.writer = writer
        self.readiness_latch = readiness_latch
        self._lock = threading.RLock()
        self._last_sequence: dict[str, int] = {}
        self._fenced: set[str] = set()

    def post_receipt(
        self,
        receipt: ActuationReceipt,
        *,
        producer_sequence: int,
        generation: int | None = None,
    ) -> bool:
        return self._post(
            producer="actuation",
            producer_sequence=producer_sequence,
            generation=generation,
            record_type="receipt",
            payload={"receipt": receipt},
            timestamp=receipt.wall_timestamp,
            monotonic_ns=receipt.actual_ns,
        )

    def post_event(
        self,
        *,
        producer: str,
        producer_sequence: int,
        record_type: str,
        event: str,
        result: str,
        message: str,
        payload: dict[str, Any] | None = None,
        timestamp: float | None = None,
        monotonic_ns: int | None = None,
        generation: int | None = None,
    ) -> bool:
        values = dict(payload or {})
        values.update(
            {
                "event": str(event),
                "result": str(result),
                "message": str(message),
            }
        )
        return self._post(
            producer=str(producer),
            producer_sequence=producer_sequence,
            generation=generation,
            record_type=str(record_type),
            payload=values,
            timestamp=time.time() if timestamp is None else float(timestamp),
            monotonic_ns=monotonic_ns,
        )

    def post_fence(
        self,
        producer: str,
        *,
        producer_sequence: int,
        final_payload: dict[str, Any] | None = None,
    ) -> bool:
        notify = False
        with self._lock:
            if not self._identity_ready(
                producer=producer,
                producer_sequence=producer_sequence,
                generation=None,
                fence=True,
            ):
                accepted = False
                notify = True
            else:
                descriptor = self.writer.descriptor
                assert isinstance(descriptor, MaintenanceDescriptor)
                fence = MaintenanceProducerFence(
                    operation_id=descriptor.operation_id,
                    operation_generation=descriptor.generation,
                    producer=producer,
                    producer_sequence=producer_sequence,
                    final_payload=final_payload or {},
                )
                accepted = self.writer.put_nowait(fence, notify_failure=False)
                if accepted:
                    self._fenced.add(producer)
                else:
                    notify = self.writer.failure is not None
        if notify:
            self.writer.notify_failure()
        return accepted

    def _post(
        self,
        *,
        producer: str,
        producer_sequence: int,
        generation: int | None,
        record_type: str,
        payload: dict[str, Any],
        timestamp: float | None,
        monotonic_ns: int | None,
    ) -> bool:
        notify = False
        with self._lock:
            if not self._identity_ready(
                producer=producer,
                producer_sequence=producer_sequence,
                generation=generation,
                fence=False,
            ):
                accepted = False
                notify = True
            else:
                descriptor = self.writer.descriptor
                assert isinstance(descriptor, MaintenanceDescriptor)
                envelope = MaintenanceRecordEnvelope(
                    operation_id=descriptor.operation_id,
                    operation_generation=descriptor.generation,
                    producer=producer,
                    producer_sequence=producer_sequence,
                    event_id=f"{producer}:{descriptor.generation}:{producer_sequence}",
                    record_type=record_type,
                    payload=payload,
                    timestamp=timestamp,
                    monotonic_ns=monotonic_ns,
                )
                accepted = self.writer.put_nowait(envelope, notify_failure=False)
                if accepted:
                    self._last_sequence[producer] = producer_sequence
                else:
                    notify = self.writer.failure is not None
        if notify:
            self.writer.notify_failure()
        return accepted

    def _identity_ready(
        self,
        *,
        producer: str,
        producer_sequence: int,
        generation: int | None,
        fence: bool,
    ) -> bool:
        descriptor = self.writer.descriptor
        assert isinstance(descriptor, MaintenanceDescriptor)
        snapshot = self.readiness_latch.read()
        target_generation = descriptor.generation if generation is None else int(generation)
        if producer not in self.writer.expected_producers:
            self.writer.fail_from_producer(
                stage="producer",
                message=f"maintenance producer 未注册：{producer}。",
                notify=False,
            )
            return False
        if (
            not snapshot.recording_ready
            or snapshot.failed
            or snapshot.operation_id != descriptor.operation_id
            or target_generation != descriptor.generation
        ):
            self.writer.fail_from_producer(
                stage="identity",
                message="maintenance generation 不匹配或 recorder 未就绪。",
                notify=False,
            )
            return False
        if producer in self._fenced:
            self.writer.fail_from_producer(
                stage="producer_fence",
                message=f"{producer} 已提交 maintenance fence。",
                notify=False,
            )
            return False
        previous = self._last_sequence.get(producer, 0)
        expected = previous if fence else previous + 1
        if int(producer_sequence) != expected:
            self.writer.fail_from_producer(
                stage="producer_sequence",
                message=(
                    f"{producer} maintenance sequence 不连续："
                    f"expected={expected}, actual={producer_sequence}。"
                ),
                notify=False,
            )
            return False
        return True


class SessionWriterWorker(QThread):
    failure_ready = Signal(object)
    finalization_ack = Signal(object)

    def __init__(
        self,
        *,
        descriptor: SessionDescriptor | MaintenanceDescriptor,
        config: SessionWriterConfig | dict[str, Any] | None,
        expected_producers: tuple[str, ...],
        session_started_payload: dict[str, Any],
        readiness_latch: RecorderReadinessLatch | MaintenanceReadinessLatch,
        master_valve_line: str = "",
        fault_injector=None,
        failure_callback=None,
    ) -> None:
        super().__init__()
        self.descriptor = descriptor
        self._maintenance = isinstance(descriptor, MaintenanceDescriptor)
        self.config = (
            config
            if isinstance(config, SessionWriterConfig)
            else SessionWriterConfig.from_mapping(config)
        )
        self.expected_producers = frozenset(expected_producers)
        if self._maintenance and self.expected_producers != {
            "actuation",
            "controller",
            "flow",
        }:
            raise ValueError(
                "maintenance expected_producers 必须恰为 actuation/controller/flow。"
            )
        self.session_started_payload = dict(session_started_payload)
        actual_started_at = self.session_started_payload.get(
            "recording_started_at",
            descriptor.started_at_iso,
        )
        self._recording_started_at_iso = (
            str(actual_started_at)
            if actual_started_at
            else descriptor.started_at_iso
        )
        self.readiness_latch = readiness_latch
        self.readiness_latch.bind(descriptor)
        configured_master = str(master_valve_line or "")
        self._master_target = (
            tuple(configured_master.split("/", 1))
            if "/" in configured_master
            else (None, configured_master)
            if configured_master
            else None
        )
        self._fault_injector = fault_injector
        self._failure_callback = failure_callback
        self._queue: queue.Queue[
            SessionRecordEnvelope
            | ProducerFence
            | MaintenanceRecordEnvelope
            | MaintenanceProducerFence
        ] = queue.Queue(maxsize=self.config.queue_capacity)
        self._state_lock = threading.RLock()
        self._initialized = threading.Event()
        self._initialized_success = False
        self._stop_requested = threading.Event()
        self._close_requested = threading.Event()
        self._finalized = threading.Event()
        self._close_reason = ""
        self._close_deadline_monotonic: float | None = None
        self._final_quality: ActuationQualitySnapshot | None = None
        self._maintenance_status = CleaningStatus.COMPLETED
        self._maintenance_outcome = "completed"
        self._maintenance_failure_reason = ""
        self._final_result: (
            SessionFinalizationResult | MaintenanceFinalizationResult | None
        ) = None
        self._failure: SessionWriterFailure | None = None
        self._maintenance_terminal_snapshot: (
            MaintenanceWriterTerminalSnapshot | None
        ) = None
        self._failure_notified = False
        self._publish_cancelled = threading.Event()
        self._fences: dict[str, int] = {}
        self._queue_high_water = 0
        self._dropped_count = 0
        self._raw_handle = None
        self._log_handle = None
        self._raw_hash = hashlib.sha256()
        self._log_hash = hashlib.sha256()
        self._raw_bytes = 0
        self._log_bytes = 0
        self._raw_record_count = 0
        self._last_raw_sample_identity: tuple[int, int, int] | None = None
        self._log_event_count = 0
        self._receipt_count = 0
        self._session_sequence = 0
        self._records_since_flush = 0
        self._seen_receipts: dict[
            tuple[str, int, str],
            ActuationReceipt,
        ] = {}

    @property
    def failure(self) -> SessionWriterFailure | None:
        with self._state_lock:
            return self._failure

    @property
    def maintenance_terminal_snapshot(
        self,
    ) -> MaintenanceWriterTerminalSnapshot | None:
        with self._state_lock:
            return self._maintenance_terminal_snapshot

    def start_and_wait(self, timeout_ms: int | None = None) -> bool:
        if not self.isRunning() and not self._initialized.is_set():
            self.start()
        timeout = (
            self.config.close_timeout_ms
            if timeout_ms is None
            else max(1, int(timeout_ms))
        )
        started = time.monotonic()
        if not self._initialized.wait(timeout / 1000):
            self.fail_from_producer(
                stage="initialize_timeout",
                message="会话 writer 初始化超时。",
            )
            return False
        if not self._initialized_success and self.isRunning():
            remaining_ms = max(
                1,
                int(timeout - (time.monotonic() - started) * 1000),
            )
            self.wait(remaining_ms)
        return self._initialized_success

    def put_nowait(
        self,
        item: (
            SessionRecordEnvelope
            | ProducerFence
            | MaintenanceRecordEnvelope
            | MaintenanceProducerFence
        ),
        *,
        notify_failure: bool = True,
    ) -> bool:
        if self._stop_requested.is_set() or self._failure is not None:
            return False
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            with self._state_lock:
                self._dropped_count += 1
            self.fail_from_producer(
                stage="queue_full",
                message=(
                    "会话写入队列已满，数据完整性无法保证；"
                    "实验已停止记录，请执行安全停止后新建会话。"
                ),
                notify=notify_failure,
            )
            return False
        with self._state_lock:
            self._queue_high_water = max(
                self._queue_high_water,
                self._queue.qsize(),
            )
        return True

    def fail_from_producer(
        self,
        *,
        stage: str,
        message: str,
        notify: bool = True,
    ) -> None:
        self._fail(
            stage=stage,
            path=self.descriptor.paths.staging_dir,
            detail=message,
            notify=notify,
        )

    def notify_failure(self) -> None:
        with self._state_lock:
            if self._failure is None or self._failure_notified:
                return
            self._failure_notified = True
            failure = self._failure
        if self._failure_callback is not None:
            try:
                self._failure_callback(failure)
            except Exception:
                pass
        self.failure_ready.emit(failure)

    def close(
        self,
        *,
        reason: str,
        final_quality: ActuationQualitySnapshot | None = None,
        timeout_ms: int | None = None,
        maintenance_status: CleaningStatus | None = None,
        maintenance_outcome: str | None = None,
        maintenance_failure_reason: str = "",
    ) -> SessionFinalizationResult | MaintenanceFinalizationResult:
        timeout = (
            self.config.close_timeout_ms
            if timeout_ms is None
            else max(1, int(timeout_ms))
        )
        deadline = time.monotonic() + timeout / 1000.0
        existing_result = None
        with self._state_lock:
            if self._final_result is not None:
                existing_result = self._final_result
            else:
                if (
                    self._close_deadline_monotonic is None
                    or deadline < self._close_deadline_monotonic
                ):
                    self._close_deadline_monotonic = deadline
                if not self._close_requested.is_set():
                    self._close_reason = str(reason)
                    self._final_quality = final_quality
                    if self._maintenance:
                        self._maintenance_status = (
                            CleaningStatus.COMPLETED
                            if maintenance_status is None
                            else CleaningStatus(maintenance_status)
                        )
                        self._maintenance_outcome = str(
                            maintenance_outcome or "completed"
                        )
                        self._maintenance_failure_reason = str(
                            maintenance_failure_reason
                        )
                    self._close_requested.set()
        if existing_result is not None:
            if self.isRunning():
                self.wait(timeout)
            return existing_result
        if not self._finalized.wait(timeout / 1000):
            with self._state_lock:
                if self._final_result is None:
                    self._claim_close_timeout_locked()
            assert self._final_result is not None
            return self._final_result
        assert self._final_result is not None
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if self.isRunning() and remaining_ms > 0:
            self.wait(remaining_ms)
        return self._final_result

    def run(self) -> None:
        try:
            self._initialize_files()
            self._initialized_success = True
            self.readiness_latch.mark_ready(self.descriptor)
        except Exception as exc:
            self._fail_exception("initialize", self.descriptor.paths.staging_dir, exc)
        finally:
            self._initialized.set()
        if not self._initialized_success:
            self._cleanup_handles()
            return

        while not self._stop_requested.is_set():
            try:
                item = self._queue.get(timeout=0.02)
            except queue.Empty:
                item = None
            if item is not None:
                try:
                    self._consume(item)
                except Exception as exc:
                    self._fail_exception(
                        "write",
                        self.descriptor.paths.staging_dir,
                        exc,
                    )
                finally:
                    self._queue.task_done()
            if self._failure is not None:
                break
            if (
                self._close_requested.is_set()
                and self.expected_producers.issubset(self._fences)
                and self._queue.empty()
            ):
                try:
                    self._finalize()
                except Exception as exc:
                    self._fail_exception(
                        "finalize",
                        self.descriptor.paths.staging_dir,
                        exc,
                        recovery_required=True,
                    )
                break
        self._cleanup_handles()

    def _initialize_files(self) -> None:
        if self._maintenance:
            self._initialize_maintenance_files()
            return
        self._raw_handle = self.descriptor.paths.raw_path.open("r+b")
        self._log_handle = self.descriptor.paths.log_path.open("r+b")
        self._assert_empty_reserved_stream(
            self._raw_handle,
            self.descriptor.paths.raw_path,
        )
        self._assert_empty_reserved_stream(
            self._log_handle,
            self.descriptor.paths.log_path,
        )
        metadata = {
            "schema": "olfactorypilot.raw",
            "schema_version": 1,
            "session_id": self.descriptor.session_id,
            "columns": [
                "record_sequence",
                "timestamp",
                "monotonic_ns",
                "ai_epoch",
                "sample_sequence",
                "ai0_raw",
            ],
            "nominal_rate_hz": 100,
        }
        raw_header = (
            "# "
            + json.dumps(
                metadata,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
            + "record_sequence,timestamp,monotonic_ns,ai_epoch,"
            + "sample_sequence,ai0_raw\n"
        ).encode("utf-8")
        self._write_raw(raw_header, "raw_header_write")
        started_payload = {
            key: value
            for key, value in self.session_started_payload.items()
            if key not in _CANONICAL_LOG_FIELDS
        }
        started = {
            "record_type": "session_event",
            "event": "session_started",
            "timestamp": self._recording_started_at_iso,
            "monotonic_ns": None,
            "source": "session",
            "result": "success",
            "message": "会话已开始。",
            "producer": "session",
            "producer_sequence": 1,
            "event_id": f"session:{self.descriptor.generation}:1",
            "subject_original": self.descriptor.subject_original,
            "subject_clean": self.descriptor.subject_clean,
            "condition_original": self.descriptor.condition_original,
            "condition_clean": self.descriptor.condition_clean,
            "stem": self.descriptor.stem,
            "protocol_source": self.descriptor.protocol_source,
            "protocol_metadata": dict(self.descriptor.protocol_metadata),
            **started_payload,
        }
        self._write_log_record(started, stage="log_session_started_write")

    def _initialize_maintenance_files(self) -> None:
        descriptor = self.descriptor
        assert isinstance(descriptor, MaintenanceDescriptor)
        self._log_handle = descriptor.paths.log_path.open("r+b")
        self._assert_empty_reserved_stream(
            self._log_handle,
            descriptor.paths.log_path,
        )
        started_payload = {
            key: value
            for key, value in self.session_started_payload.items()
            if key not in _CANONICAL_LOG_FIELDS
        }
        self._write_log_record(
            {
                "record_type": "maintenance_event",
                "event": "maintenance_started",
                "timestamp": descriptor.started_at_iso,
                "monotonic_ns": None,
                "source": "maintenance",
                "result": "success",
                "message": "maintenance bundle 已绑定并进入记录就绪。",
                "producer": "maintenance",
                "producer_sequence": 1,
                "event_id": f"maintenance:{descriptor.generation}:1",
                "plan_snapshot": dict(descriptor.plan_snapshot),
                "step_count": descriptor.step_count,
                **started_payload,
            },
            stage="log_session_started_write",
        )

    def _consume(
        self,
        item: (
            SessionRecordEnvelope
            | ProducerFence
            | MaintenanceRecordEnvelope
            | MaintenanceProducerFence
        ),
    ) -> None:
        if self._maintenance:
            self._consume_maintenance(item)
            return
        assert isinstance(item, SessionRecordEnvelope | ProducerFence)
        if isinstance(item, ProducerFence):
            if (
                item.session_id != self.descriptor.session_id
                or item.session_generation != self.descriptor.generation
            ):
                raise ValueError("producer fence generation 不匹配")
            self._fences[item.producer] = item.producer_sequence
            if item.producer == "actuation":
                final_quality = item.final_payload.get("quality")
                if isinstance(final_quality, ActuationQualitySnapshot):
                    self._final_quality = final_quality
            return
        if (
            item.session_id != self.descriptor.session_id
            or item.session_generation != self.descriptor.generation
        ):
            raise ValueError("record envelope generation 不匹配")
        if item.record_type == "raw_batch":
            self._consume_raw(item)
        elif item.record_type == "receipt":
            self._consume_receipt(item)
        elif item.record_type == "protocol_event":
            self._consume_protocol_event(item)
        elif item.record_type == "quality_event":
            self._consume_quality_event(item)
        elif item.record_type == "session_event":
            self._consume_session_event(item)
        else:
            raise ValueError(f"未知 session record_type：{item.record_type}")
        self._records_since_flush += 1
        if self._records_since_flush >= self.config.flush_every_records:
            self._periodic_flush()

    def _consume_maintenance(
        self,
        item: (
            SessionRecordEnvelope
            | ProducerFence
            | MaintenanceRecordEnvelope
            | MaintenanceProducerFence
        ),
    ) -> None:
        descriptor = self.descriptor
        assert isinstance(descriptor, MaintenanceDescriptor)
        if isinstance(item, MaintenanceProducerFence):
            if (
                item.operation_id != descriptor.operation_id
                or item.operation_generation != descriptor.generation
            ):
                raise ValueError("maintenance producer fence identity 不匹配。")
            self._fences[item.producer] = item.producer_sequence
            return
        if not isinstance(item, MaintenanceRecordEnvelope):
            raise TypeError("maintenance writer 收到 session envelope。")
        if (
            item.operation_id != descriptor.operation_id
            or item.operation_generation != descriptor.generation
        ):
            raise ValueError("maintenance record identity 不匹配。")
        if item.record_type == "receipt":
            receipt = item.payload.get("receipt")
            if not isinstance(receipt, ActuationReceipt):
                raise TypeError("maintenance receipt payload 无效。")
            if (
                receipt.operation_id != descriptor.operation_id
                or receipt.generation != descriptor.generation
                or receipt.category not in {
                    ActuationCategory.CLEANING,
                    ActuationCategory.SAFETY,
                }
            ):
                raise ValueError("maintenance receipt identity/category 不匹配。")
            identity = (
                descriptor.operation_id,
                descriptor.generation,
                receipt.command_id,
            )
            previous = self._seen_receipts.get(identity)
            if previous is not None:
                if previous != receipt:
                    raise ValueError("maintenance receipt canonical identity 内容冲突。")
                return
            self._seen_receipts[identity] = receipt
            self._receipt_count += 1
            values = {
                "event": "actuation_receipt",
                "result": receipt.result.value,
                "message": receipt.message or "动作回执已记录。",
                "command_id": receipt.command_id,
                "step_id": receipt.step_id,
                "generation": receipt.generation,
                "target": receipt.target,
                "action_kind": (
                    None if receipt.action_kind is None else receipt.action_kind.value
                ),
                "category": receipt.category.value,
                "valve": receipt.valve,
                "safety_generation": receipt.safety_generation,
                "started_ns": receipt.started_ns,
                "actual_ns": receipt.actual_ns,
                "stale": receipt.stale,
            }
        else:
            values = dict(item.payload)
        event = str(values.pop("event"))
        result = str(values.pop("result"))
        message = str(values.pop("message"))
        self._write_log_record(
            {
                "record_type": item.record_type,
                "event": event,
                "timestamp": _timestamp_iso(item.timestamp),
                "monotonic_ns": item.monotonic_ns,
                "source": item.producer,
                "result": result,
                "message": message,
                "producer": item.producer,
                "producer_sequence": item.producer_sequence,
                "event_id": item.event_id,
                **values,
            },
            stage="log_write",
        )
        self._records_since_flush += 1
        if self._records_since_flush >= self.config.flush_every_records:
            self._periodic_flush()

    def _consume_raw(self, envelope: SessionRecordEnvelope) -> None:
        batch = envelope.payload.get("batch")
        if not isinstance(batch, BreathSampleBatch):
            raise TypeError("raw_batch payload 必须是 BreathSampleBatch")
        lines = []
        for sample in batch.samples:
            if self._last_raw_sample_identity is not None:
                previous_epoch, previous_sample, previous_monotonic = (
                    self._last_raw_sample_identity
                )
                if (
                    sample.monotonic_ns <= previous_monotonic
                    or sample.ai_epoch < previous_epoch
                    or (
                        sample.ai_epoch == previous_epoch
                        and sample.sample_sequence <= previous_sample
                    )
                ):
                    raise ValueError(
                        "raw sample identity 或 monotonic_ns 重复/倒退"
                    )
            self._last_raw_sample_identity = (
                int(sample.ai_epoch),
                int(sample.sample_sequence),
                int(sample.monotonic_ns),
            )
            self._raw_record_count += 1
            lines.append(
                f"{self._raw_record_count},{sample.timestamp:.9f},"
                f"{sample.monotonic_ns},{sample.ai_epoch},"
                f"{sample.sample_sequence},{sample.value!r}\n"
            )
        if lines:
            self._write_raw("".join(lines).encode("utf-8"), "raw_write")

    def _consume_receipt(self, envelope: SessionRecordEnvelope) -> None:
        receipt = envelope.payload.get("receipt")
        if not isinstance(receipt, ActuationReceipt):
            raise TypeError("receipt payload 必须是 ActuationReceipt")
        identity = (
            self.descriptor.session_id,
            receipt.execution_epoch,
            receipt.command_id,
        )
        previous_receipt = self._seen_receipts.get(identity)
        if previous_receipt is not None:
            if previous_receipt != receipt:
                raise ValueError(
                    "receipt canonical identity 相同但内容冲突，"
                    "已阻止会话发布。"
                )
            self._write_log_record(
                {
                    "record_type": "protocol_event",
                    "event": "duplicate_receipt_ignored",
                    "timestamp": _timestamp_iso(envelope.timestamp),
                    "monotonic_ns": envelope.monotonic_ns,
                    "source": "actuation",
                    "result": "ignored",
                    "message": "重复动作回执已按 canonical identity 去重。",
                    "producer": envelope.producer,
                    "producer_sequence": envelope.producer_sequence,
                    "event_id": envelope.event_id,
                    "command_id": receipt.command_id,
                    "execution_epoch": receipt.execution_epoch,
                },
                stage="log_write",
            )
            return
        payload = {
            "record_type": "receipt",
            "event": "actuation_receipt",
            "timestamp": _timestamp_iso(receipt.wall_timestamp),
            "monotonic_ns": receipt.actual_ns,
            "source": "actuation",
            "result": receipt.result.value,
            "message": receipt.message or "动作回执已记录。",
            "producer": envelope.producer,
            "producer_sequence": envelope.producer_sequence,
            "event_id": envelope.event_id,
            "canonical_identity": list(identity),
            "command_id": receipt.command_id,
            "execution_epoch": receipt.execution_epoch,
            "arm_epoch": receipt.arm_epoch,
            "sequence": receipt.sequence,
            "trial_id": receipt.trial_id,
            "trial_index": receipt.trial_index,
            "valve": receipt.valve,
            "action": receipt.action.value,
            "category": receipt.category.value,
            "expected_ns": receipt.expected_ns,
            "started_ns": receipt.started_ns,
            "actual_ns": receipt.actual_ns,
            "offset_ms": receipt.offset_ms,
            "jitter_ms": receipt.jitter_ms,
            "measurement_point": receipt.measurement_point,
            "actual_ns_semantics": "daqmx_write_ack",
            "stale": receipt.stale,
            "actual_duration_ms": receipt.actual_duration_ms,
            "target_device": receipt.target_device,
            "target_line": receipt.target_line,
            "operation_id": receipt.operation_id,
            "generation": receipt.generation,
            "step_id": receipt.step_id,
            "action_kind": (
                None if receipt.action_kind is None else receipt.action_kind.value
            ),
            "safety_generation": receipt.safety_generation,
        }
        contract_reason = _receipt_contract_reason(
            payload,
            master_target=self._master_target,
        )
        if contract_reason:
            raise ValueError(contract_reason)
        self._seen_receipts[identity] = receipt
        self._receipt_count += 1
        self._write_log_record(payload, stage="log_write")

    def _consume_protocol_event(self, envelope: SessionRecordEnvelope) -> None:
        event = envelope.payload.get("event")
        if not isinstance(event, ProtocolGateEvent):
            raise TypeError("protocol_event payload 必须是 ProtocolGateEvent")
        values = {
            key: value
            for key, value in event.as_dict().items()
            if key not in _RECEIPT_TIMING_FIELDS
            and key not in _CANONICAL_LOG_FIELDS
            and value is not None
        }
        self._write_log_record(
            {
                "record_type": "protocol_event",
                "event": event.event,
                "timestamp": _timestamp_iso(event.timestamp),
                "monotonic_ns": envelope.monotonic_ns,
                "source": event.trigger_source or "protocol",
                "result": event.result,
                "message": event.message or "协议事件已记录。",
                "producer": envelope.producer,
                "producer_sequence": envelope.producer_sequence,
                "event_id": envelope.event_id,
                **values,
            },
            stage="log_write",
        )

    def _consume_quality_event(self, envelope: SessionRecordEnvelope) -> None:
        snapshot = envelope.payload.get("snapshot")
        if not isinstance(snapshot, ActuationQualitySnapshot):
            raise TypeError("quality_event payload 必须是 ActuationQualitySnapshot")
        transitions = []
        for item in envelope.payload.get("transitions", ()):
            if not isinstance(item, Mapping):
                continue
            stream = str(item.get("stream", ""))
            direction = str(item.get("direction", ""))
            if stream not in {"open", "close", "combined"}:
                continue
            if direction not in {"entered", "recovered"}:
                continue
            transitions.append(
                {
                    "stream": stream,
                    "direction": direction,
                    "p95_ms": item.get("p95_ms"),
                }
            )
        warning = bool(
            snapshot.open.warning
            or snapshot.close.warning
            or snapshot.combined.warning
        )
        result = (
            "severe"
            if snapshot.severe_latched
            else "warning"
            if warning
            else "success"
        )
        self._write_log_record(
            {
                "record_type": "quality_event",
                "event": str(envelope.payload.get("event")),
                "timestamp": _timestamp_iso(envelope.timestamp),
                "monotonic_ns": envelope.monotonic_ns,
                "source": "actuation_metrics",
                "result": result,
                "message": str(envelope.payload.get("message") or "动作质量已更新。"),
                "producer": envelope.producer,
                "producer_sequence": envelope.producer_sequence,
                "event_id": envelope.event_id,
                "command_id": envelope.payload.get("command_id"),
                "transitions": transitions,
                **_quality_fields(snapshot),
            },
            stage="log_write",
        )

    def _consume_session_event(self, envelope: SessionRecordEnvelope) -> None:
        values = dict(envelope.payload)
        event = str(values.pop("event"))
        source = str(values.pop("source"))
        result = str(values.pop("result"))
        message = str(values.pop("message"))
        self._write_log_record(
            {
                "record_type": "session_event",
                "event": event,
                "timestamp": _timestamp_iso(envelope.timestamp),
                "monotonic_ns": envelope.monotonic_ns,
                "source": source,
                "result": result,
                "message": message,
                "producer": envelope.producer,
                "producer_sequence": envelope.producer_sequence,
                "event_id": envelope.event_id,
                **values,
            },
            stage="log_write",
        )

    def _periodic_flush(self) -> None:
        self._fault("periodic_flush", self.descriptor.paths.staging_dir)
        if self._raw_handle is not None:
            self._raw_handle.flush()
        self._log_handle.flush()
        self._records_since_flush = 0

    def _finalize(self) -> None:
        if self._maintenance:
            self._finalize_maintenance()
            return
        self._ensure_publish_allowed()
        ended_at = datetime.now().astimezone()
        final_event_count = self._log_event_count + 1
        closed = {
            "record_type": "session_event",
            "event": "session_closed",
            "timestamp": ended_at.isoformat(timespec="milliseconds"),
            "monotonic_ns": time.perf_counter_ns(),
            "source": "session",
            "result": "success",
            "message": "会话文件已完成收尾，等待 bundle 发布。",
            "producer": "session",
            "producer_sequence": 2,
            "event_id": f"session:{self.descriptor.generation}:2",
            "reason": self._close_reason,
            "started_at": self._recording_started_at_iso,
            "ended_at": ended_at.isoformat(timespec="milliseconds"),
            "sample_count": self._raw_record_count,
            "event_count": final_event_count,
            "receipt_count": self._receipt_count,
            "queue_high_water": self._queue_high_water,
            "dropped_count": self._dropped_count,
            "producer_fences": dict(self._fences),
            "final_quality": (
                None
                if self._final_quality is None
                else _quality_fields(self._final_quality)
            ),
        }
        self._write_log_record(closed, stage="log_write")
        self._ensure_publish_allowed()
        self._flush_fsync_close(
            self._raw_handle,
            self.descriptor.paths.raw_path,
            "raw",
        )
        self._raw_handle = None
        self._flush_fsync_close(
            self._log_handle,
            self.descriptor.paths.log_path,
            "log",
        )
        self._log_handle = None
        manifest = {
            "schema": "olfactorypilot.session",
            "schema_version": 1,
            "status": "complete",
            "session_id": self.descriptor.session_id,
            "session_generation": self.descriptor.generation,
            "stem": self.descriptor.stem,
            "raw_file": self.descriptor.paths.raw_path.name,
            "log_file": self.descriptor.paths.log_path.name,
            "raw_sha256": self._raw_hash.hexdigest(),
            "log_sha256": self._log_hash.hexdigest(),
            "raw_bytes": self._raw_bytes,
            "log_bytes": self._log_bytes,
            "raw_record_count": self._raw_record_count,
            "log_event_count": self._log_event_count,
            "receipt_count": self._receipt_count,
            "queue_high_water": self._queue_high_water,
            "dropped_count": self._dropped_count,
            "last_session_sequence": self._session_sequence,
            "started_at": self._recording_started_at_iso,
            "ended_at": ended_at.isoformat(timespec="milliseconds"),
        }
        self._commit_manifest(manifest)
        self._fault("publish_rename", self.descriptor.paths.staging_dir)
        result = SessionFinalizationResult(
            session_id=self.descriptor.session_id,
            complete=True,
            status=SessionStatus.CLOSED,
            final_dir=self.descriptor.paths.final_dir,
            message="会话已完整发布。",
        )
        self._ensure_publish_allowed()
        if self.descriptor.paths.final_dir.exists():
            raise FileExistsError(
                f"最终会话目录已存在：{self.descriptor.paths.final_dir}"
            )
        self._ensure_publish_allowed()
        os.rename(
            self.descriptor.paths.staging_dir,
            self.descriptor.paths.final_dir,
        )
        rollback = False
        with self._state_lock:
            if self._publish_cancelled.is_set() or self._final_result is not None:
                rollback = True
            elif (
                self._close_deadline_monotonic is not None
                and time.monotonic() >= self._close_deadline_monotonic
            ):
                self._claim_close_timeout_locked()
                rollback = True
            else:
                self._final_result = result
                self.readiness_latch.close(
                    session_id=self.descriptor.session_id,
                    generation=self.descriptor.generation,
                )
                self._finalized.set()
        if rollback:
            try:
                if self.descriptor.paths.final_dir.exists():
                    if self.descriptor.paths.staging_dir.exists():
                        self._mark_publish_rollback_failure(
                            RuntimeError(
                                "staging 路径已重现，无法回滚最终目录"
                            )
                        )
                    else:
                        os.rename(
                            self.descriptor.paths.final_dir,
                            self.descriptor.paths.staging_dir,
                        )
            except Exception as exc:
                self._mark_publish_rollback_failure(exc)
            finally:
                raise _FinalizationCancelled(
                    "publish 完成前收到失败/timeout，已撤销最终目录"
                )
        self.finalization_ack.emit(result)

    def _finalize_maintenance(self) -> None:
        descriptor = self.descriptor
        assert isinstance(descriptor, MaintenanceDescriptor)
        self._ensure_publish_allowed()
        if (
            self._maintenance_status != CleaningStatus.COMPLETED
            or self._maintenance_outcome not in {"completed", "aborted"}
        ):
            raise RuntimeError(
                "失败或 recovery-required maintenance bundle 不得发布 complete。"
            )
        ended_at = datetime.now().astimezone()
        self._write_log_record(
            {
                "record_type": "maintenance_event",
                "event": "maintenance_closed",
                "timestamp": ended_at.isoformat(timespec="milliseconds"),
                "monotonic_ns": time.perf_counter_ns(),
                "source": "maintenance",
                "result": self._maintenance_outcome,
                "message": "maintenance bundle 已完成收尾，等待发布。",
                "producer": "maintenance",
                "producer_sequence": 2,
                "event_id": f"maintenance:{descriptor.generation}:2",
                "reason": self._close_reason,
                "operation_status": self._maintenance_status.value,
                "outcome": self._maintenance_outcome,
                "failure_reason": self._maintenance_failure_reason,
                "receipt_count": self._receipt_count,
                "producer_fences": dict(self._fences),
            },
            stage="log_write",
        )
        self._flush_fsync_close(
            self._log_handle,
            descriptor.paths.log_path,
            "log",
        )
        self._log_handle = None
        manifest = {
            "schema": "maintenance-v1",
            "status": "complete",
            "operation_id": descriptor.operation_id,
            "operation_generation": descriptor.generation,
            "stem": descriptor.stem,
            "log_file": descriptor.paths.log_path.name,
            "plan_snapshot": _to_jsonable(descriptor.plan_snapshot),
            "step_count": descriptor.step_count,
            "receipt_count": self._receipt_count,
            "producer_fences": dict(self._fences),
            "log_sha256": self._log_hash.hexdigest(),
            "log_bytes": self._log_bytes,
            "log_event_count": self._log_event_count,
            "last_sequence": self._session_sequence,
            "operation_status": self._maintenance_status.value,
            "outcome": self._maintenance_outcome,
            "failure_reason": self._maintenance_failure_reason,
            "queue_high_water": self._queue_high_water,
            "dropped_count": self._dropped_count,
            "started_at": self._recording_started_at_iso,
            "ended_at": ended_at.isoformat(timespec="milliseconds"),
        }
        self._commit_manifest(manifest)
        self._fault("publish_rename", descriptor.paths.staging_dir)
        result = MaintenanceFinalizationResult(
            operation_id=descriptor.operation_id,
            complete=True,
            status=CleaningStatus.COMPLETED,
            outcome=self._maintenance_outcome,
            final_dir=descriptor.paths.final_dir,
            message="maintenance bundle 已完整发布。",
        )
        self._ensure_publish_allowed()
        if descriptor.paths.final_dir.exists():
            raise FileExistsError(
                f"最终 maintenance 目录已存在：{descriptor.paths.final_dir}"
            )
        os.rename(descriptor.paths.staging_dir, descriptor.paths.final_dir)
        rollback = False
        with self._state_lock:
            if self._publish_cancelled.is_set() or self._final_result is not None:
                rollback = True
            elif (
                self._close_deadline_monotonic is not None
                and time.monotonic() >= self._close_deadline_monotonic
            ):
                self._claim_close_timeout_locked()
                rollback = True
            else:
                self._final_result = result
                self._maintenance_terminal_snapshot = (
                    MaintenanceWriterTerminalSnapshot(
                        operation_id=descriptor.operation_id,
                        generation=descriptor.generation,
                        status=CleaningStatus.COMPLETED,
                        outcome=self._maintenance_outcome,
                        persisted_log_event_count=self._log_event_count,
                        unpersisted_count=0,
                        reason=self._close_reason,
                    )
                )
                self.readiness_latch.close(
                    operation_id=descriptor.operation_id,
                    generation=descriptor.generation,
                )
                self._finalized.set()
        if rollback:
            try:
                if descriptor.paths.final_dir.exists() and not descriptor.paths.staging_dir.exists():
                    os.rename(descriptor.paths.final_dir, descriptor.paths.staging_dir)
            finally:
                raise _FinalizationCancelled(
                    "maintenance publish 完成前收到失败/timeout，已撤销最终目录"
                )
        self.finalization_ack.emit(result)

    def _claim_close_timeout_locked(self) -> None:
        message = (
            "maintenance 关闭等待 producer fence 超时；安全全关不受阻，"
            "未完成数据保留在 .maintenance.part。"
            if self._maintenance
            else (
                "会话关闭等待 producer fence 超时；硬件安全释放不受阻，"
                "未完成数据保留在 .session.part。"
            )
        )
        if self._maintenance:
            descriptor = self.descriptor
            assert isinstance(descriptor, MaintenanceDescriptor)
            self._final_result = MaintenanceFinalizationResult(
                operation_id=descriptor.operation_id,
                complete=False,
                status=CleaningStatus.RECOVERY_REQUIRED,
                outcome="failed",
                final_dir=None,
                message=message,
            )
            self._maintenance_terminal_snapshot = (
                MaintenanceWriterTerminalSnapshot(
                    operation_id=descriptor.operation_id,
                    generation=descriptor.generation,
                    status=CleaningStatus.RECOVERY_REQUIRED,
                    outcome="failed",
                    persisted_log_event_count=self._log_event_count,
                    unpersisted_count=self._queue.qsize() + self._dropped_count,
                    reason=message,
                )
            )
        else:
            self._final_result = SessionFinalizationResult(
                session_id=self.descriptor.session_id,
                complete=False,
                status=SessionStatus.RECOVERY_REQUIRED,
                final_dir=None,
                message=message,
            )
        self._stop_requested.set()
        self._publish_cancelled.set()
        self._fail_readiness(message)
        self._finalized.set()

    def _mark_publish_rollback_failure(self, error: Exception) -> None:
        marker = (
            self.descriptor.paths.final_dir / PUBLISH_INCOMPLETE_MARKER
        )
        temp = marker.with_suffix(marker.suffix + ".tmp")
        payload = {
            "schema": "olfactorypilot.publish_failure",
            "schema_version": 1,
            "status": "recovery_required",
            "stem": self.descriptor.stem,
            "stage": "publish_rollback",
            "message": str(error),
        }
        if self._maintenance:
            payload.update(
                {
                    "operation_id": self.descriptor.operation_id,
                    "operation_generation": self.descriptor.generation,
                }
            )
        else:
            payload.update(
                {
                    "session_id": self.descriptor.session_id,
                    "session_generation": self.descriptor.generation,
                }
            )
        handle = None
        try:
            handle = temp.open("xb")
            handle.write(
                (
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
            )
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.replace(temp, marker)
        except Exception:
            LOG.exception("无法写入 publish rollback failure marker：%s", marker)
            try:
                if handle is not None:
                    handle.close()
                temp.unlink(missing_ok=True)
            except Exception:
                LOG.exception("清理 publish rollback 临时 marker 失败")
            self._invalidate_published_manifest()

    def _invalidate_published_manifest(self) -> None:
        manifest = self.descriptor.paths.final_manifest_path
        try:
            manifest.unlink(missing_ok=True)
            return
        except Exception:
            LOG.exception("无法删除 publish failure 后的 complete manifest")
        invalid_manifest = manifest.with_name(
            "manifest.publish-incomplete.json"
        )
        try:
            os.replace(manifest, invalid_manifest)
            return
        except Exception:
            LOG.exception("无法隔离 publish failure 后的 complete manifest")
        fallback = self.descriptor.paths.output_dir / (
            f".{self.descriptor.stem}.publish-incomplete.session.part"
        )
        try:
            os.rename(self.descriptor.paths.final_dir, fallback)
        except Exception:
            LOG.exception("无法移动 publish failure 后的最终目录：%s", fallback)

    def _commit_manifest(self, manifest: dict[str, Any]) -> None:
        temp = self.descriptor.paths.staging_dir / "manifest.complete.tmp"
        handle = None
        try:
            handle = temp.open("xb")
            encoded = (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            self._fault("manifest_write", temp)
            self._ensure_publish_allowed()
            handle.write(encoded)
            self._fault("manifest_flush", temp)
            self._ensure_publish_allowed()
            handle.flush()
            self._fault("manifest_fsync", temp)
            self._ensure_publish_allowed()
            os.fsync(handle.fileno())
            self._fault("manifest_close", temp)
            self._ensure_publish_allowed()
            handle.close()
            handle = None
            self._fault("manifest_replace", self.descriptor.paths.manifest_path)
            self._ensure_publish_allowed()
            os.replace(temp, self.descriptor.paths.manifest_path)
        finally:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass

    def _flush_fsync_close(self, handle, path: Path, prefix: str) -> None:
        self._fault(f"{prefix}_flush", path)
        handle.flush()
        self._fault(f"{prefix}_fsync", path)
        os.fsync(handle.fileno())
        self._fault(f"{prefix}_close", path)
        handle.close()

    def _write_raw(self, data: bytes, stage: str) -> None:
        self._fault(stage, self.descriptor.paths.raw_path)
        if stage == "raw_header_write":
            self._assert_empty_reserved_stream(
                self._raw_handle,
                self.descriptor.paths.raw_path,
            )
        written = self._raw_handle.write(data)
        if written != len(data):
            raise OSError("raw 短写")
        self._raw_hash.update(data)
        self._raw_bytes += len(data)

    def _write_log_record(self, values: dict[str, Any], *, stage: str) -> None:
        self._session_sequence += 1
        if self._maintenance:
            descriptor = self.descriptor
            assert isinstance(descriptor, MaintenanceDescriptor)
            record = {
                **_to_jsonable(values),
                "schema": "maintenance-v1.event",
                "operation_id": descriptor.operation_id,
                "operation_generation": descriptor.generation,
                "operation_sequence": self._session_sequence,
            }
        else:
            record = {
                **_to_jsonable(values),
                "schema": "olfactorypilot.event",
                "schema_version": 1,
                "session_id": self.descriptor.session_id,
                "session_generation": self.descriptor.generation,
                "session_sequence": self._session_sequence,
            }
        required = {
            "record_type",
            "event",
            "timestamp",
            "monotonic_ns",
            "source",
            "result",
            "message",
        }
        missing = required.difference(record)
        if missing:
            raise ValueError(f"日志记录缺少稳定字段：{sorted(missing)}")
        encoded = (
            json.dumps(
                record,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_STREAM_LINE_BYTES:
            raise ValueError(
                f"JSONL 单行超过 {MAX_STREAM_LINE_BYTES} bytes 安全上限"
            )
        self._fault(stage, self.descriptor.paths.log_path)
        if stage == "log_session_started_write":
            self._assert_empty_reserved_stream(
                self._log_handle,
                self.descriptor.paths.log_path,
            )
        written = self._log_handle.write(encoded)
        if written != len(encoded):
            raise OSError("log 短写")
        self._log_hash.update(encoded)
        self._log_bytes += len(encoded)
        self._log_event_count += 1

    @staticmethod
    def _assert_empty_reserved_stream(handle, path: Path) -> None:
        if handle.tell() != 0 or os.fstat(handle.fileno()).st_size != 0:
            raise OSError(f"独占预留文件在初始化前已被修改：{path}")

    def _fail_exception(
        self,
        stage: str,
        path: Path,
        exc: Exception,
        *,
        recovery_required: bool = False,
        notify: bool = True,
    ) -> None:
        self._fail(
            stage=stage,
            path=path,
            detail=str(exc),
            recovery_required=recovery_required,
            notify=notify,
        )

    def _fail(
        self,
        *,
        stage: str,
        path: Path,
        detail: str,
        recovery_required: bool = False,
        notify: bool = True,
    ) -> None:
        message = (
            (
                f"maintenance 写入失败（{stage}）：{detail}。"
                "记录 readiness 已锁存失败；系统继续执行安全全关，"
                "请按恢复提示处理隔离目录。"
            )
            if self._maintenance
            else (
                f"会话写入失败（{stage}）：{detail}。请检查磁盘空间或目录权限；"
                "实验已停止记录，请执行安全停止后新建会话。"
            )
        )
        first = self._fail_readiness(message)
        with self._state_lock:
            if self._failure is None:
                self._failure = SessionWriterFailure(
                    session_id=(
                        "" if self._maintenance else self.descriptor.session_id
                    ),
                    session_generation=self.descriptor.generation,
                    stage=stage,
                    path=str(path),
                    message=message,
                    timestamp=datetime.now().astimezone().isoformat(
                        timespec="milliseconds"
                    ),
                )
                self._stop_requested.set()
                self._publish_cancelled.set()
                if self._final_result is None:
                    if self._maintenance:
                        self._final_result = MaintenanceFinalizationResult(
                            operation_id=self.descriptor.operation_id,
                            complete=False,
                            status=(
                                CleaningStatus.RECOVERY_REQUIRED
                                if recovery_required or self._initialized_success
                                else CleaningStatus.FAILED
                            ),
                            outcome="failed",
                            final_dir=None,
                            message=message,
                        )
                        self._maintenance_terminal_snapshot = (
                            MaintenanceWriterTerminalSnapshot(
                                operation_id=self.descriptor.operation_id,
                                generation=self.descriptor.generation,
                                status=self._final_result.status,
                                outcome="failed",
                                persisted_log_event_count=self._log_event_count,
                                unpersisted_count=(
                                    self._queue.qsize() + self._dropped_count
                                ),
                                reason=message,
                            )
                        )
                    else:
                        self._final_result = SessionFinalizationResult(
                            session_id=self.descriptor.session_id,
                            complete=False,
                            status=(
                                SessionStatus.RECOVERY_REQUIRED
                                if recovery_required or self._initialized_success
                                else SessionStatus.FAILED
                            ),
                            final_dir=None,
                            message=message,
                        )
                self._finalized.set()
        if first and notify:
            self.notify_failure()

    def _fail_readiness(self, message: str) -> bool:
        if self._maintenance:
            return self.readiness_latch.fail(
                message,
                operation_id=self.descriptor.operation_id,
                generation=self.descriptor.generation,
            )
        return self.readiness_latch.fail(
            message,
            session_id=self.descriptor.session_id,
            generation=self.descriptor.generation,
        )

    def _ensure_publish_allowed(self) -> None:
        with self._state_lock:
            self._ensure_publish_allowed_locked()

    def _ensure_publish_allowed_locked(self) -> None:
        if self._publish_cancelled.is_set() or self._final_result is not None:
            raise _FinalizationCancelled("finalization 已被失败终态取消")

    def _cleanup_handles(self) -> None:
        for attribute in ("_raw_handle", "_log_handle"):
            handle = getattr(self, attribute)
            if handle is None:
                continue
            try:
                handle.close()
            except Exception:
                pass
            setattr(self, attribute, None)

    def _fault(self, stage: str, path: Path) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage, path)


def _timestamp_iso(value: float | None) -> str:
    if value is None:
        return datetime.now().astimezone().isoformat(timespec="milliseconds")
    return datetime.fromtimestamp(float(value)).astimezone().isoformat(
        timespec="milliseconds"
    )


def _quality_fields(snapshot: ActuationQualitySnapshot) -> dict[str, Any]:
    return {
        "last_jitter_ms": snapshot.last_jitter_ms,
        "p95_open_ms": snapshot.open.p95_ms,
        "p95_close_ms": snapshot.close.p95_ms,
        "p95_combined_ms": snapshot.combined.p95_ms,
        "sample_count_open": snapshot.open.sample_count,
        "sample_count_close": snapshot.close.sample_count,
        "sample_count_combined": snapshot.combined.sample_count,
        "warning_open": snapshot.open.warning,
        "warning_close": snapshot.close.warning,
        "warning_combined": snapshot.combined.warning,
        "severe_latched": snapshot.severe_latched,
    }


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list | set | frozenset):
        return [_to_jsonable(item) for item in value]
    return value
