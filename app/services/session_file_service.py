from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import threading
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.models import CleaningOperationIdentity
from app.models.session import (
    MaintenanceDescriptor,
    MaintenancePaths,
    SessionDescriptor,
    SessionPaths,
)

WINDOWS_PATH_BUDGET = 240
MAX_COMPONENT_CHARS = 64
OWNER_MARKER_NAME = ".olfactorypilot-session-owner.json"
PUBLISH_INCOMPLETE_MARKER = ".olfactorypilot-publish-incomplete.json"
_INVALID_WINDOWS_CHARACTER = re.compile(r'[<>:"/\\|?*]')
_WHITESPACE = re.compile(r"\s+")
_REPLACEMENTS = re.compile(r"-+")
_READ_CHUNK_BYTES = 64 * 1024
_MAX_IDENTITY_JSON_BYTES = 1024 * 1024
MAX_STREAM_LINE_BYTES = 1024 * 1024
_RAW_COLUMNS = (
    "record_sequence",
    "timestamp",
    "monotonic_ns",
    "ai_epoch",
    "sample_sequence",
    "ai0_raw",
)
_LOG_REQUIRED_FIELDS = {
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
_LOG_RECORD_TYPES = {
    "receipt",
    "protocol_event",
    "quality_event",
    "session_event",
}
_RECEIPT_REQUIRED_FIELDS = {
    "canonical_identity",
    "command_id",
    "execution_epoch",
    "arm_epoch",
    "sequence",
    "trial_id",
    "trial_index",
    "valve",
    "action",
    "category",
    "expected_ns",
    "started_ns",
    "actual_ns",
    "offset_ms",
    "jitter_ms",
    "measurement_point",
    "actual_ns_semantics",
    "stale",
    "actual_duration_ms",
    "target_device",
    "target_line",
}
_QUALITY_REQUIRED_FIELDS = {
    "command_id",
    "transitions",
    "last_jitter_ms",
    "p95_open_ms",
    "p95_close_ms",
    "p95_combined_ms",
    "sample_count_open",
    "sample_count_close",
    "sample_count_combined",
    "warning_open",
    "warning_close",
    "warning_combined",
    "severe_latched",
}
_RESERVED = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$",
    re.IGNORECASE,
)


class SessionFileError(RuntimeError):
    def __init__(self, message: str, *, stage: str = "", path: Path | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.path = path


@dataclass(frozen=True, slots=True)
class SessionPreview:
    timestamp_text: str
    started_at: float
    started_at_iso: str
    subject_original: str
    subject_clean: str
    condition_original: str
    condition_clean: str
    stem: str
    output_dir: Path
    staging_dir: Path
    final_dir: Path
    final_raw_path: Path
    final_log_path: Path
    staging_manifest_path: Path
    editable: bool = False


@dataclass(frozen=True, slots=True)
class BundleValidation:
    path: Path
    complete: bool
    reason: str = ""
    last_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class RecoveryFinding:
    original_path: Path
    reason: str
    quarantined_path: Path | None
    last_sequence: int | None = None


FaultInjector = Callable[[str, Path], None]


class _RecoveryReadCancelled(RuntimeError):
    pass


class _RecoveryReadLimit(RuntimeError):
    pass


def _strict_json_loads(value: str | bytes | bytearray) -> Any:
    def reject_constant(constant: str):
        raise ValueError(f"JSON 非有限数值无效：{constant}")

    return json.loads(value, parse_constant=reject_constant)


def _is_strict_int(value: Any, *, minimum: int = 0) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= minimum
    )


def _is_finite_number(
    value: Any,
    *,
    minimum: float | None = None,
    optional: bool = False,
) -> bool:
    if value is None:
        return optional
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    number = float(value)
    return math.isfinite(number) and (
        minimum is None or number >= minimum
    )


def _receipt_contract_reason(
    record: dict[str, Any],
    *,
    master_target: tuple[str | None, str] | None,
) -> str:
    for field, minimum in (
        ("execution_epoch", 1),
        ("arm_epoch", 0),
        ("sequence", 1),
        ("expected_ns", 0),
    ):
        if not _is_strict_int(record.get(field), minimum=minimum):
            return f"receipt {field} 无效。"
    safety_generation = record.get("safety_generation")
    if safety_generation is not None and not _is_strict_int(
        safety_generation,
        minimum=0,
    ):
        return "receipt safety_generation 无效。"
    valve = record.get("valve")
    if not _is_strict_int(valve):
        return "receipt valve 无效。"
    if valve == 0:
        receipt_target = (
            record.get("target_device"),
            record.get("target_line"),
        )
        action = record.get("action")
        category = record.get("category")
        selector_safety_action = bool(
            category == "safety"
            and action in {"open", "close"}
            and record.get("step_id") == "selector_safe"
            and record.get("action_kind") == action
            and isinstance(record.get("operation_id"), str)
            and bool(record.get("operation_id"))
            and _is_strict_int(record.get("generation"), minimum=0)
        )
        legal_master_action = bool(
            selector_safety_action
            # Read-only compatibility for pre-Story-4.5 bundles. Runtime
            # submission no longer permits generic valve=0 safety closes.
            or (category == "safety" and action == "close")
            or (
                category in {"warmup", "manual", "pretest"}
                and action in {"open", "close"}
            )
            or (
                category == "master"
                and action in {"open", "close"}
            )
        )
        if (
            master_target is None
            or receipt_target != master_target
            or not legal_master_action
        ):
            return (
                "receipt valve=0 仅允许匹配配置主阀目标且 action/category "
                "符合 master_prepare 或安全关闭契约的动作。"
            )
    trial_index = record.get("trial_index")
    if trial_index is not None and not _is_strict_int(trial_index):
        return "receipt trial_index 无效。"
    for field in ("started_ns", "actual_ns"):
        value = record.get(field)
        if value is not None and not _is_strict_int(value):
            return f"receipt {field} 无效。"
    expected_ns = record["expected_ns"]
    started_ns = record.get("started_ns")
    actual_ns = record.get("actual_ns")
    if record.get("result") == "success" and (
        started_ns is None
        or actual_ns is None
        or not expected_ns <= started_ns <= actual_ns
    ):
        return "receipt 成功 timing 顺序无效。"
    if (
        started_ns is not None
        and actual_ns is not None
        and actual_ns < started_ns
    ):
        return "receipt timing 顺序无效。"
    for field, minimum in (
        ("offset_ms", None),
        ("jitter_ms", 0.0),
        ("actual_duration_ms", 0.0),
    ):
        if not _is_finite_number(
            record.get(field),
            minimum=minimum,
            optional=True,
        ):
            return f"receipt {field} 无效。"
    if not isinstance(record.get("stale"), bool):
        return "receipt stale 无效。"
    if record.get("action") not in {"open", "close"}:
        return "receipt action 无效。"
    if record.get("category") not in {
        "normal",
        "safety",
        "warmup",
        "manual",
        "pretest",
        "master",
    }:
        return "receipt category 无效。"
    if (
        record.get("measurement_point") != "daqmx_write_ack"
        or record.get("actual_ns_semantics") != "daqmx_write_ack"
    ):
        return "receipt measurement_point/actual_ns_semantics 无效。"
    return ""


def _quality_contract_reason(record: dict[str, Any]) -> str:
    for field in (
        "last_jitter_ms",
        "p95_open_ms",
        "p95_close_ms",
        "p95_combined_ms",
    ):
        if not _is_finite_number(
            record.get(field),
            minimum=0.0,
            optional=True,
        ):
            return f"quality_event {field} 无效。"
    transitions = record.get("transitions")
    if not isinstance(transitions, list):
        return "quality_event transitions 无效。"
    for transition in transitions:
        if (
            not isinstance(transition, dict)
            or transition.get("stream")
            not in {"open", "close", "combined"}
            or transition.get("direction")
            not in {"entered", "recovered"}
            or not _is_finite_number(
                transition.get("p95_ms"),
                minimum=0.0,
                optional=True,
            )
        ):
            return "quality_event transition 语义无效。"
    return ""


def _read_json_limited(
    path: Path,
    *,
    cancel_event: threading.Event | None = None,
    max_bytes: int = _MAX_IDENTITY_JSON_BYTES,
) -> Any:
    payload = bytearray()
    with path.open("rb") as handle:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise _RecoveryReadCancelled
            chunk = handle.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise _RecoveryReadLimit(
                    f"JSON 超过 {max_bytes} bytes 安全上限"
                )
            if cancel_event is not None and cancel_event.is_set():
                raise _RecoveryReadCancelled
    return _strict_json_loads(payload.decode("utf-8"))


def _iter_bounded_lines(
    handle,
    *,
    cancel_event: threading.Event | None = None,
    max_line_bytes: int = MAX_STREAM_LINE_BYTES,
):
    pending = bytearray()
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise _RecoveryReadCancelled
        chunk = handle.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        pending.extend(chunk)
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            line_size = newline + 1
            if line_size > max_line_bytes:
                raise _RecoveryReadLimit(
                    f"单行超过 {max_line_bytes} bytes 安全上限"
                )
            yield bytes(pending[:line_size])
            del pending[:line_size]
        if len(pending) > max_line_bytes:
            raise _RecoveryReadLimit(
                f"单行超过 {max_line_bytes} bytes 安全上限"
            )
        if cancel_event is not None and cancel_event.is_set():
            raise _RecoveryReadCancelled
    if pending:
        yield bytes(pending)


def _is_network_location(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        root = path.anchor or str(path)
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(root))  # type: ignore[attr-defined]
        return int(drive_type) == 4
    except (AttributeError, OSError, ValueError):
        return False


def utf16_code_units(value: str | Path) -> int:
    return len(str(value).encode("utf-16-le")) // 2


def _truncate_component(original: str, cleaned: str, limit: int) -> str:
    if len(cleaned) <= limit:
        return cleaned
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
    prefix_length = max(1, limit - 9)
    prefix = cleaned[:prefix_length].rstrip(" .-")
    if not prefix:
        prefix = "_"
    return f"{prefix}-{digest}"[:limit]


def sanitize_windows_component(value: str, *, max_chars: int = MAX_COMPONENT_CHARS) -> str:
    original = str(value)
    cleaned = unicodedata.normalize("NFC", original).strip()
    cleaned = "".join(
        "-" if unicodedata.category(character) == "Cc" else character
        for character in cleaned
    )
    cleaned = _INVALID_WINDOWS_CHARACTER.sub("-", cleaned)
    cleaned = _WHITESPACE.sub("-", cleaned)
    cleaned = _REPLACEMENTS.sub("-", cleaned)
    cleaned = cleaned.rstrip(" .")
    if not cleaned or not cleaned.strip("-"):
        raise SessionFileError("受试者或条件清洗后不能为空，请输入有效内容。")
    if _RESERVED.fullmatch(cleaned):
        cleaned = "_" + cleaned
    return _truncate_component(original, cleaned, max(10, int(max_chars)))


class SessionFileService:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        fault_injector: FaultInjector | None = None,
        master_valve_line: str = "",
    ) -> None:
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._fault_injector = fault_injector
        configured_master = str(master_valve_line or "")
        self._master_target = (
            self._split_configured_target(configured_master)
            if configured_master
            else None
        )
        self._active_lock = threading.RLock()
        self._active_staging: set[Path] = set()
        self._orphan_staging: set[Path] = set()

    def mark_active(self, staging_dir: str | Path) -> None:
        with self._active_lock:
            self._active_staging.add(Path(staging_dir).resolve(strict=False))

    def mark_inactive(self, staging_dir: str | Path) -> None:
        with self._active_lock:
            self._active_staging.discard(Path(staging_dir).resolve(strict=False))

    def preview(
        self,
        *,
        output_dir: str | Path,
        subject: str,
        condition: str,
    ) -> SessionPreview:
        root = self._normalize_output(output_dir, require_exists=False)
        started = self._clock()
        if started.tzinfo is None or started.utcoffset() is None:
            started = started.astimezone()
        timestamp_text = started.strftime("%Y%m%d-%H%M%S-%f")[:-3]
        subject_clean = sanitize_windows_component(subject)
        condition_clean = sanitize_windows_component(condition)
        subject_clean, condition_clean = self._fit_path_budget(
            root=root,
            timestamp_text=timestamp_text,
            subject_original=str(subject),
            subject_clean=subject_clean,
            condition_original=str(condition),
            condition_clean=condition_clean,
        )
        stem = f"{timestamp_text}_{subject_clean}_{condition_clean}"
        staging = root / f".{stem}.session.part"
        final = root / stem
        return SessionPreview(
            timestamp_text=timestamp_text,
            started_at=started.timestamp(),
            started_at_iso=started.isoformat(timespec="milliseconds"),
            subject_original=str(subject),
            subject_clean=subject_clean,
            condition_original=str(condition),
            condition_clean=condition_clean,
            stem=stem,
            output_dir=root,
            staging_dir=staging,
            final_dir=final,
            final_raw_path=final / f"{stem}.raw",
            final_log_path=final / f"{stem}.log",
            staging_manifest_path=staging / "manifest.json",
        )

    def reserve(
        self,
        *,
        output_dir: str | Path,
        subject: str,
        condition: str,
        generation: int,
        protocol_source: str = "",
        protocol_metadata: dict[str, str] | None = None,
        preview: SessionPreview | None = None,
    ) -> SessionDescriptor:
        root = self._normalize_output(output_dir, require_exists=True)
        if preview is None:
            preview = self.preview(
                output_dir=root,
                subject=subject,
                condition=condition,
            )
        elif (
            preview.output_dir != root
            or preview.subject_original != str(subject)
            or preview.condition_original != str(condition)
        ):
            raise SessionFileError(
                "会话预览已失效，请刷新受试者、条件和输出目录后重试。",
                stage="preview_identity",
                path=root,
            )
        session_id = str(uuid.uuid4())
        last_collision: Path | None = None
        for collision in range(1000):
            suffix = "" if collision == 0 else f"__{collision:03d}"
            stem = preview.stem + suffix
            staging = root / f".{stem}.session.part"
            final = root / stem
            with self._active_lock:
                try:
                    self._fault("create_staging", staging)
                    staging.mkdir()
                except FileExistsError:
                    last_collision = staging
                    continue
                except Exception as exc:
                    raise SessionFileError(
                        f"无法创建会话工作目录：{exc}。请检查输出目录权限。",
                        stage="create_staging",
                        path=staging,
                    ) from exc

                if final.exists():
                    try:
                        staging.rmdir()
                    except OSError as exc:
                        recovery_identity = {
                            "schema": "olfactorypilot.session-owner",
                            "schema_version": 1,
                            "session_id": session_id,
                            "session_generation": int(generation),
                            "stem": stem,
                        }
                        try:
                            self._create_owner_marker(
                                staging / OWNER_MARKER_NAME,
                                recovery_identity,
                            )
                        except SessionFileError:
                            try:
                                self._create_manifest(
                                    staging / "manifest.json",
                                    {
                                        "schema": "olfactorypilot.session",
                                        "schema_version": 1,
                                        "status": "recovery_required",
                                        "session_id": session_id,
                                        "session_generation": int(generation),
                                        "stem": stem,
                                        "raw_file": f"{stem}.raw",
                                        "log_file": f"{stem}.log",
                                        "failure_stage": "collision_cleanup",
                                    },
                                )
                            except SessionFileError as manifest_exc:
                                self._orphan_staging.add(
                                    staging.resolve(strict=False)
                                )
                                raise SessionFileError(
                                    "碰撞候选工作目录清理失败，且无法写入 "
                                    "ownership marker 或恢复 manifest；"
                                    "请保留该路径并检查目录权限。",
                                    stage="collision_cleanup_identity",
                                    path=staging,
                                ) from manifest_exc
                        raise SessionFileError(
                            "碰撞候选工作目录清理失败，已保留为可恢复数据；"
                            "请检查目录权限后重试。",
                            stage="collision_cleanup",
                            path=staging,
                        ) from exc
                    last_collision = final
                    continue
                self._active_staging.add(staging.resolve(strict=False))

            raw_path = staging / f"{stem}.raw"
            log_path = staging / f"{stem}.log"
            manifest_path = staging / "manifest.json"
            try:
                self._create_owner_marker(
                    staging / OWNER_MARKER_NAME,
                    {
                        "schema": "olfactorypilot.session-owner",
                        "schema_version": 1,
                        "session_id": session_id,
                        "session_generation": int(generation),
                        "stem": stem,
                    },
                )
                self._create_exclusive(raw_path, "create_raw", "原始数据文件")
                self._create_exclusive(log_path, "create_log", "日志文件")
                self._create_manifest(
                    manifest_path,
                    {
                        "schema": "olfactorypilot.session",
                        "schema_version": 1,
                        "status": "recording",
                        "session_id": session_id,
                        "session_generation": int(generation),
                        "stem": stem,
                        "raw_file": raw_path.name,
                        "log_file": log_path.name,
                        "started_at": preview.started_at_iso,
                    },
                )
            except SessionFileError as exc:
                self.mark_inactive(staging)
                if staging.exists():
                    self._preserve_orphan_identity(
                        staging,
                        session_id=session_id,
                        generation=int(generation),
                        stem=stem,
                        failure_stage=exc.stage or "reservation",
                    )
                    raise SessionFileError(
                        str(exc),
                        stage=exc.stage,
                        path=staging,
                    ) from exc
                raise
            paths = SessionPaths(
                output_dir=root,
                staging_dir=staging,
                final_dir=final,
                raw_path=raw_path,
                log_path=log_path,
                manifest_path=manifest_path,
                final_raw_path=final / raw_path.name,
                final_log_path=final / log_path.name,
                final_manifest_path=final / manifest_path.name,
            )
            return SessionDescriptor(
                session_id=session_id,
                generation=int(generation),
                timestamp_text=preview.timestamp_text,
                started_at=preview.started_at,
                started_at_iso=preview.started_at_iso,
                subject_original=preview.subject_original,
                subject_clean=preview.subject_clean,
                condition_original=preview.condition_original,
                condition_clean=preview.condition_clean,
                stem=stem,
                paths=paths,
                protocol_source=str(protocol_source),
                protocol_metadata=protocol_metadata or {},
            )
        raise SessionFileError(
            "会话文件名碰撞已达到 __999，请更换输出目录或稍后重试。",
            stage="collision",
            path=last_collision,
        )

    def reserve_maintenance(
        self,
        *,
        output_dir: str | Path,
        identity: CleaningOperationIdentity,
        plan_snapshot: dict[str, Any],
        step_count: int,
    ) -> MaintenanceDescriptor:
        experiment_root = self._normalize_output(output_dir, require_exists=True)
        root = experiment_root / "maintenance"
        try:
            root.mkdir(exist_ok=True)
        except Exception as exc:
            raise SessionFileError(
                f"无法创建 maintenance 根目录：{exc}。请检查输出目录权限。",
                stage="create_maintenance_root",
                path=root,
            ) from exc
        started = self._clock()
        if started.tzinfo is None or started.utcoffset() is None:
            started = started.astimezone()
        timestamp_text = started.strftime("%Y%m%d-%H%M%S-%f")[:-3]
        operation_component = sanitize_windows_component(identity.operation_id)
        base_stem = f"{timestamp_text}_cleaning_{operation_component}"
        last_collision: Path | None = None
        for collision in range(1000):
            suffix = "" if collision == 0 else f"__{collision:03d}"
            stem = base_stem + suffix
            staging = root / f".{stem}.maintenance.part"
            final = root / stem
            log_path = staging / f"{stem}.log"
            manifest_path = staging / "manifest.json"
            if max(
                len(str(log_path)),
                len(str(final / log_path.name)),
                len(str(manifest_path)),
            ) > WINDOWS_PATH_BUDGET:
                raise SessionFileError(
                    "maintenance bundle 路径超过 Windows 安全预算，请缩短输出目录。",
                    stage="path_budget",
                    path=staging,
                )
            with self._active_lock:
                try:
                    self._fault("create_staging", staging)
                    staging.mkdir()
                except FileExistsError:
                    last_collision = staging
                    continue
                except Exception as exc:
                    raise SessionFileError(
                        f"无法创建 maintenance 工作目录：{exc}。",
                        stage="create_staging",
                        path=staging,
                    ) from exc
                if final.exists():
                    try:
                        staging.rmdir()
                    except OSError as cleanup_exc:
                        self._orphan_staging.add(staging.resolve(strict=False))
                        raise SessionFileError(
                            "maintenance 碰撞目录无法安全清理，已保留供恢复。",
                            stage="collision_cleanup",
                            path=staging,
                        ) from cleanup_exc
                    last_collision = final
                    continue
                self._active_staging.add(staging.resolve(strict=False))
            try:
                self._create_owner_marker(
                    staging / OWNER_MARKER_NAME,
                    {
                        "schema": "olfactorypilot.maintenance-owner",
                        "schema_version": 1,
                        "operation_id": identity.operation_id,
                        "operation_generation": identity.generation,
                        "stem": stem,
                    },
                )
                self._create_exclusive(log_path, "create_log", "maintenance 日志文件")
                self._create_manifest(
                    manifest_path,
                    {
                        "schema": "maintenance-v1",
                        "status": "recording",
                        "operation_id": identity.operation_id,
                        "operation_generation": identity.generation,
                        "stem": stem,
                        "log_file": log_path.name,
                        "started_at": started.isoformat(timespec="milliseconds"),
                    },
                )
            except SessionFileError:
                self.mark_inactive(staging)
                self._orphan_staging.add(staging.resolve(strict=False))
                raise
            paths = MaintenancePaths(
                output_dir=root,
                staging_dir=staging,
                final_dir=final,
                log_path=log_path,
                manifest_path=manifest_path,
                final_log_path=final / log_path.name,
                final_manifest_path=final / manifest_path.name,
            )
            return MaintenanceDescriptor(
                operation_id=identity.operation_id,
                generation=identity.generation,
                timestamp_text=timestamp_text,
                started_at=started.timestamp(),
                started_at_iso=started.isoformat(timespec="milliseconds"),
                stem=stem,
                paths=paths,
                plan_snapshot=plan_snapshot,
                step_count=int(step_count),
            )
        raise SessionFileError(
            "maintenance 文件名碰撞已达到 __999，请更换输出目录或稍后重试。",
            stage="collision",
            path=last_collision,
        )

    def validate_complete_bundle(
        self,
        path: str | Path,
        *,
        cancel_event: threading.Event | None = None,
    ) -> BundleValidation:
        bundle = Path(path)
        if cancel_event is not None and cancel_event.is_set():
            return BundleValidation(bundle, False, "恢复扫描已取消。")
        if (bundle / PUBLISH_INCOMPLETE_MARKER).is_file():
            return BundleValidation(
                bundle,
                False,
                "bundle 发布回滚失败，必须作为不完整会话恢复。",
            )
        manifest_path = bundle / "manifest.json"
        try:
            manifest = _read_json_limited(
                manifest_path,
                cancel_event=cancel_event,
            )
        except _RecoveryReadCancelled:
            return BundleValidation(bundle, False, "恢复扫描已取消。")
        except _RecoveryReadLimit as exc:
            return BundleValidation(bundle, False, f"manifest 过大：{exc}")
        except Exception as exc:
            return BundleValidation(
                bundle,
                False,
                f"manifest 缺失、非法 UTF-8 或无法读取：{exc}",
            )
        if not isinstance(manifest, dict):
            return BundleValidation(bundle, False, "manifest JSON 根必须是对象。")
        if manifest.get("schema") != "olfactorypilot.session":
            return BundleValidation(bundle, False, "manifest schema 无效。")
        if not _is_strict_int(manifest.get("schema_version"), minimum=1) or (
            manifest["schema_version"] != 1
        ):
            return BundleValidation(bundle, False, "manifest schema_version 无效。")
        if manifest.get("stem") != bundle.name:
            return BundleValidation(bundle, False, "manifest stem 无效。")
        if not isinstance(manifest.get("session_id"), str) or not manifest["session_id"]:
            return BundleValidation(bundle, False, "manifest session_id 无效。")
        generation = manifest.get("session_generation")
        if not _is_strict_int(generation, minimum=1):
            return BundleValidation(bundle, False, "manifest session_generation 无效。")
        if manifest.get("status") != "complete":
            return BundleValidation(
                bundle,
                False,
                f"manifest 状态为 {manifest.get('status', 'missing')}，不是 complete。",
                self._last_sequence(manifest),
            )
        owner = self._session_owner_identity(
            bundle / OWNER_MARKER_NAME,
            expected_stem=bundle.name,
            cancel_event=cancel_event,
        )
        if owner is None:
            return BundleValidation(
                bundle,
                False,
                "ownership marker 缺失、无效或无法读取。",
            )
        if (
            owner.get("session_id") != manifest["session_id"]
            or owner.get("session_generation") != generation
        ):
            return BundleValidation(
                bundle,
                False,
                "ownership marker 与 manifest identity 不一致。",
            )
        for field in (
            "raw_bytes",
            "log_bytes",
            "raw_record_count",
            "log_event_count",
            "receipt_count",
            "queue_high_water",
            "dropped_count",
            "last_session_sequence",
        ):
            if not _is_strict_int(manifest.get(field)):
                return BundleValidation(bundle, False, f"manifest {field} 无效。")
        manifest_last_sequence = self._last_sequence(manifest)
        if manifest["dropped_count"] != 0:
            return BundleValidation(
                bundle,
                False,
                "成功会话 manifest dropped_count 必须为 0。",
            )
        for field in ("raw_sha256", "log_sha256"):
            digest = manifest.get(field)
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                return BundleValidation(bundle, False, f"manifest {field} 无效。")
        raw_name = manifest.get("raw_file")
        log_name = manifest.get("log_file")
        expected_names = {
            "raw": f"{bundle.name}.raw",
            "log": f"{bundle.name}.log",
        }
        for label, name in (("raw", raw_name), ("log", log_name)):
            if (
                not isinstance(name, str)
                or not name
                or Path(name).name != name
                or "/" in name
                or "\\" in name
                or name != expected_names[label]
            ):
                return BundleValidation(
                    bundle,
                    False,
                    f"manifest {label}_file 必须是 bundle 内的规范 basename。",
                    self._last_sequence(manifest),
                )
        raw_path = bundle / raw_name
        log_path = bundle / log_name
        raw_hash = hashlib.sha256()
        raw_bytes = 0
        raw_record_count = 0
        raw_metadata: dict[str, Any] | None = None
        raw_csv_header_seen = False
        previous_raw_identity: tuple[int, int, int] | None = None
        try:
            if not raw_path.is_file():
                return BundleValidation(bundle, False, "raw 文件缺失。")
            with raw_path.open("rb") as handle:
                for line_number, encoded in enumerate(
                    _iter_bounded_lines(handle, cancel_event=cancel_event),
                    start=1,
                ):
                    raw_hash.update(encoded)
                    raw_bytes += len(encoded)
                    if not encoded.endswith(b"\n"):
                        return BundleValidation(
                            bundle,
                            False,
                            "raw 每行必须以 LF 换行结束。",
                        )
                    text = encoded[:-1].decode("utf-8")
                    if text.endswith("\r"):
                        return BundleValidation(
                            bundle,
                            False,
                            "raw 必须使用固定 LF 换行。",
                        )
                    if line_number == 1:
                        if not text.startswith("# "):
                            return BundleValidation(
                                bundle,
                                False,
                                "raw header 缺失或格式无效。",
                            )
                        parsed = _strict_json_loads(text[2:])
                        if not isinstance(parsed, dict):
                            return BundleValidation(
                                bundle,
                                False,
                                "raw metadata JSON 根必须是对象。",
                            )
                        raw_metadata = parsed
                    elif line_number == 2:
                        if text != ",".join(_RAW_COLUMNS):
                            return BundleValidation(
                                bundle,
                                False,
                                "raw CSV header 与 v1 固定列不一致。",
                            )
                        raw_csv_header_seen = True
                    else:
                        row = next(csv.reader([text]))
                        if len(row) != len(_RAW_COLUMNS):
                            return BundleValidation(
                                bundle,
                                False,
                                "raw CSV 数据行列数无效。",
                            )
                        try:
                            record_sequence = int(row[0])
                            timestamp = float(row[1])
                            monotonic_ns = int(row[2])
                            ai_epoch = int(row[3])
                            sample_sequence = int(row[4])
                            ai0_raw = float(row[5])
                        except (TypeError, ValueError, OverflowError):
                            return BundleValidation(
                                bundle,
                                False,
                                "raw CSV 数据行字段类型无效。",
                            )
                        if (
                            str(record_sequence) != row[0]
                            or record_sequence != raw_record_count + 1
                            or not math.isfinite(timestamp)
                            or str(monotonic_ns) != row[2]
                            or monotonic_ns <= 0
                            or str(ai_epoch) != row[3]
                            or ai_epoch < 0
                            or str(sample_sequence) != row[4]
                            or sample_sequence < 0
                            or not math.isfinite(ai0_raw)
                        ):
                            return BundleValidation(
                                bundle,
                                False,
                                "raw CSV 数据行结构或序列无效。",
                            )
                        if previous_raw_identity is not None:
                            previous_epoch, previous_sample, previous_monotonic = (
                                previous_raw_identity
                            )
                            if (
                                monotonic_ns <= previous_monotonic
                                or ai_epoch < previous_epoch
                                or (
                                    ai_epoch == previous_epoch
                                    and sample_sequence <= previous_sample
                                )
                            ):
                                return BundleValidation(
                                    bundle,
                                    False,
                                    "raw sample identity 或 monotonic_ns 重复/倒退。",
                                )
                        previous_raw_identity = (
                            ai_epoch,
                            sample_sequence,
                            monotonic_ns,
                        )
                        raw_record_count += 1
        except _RecoveryReadCancelled:
            return BundleValidation(bundle, False, "恢复扫描已取消。")
        except _RecoveryReadLimit as exc:
            return BundleValidation(bundle, False, f"raw 读取被限止：{exc}")
        except UnicodeDecodeError as exc:
            return BundleValidation(bundle, False, f"raw 不是合法 UTF-8：{exc}")
        except json.JSONDecodeError as exc:
            return BundleValidation(bundle, False, f"raw metadata JSON 无效：{exc}")
        except csv.Error as exc:
            return BundleValidation(
                bundle,
                False,
                f"raw CSV 无效：{exc}",
                manifest_last_sequence,
            )
        except (OSError, ValueError) as exc:
            return BundleValidation(bundle, False, f"raw 文件无法读取：{exc}")
        if raw_metadata is None:
            return BundleValidation(bundle, False, "raw header 缺失或格式无效。")
        if not raw_csv_header_seen:
            return BundleValidation(bundle, False, "raw CSV header 缺失。")
        if (
            raw_metadata.get("schema") != "olfactorypilot.raw"
            or raw_metadata.get("session_id") != manifest["session_id"]
        ):
            return BundleValidation(bundle, False, "raw metadata identity 无效。")
        if (
            not _is_strict_int(raw_metadata.get("schema_version"), minimum=1)
            or raw_metadata["schema_version"] != 1
            or raw_metadata.get("columns") != list(_RAW_COLUMNS)
            or not _is_strict_int(
                raw_metadata.get("nominal_rate_hz"),
                minimum=1,
            )
            or raw_metadata["nominal_rate_hz"] != 100
        ):
            return BundleValidation(bundle, False, "raw metadata v1 schema 无效。")

        log_hash = hashlib.sha256()
        log_bytes = 0
        log_event_count = 0
        observed_receipt_count = 0
        first_event = ""
        last_record: dict[str, Any] | None = None
        seen_event_ids: set[str] = set()
        producer_sequences: dict[str, int] = {}
        try:
            if not log_path.is_file():
                return BundleValidation(bundle, False, "log 文件缺失。")
            with log_path.open("rb") as handle:
                for encoded in _iter_bounded_lines(
                    handle,
                    cancel_event=cancel_event,
                ):
                    log_hash.update(encoded)
                    log_bytes += len(encoded)
                    if not encoded.endswith(b"\n"):
                        return BundleValidation(
                            bundle,
                            False,
                            "log 每行必须以 LF 换行结束。",
                        )
                    text = encoded[:-1].decode("utf-8")
                    if text.endswith("\r"):
                        return BundleValidation(
                            bundle,
                            False,
                            "log 必须使用固定 LF 换行。",
                        )
                    if not text.strip():
                        return BundleValidation(
                            bundle,
                            False,
                            "log JSONL 不允许空白行；每行必须恰好包含一个对象。",
                        )
                    log_event_count += 1
                    record = _strict_json_loads(text)
                    if not isinstance(record, dict):
                        return BundleValidation(
                            bundle,
                            False,
                            "log JSONL record 必须是对象。",
                        )
                    missing = _LOG_REQUIRED_FIELDS.difference(record)
                    if missing:
                        return BundleValidation(
                            bundle,
                            False,
                            "log JSONL 缺少稳定字段："
                            + ", ".join(sorted(missing)),
                        )
                    if record.get("schema") != "olfactorypilot.event":
                        return BundleValidation(
                            bundle,
                            False,
                            "log JSONL identity/sequence 无效。",
                        )
                    if (
                        not _is_strict_int(
                            record.get("schema_version"),
                            minimum=1,
                        )
                        or record["schema_version"] != 1
                        or record.get("session_id") != manifest["session_id"]
                        or not _is_strict_int(
                            record.get("session_generation"),
                            minimum=1,
                        )
                        or record["session_generation"] != generation
                        or not _is_strict_int(
                            record.get("session_sequence"),
                            minimum=1,
                        )
                        or record["session_sequence"] != log_event_count
                        or not _is_strict_int(
                            record.get("producer_sequence"),
                        )
                    ):
                        return BundleValidation(
                            bundle,
                            False,
                            "log JSONL identity/sequence 无效。",
                        )
                    monotonic_ns = record.get("monotonic_ns")
                    if monotonic_ns is not None and not _is_strict_int(
                        monotonic_ns
                    ):
                        return BundleValidation(
                            bundle,
                            False,
                            "log JSONL monotonic_ns 无效。",
                        )
                    for field in (
                        "record_type",
                        "event",
                        "timestamp",
                        "source",
                        "result",
                        "message",
                        "producer",
                        "event_id",
                    ):
                        if not isinstance(record.get(field), str) or not record[field]:
                            return BundleValidation(
                                bundle,
                                False,
                                f"log JSONL {field} 无效。",
                            )
                    record_type = record["record_type"]
                    if record_type not in _LOG_RECORD_TYPES:
                        return BundleValidation(
                            bundle,
                            False,
                            f"log JSONL record_type 未知：{record_type}。",
                        )
                    event_id = record["event_id"]
                    if event_id in seen_event_ids:
                        return BundleValidation(
                            bundle,
                            False,
                            "log JSONL event_id 重复。",
                        )
                    seen_event_ids.add(event_id)
                    producer = record["producer"]
                    producer_sequence = record["producer_sequence"]
                    expected_producer_sequence = (
                        producer_sequences.get(producer, 0) + 1
                    )
                    if producer_sequence != expected_producer_sequence:
                        return BundleValidation(
                            bundle,
                            False,
                            "log JSONL producer_sequence 重复、跳号或倒退。",
                        )
                    producer_sequences[producer] = producer_sequence
                    if record_type == "receipt":
                        missing_receipt = _RECEIPT_REQUIRED_FIELDS.difference(
                            record
                        )
                        if missing_receipt:
                            return BundleValidation(
                                bundle,
                                False,
                                "receipt 缺少专属字段："
                                + ", ".join(sorted(missing_receipt)),
                            )
                        canonical = record.get("canonical_identity")
                        if (
                            not isinstance(canonical, list)
                            or len(canonical) != 3
                            or canonical[0] != manifest["session_id"]
                            or canonical[1] != record.get("execution_epoch")
                            or canonical[2] != record.get("command_id")
                        ):
                            return BundleValidation(
                                bundle,
                                False,
                                "receipt canonical identity 无效。",
                            )
                        receipt_reason = _receipt_contract_reason(
                            record,
                            master_target=self._master_target,
                        )
                        if receipt_reason:
                            return BundleValidation(
                                bundle,
                                False,
                                receipt_reason,
                            )
                        observed_receipt_count += 1
                    elif record_type == "quality_event":
                        missing_quality = _QUALITY_REQUIRED_FIELDS.difference(
                            record
                        )
                        if missing_quality:
                            return BundleValidation(
                                bundle,
                                False,
                                "quality_event 缺少专属字段："
                                + ", ".join(sorted(missing_quality)),
                            )
                        for field in (
                            "sample_count_open",
                            "sample_count_close",
                            "sample_count_combined",
                        ):
                            if not _is_strict_int(record.get(field)):
                                return BundleValidation(
                                    bundle,
                                    False,
                                    f"quality_event {field} 无效。",
                                )
                        for field in (
                            "warning_open",
                            "warning_close",
                            "warning_combined",
                            "severe_latched",
                        ):
                            if not isinstance(record.get(field), bool):
                                return BundleValidation(
                                    bundle,
                                    False,
                                    f"quality_event {field} 无效。",
                                )
                        quality_reason = _quality_contract_reason(record)
                        if quality_reason:
                            return BundleValidation(
                                bundle,
                                False,
                                quality_reason,
                            )
                    try:
                        parsed_timestamp = datetime.fromisoformat(
                            record["timestamp"]
                        )
                    except ValueError:
                        return BundleValidation(
                            bundle,
                            False,
                            "log JSONL timestamp 不是 ISO-8601。",
                        )
                    if (
                        parsed_timestamp.tzinfo is None
                        or parsed_timestamp.utcoffset() is None
                    ):
                        return BundleValidation(
                            bundle,
                            False,
                            "log JSONL timestamp 缺少 UTC offset。",
                        )
                    if log_event_count == 1:
                        first_event = record["event"]
                    last_record = record
        except _RecoveryReadCancelled:
            return BundleValidation(bundle, False, "恢复扫描已取消。")
        except _RecoveryReadLimit as exc:
            return BundleValidation(bundle, False, f"log 读取被限止：{exc}")
        except UnicodeDecodeError as exc:
            return BundleValidation(bundle, False, f"log 不是合法 UTF-8：{exc}")
        except json.JSONDecodeError as exc:
            return BundleValidation(bundle, False, f"log JSONL 无效：{exc}")
        except (OSError, ValueError) as exc:
            return BundleValidation(bundle, False, f"log 文件无法读取：{exc}")
        if first_event != "session_started":
            return BundleValidation(
                bundle,
                False,
                "log 第一条生命周期事件必须是 session_started。",
            )
        if last_record is None or last_record.get("event") != "session_closed":
            return BundleValidation(
                bundle,
                False,
                "log 最后一条生命周期事件必须是 session_closed。",
            )
        if (
            last_record.get("result") != "success"
            or not _is_strict_int(last_record.get("dropped_count"))
            or last_record["dropped_count"] != 0
        ):
            return BundleValidation(
                bundle,
                False,
                "成功 session_closed 的 dropped_count 必须为 0。",
            )
        for field in (
            "sample_count",
            "event_count",
            "receipt_count",
            "queue_high_water",
        ):
            if not _is_strict_int(last_record.get(field)):
                return BundleValidation(
                    bundle,
                    False,
                    f"session_closed {field} 无效。",
                )
        if (
            last_record["sample_count"] != raw_record_count
            or last_record["event_count"] != log_event_count
            or last_record["receipt_count"] != observed_receipt_count
            or last_record["receipt_count"] != manifest["receipt_count"]
            or last_record["queue_high_water"] != manifest["queue_high_water"]
        ):
            return BundleValidation(
                bundle,
                False,
                "session_closed 与 manifest/raw/log 汇总计数不一致。",
                manifest_last_sequence,
            )

        for label, digest, byte_count in (
            ("raw", raw_hash.hexdigest(), raw_bytes),
            ("log", log_hash.hexdigest(), log_bytes),
        ):
            if digest != manifest.get(f"{label}_sha256"):
                return BundleValidation(
                    bundle,
                    False,
                    f"{label} SHA-256 不一致。",
                    manifest_last_sequence,
                )
            if byte_count != manifest[f"{label}_bytes"]:
                return BundleValidation(
                    bundle,
                    False,
                    f"{label} byte count 不一致。",
                    manifest_last_sequence,
                )
        if raw_record_count != manifest["raw_record_count"]:
            return BundleValidation(
                bundle,
                False,
                "raw record count 不一致。",
                manifest_last_sequence,
            )
        if log_event_count != manifest["log_event_count"]:
            return BundleValidation(
                bundle,
                False,
                "log event count 不一致。",
                manifest_last_sequence,
            )
        if manifest["last_session_sequence"] != log_event_count:
            return BundleValidation(
                bundle,
                False,
                "last session sequence 不一致。",
                manifest_last_sequence,
            )
        return BundleValidation(
            bundle,
            True,
            last_sequence=manifest_last_sequence,
        )

    def scan_recovery(
        self,
        output_dir: str | Path,
        *,
        cancel_event: threading.Event | None = None,
    ) -> tuple[RecoveryFinding, ...]:
        root = self._normalize_output(output_dir, require_exists=True)
        if cancel_event is not None and cancel_event.is_set():
            return ()
        candidates: list[tuple[Path, BundleValidation, Path]] = []
        findings: list[RecoveryFinding] = []
        for path in root.iterdir():
            if cancel_event is not None and cancel_event.is_set():
                return ()
            if path.name == "recovery":
                continue
            resolved = path.resolve(strict=False)
            if _is_network_location(path) or _is_network_location(resolved):
                findings.append(
                    RecoveryFinding(
                        original_path=path,
                        reason=(
                            "恢复候选解析到 UNC/网络位置，v1 已拒绝读取或移动；"
                            "请在本地磁盘人工检查该 reparse point。"
                        ),
                        quarantined_path=None,
                    )
                )
                continue
            if not path.is_dir():
                continue
            with self._active_lock:
                if resolved in self._active_staging:
                    continue
            is_staging = path.name.startswith(".") and path.name.endswith(
                ".session.part"
            )
            stem = (
                path.name[1 : -len(".session.part")]
                if is_staging
                else path.name
            )
            manifest = self._session_manifest_identity(
                path / "manifest.json",
                expected_stem=stem,
                cancel_event=cancel_event,
            )
            owner = self._session_owner_identity(
                path / OWNER_MARKER_NAME,
                expected_stem=stem,
                cancel_event=cancel_event,
            )
            with self._active_lock:
                known_orphan = resolved in self._orphan_staging
            if cancel_event is not None and cancel_event.is_set():
                return ()
            if manifest is None and owner is None and not known_orphan:
                continue
            if is_staging:
                validation = BundleValidation(
                    path,
                    False,
                    "发现本程序创建但未完成的 .session.part 工作目录。",
                    self._last_sequence(manifest or {}),
                )
            else:
                validation = self.validate_complete_bundle(
                    path,
                    cancel_event=cancel_event,
                )
                if cancel_event is not None and cancel_event.is_set():
                    return ()
            if not validation.complete:
                candidates.append((path, validation, root))

        maintenance_root = root / "maintenance"
        if maintenance_root.is_dir():
            for path in maintenance_root.iterdir():
                if path.name == "recovery" or not path.is_dir():
                    continue
                resolved = path.resolve(strict=False)
                with self._active_lock:
                    if resolved in self._active_staging:
                        continue
                is_staging = path.name.startswith(".") and path.name.endswith(
                    ".maintenance.part"
                )
                stem = (
                    path.name[1 : -len(".maintenance.part")]
                    if is_staging
                    else path.name
                )
                manifest = self._maintenance_manifest_identity(
                    path / "manifest.json",
                    expected_stem=stem,
                    cancel_event=cancel_event,
                )
                owner = self._maintenance_owner_identity(
                    path / OWNER_MARKER_NAME,
                    expected_stem=stem,
                    cancel_event=cancel_event,
                )
                with self._active_lock:
                    known_orphan = resolved in self._orphan_staging
                if manifest is None and owner is None and not known_orphan:
                    continue
                validation = (
                    BundleValidation(
                        path,
                        False,
                        "发现本程序创建但未完成的 .maintenance.part 工作目录。",
                        self._last_sequence(manifest or {}),
                    )
                    if is_staging
                    else self.validate_maintenance_bundle(path)
                )
                if not validation.complete:
                    candidates.append((path, validation, maintenance_root))

        for path, validation, quarantine_root in candidates:
            if cancel_event is not None and cancel_event.is_set():
                return tuple(findings)
            with self._active_lock:
                if path.resolve(strict=False) in self._active_staging:
                    continue
            quarantined = self._quarantine(quarantine_root, path)
            if quarantined is not None:
                with self._active_lock:
                    self._orphan_staging.discard(
                        path.resolve(strict=False)
                    )
            findings.append(
                RecoveryFinding(
                    original_path=path,
                    reason=validation.reason,
                    quarantined_path=quarantined,
                    last_sequence=validation.last_sequence,
                )
            )
        return tuple(findings)

    def validate_maintenance_bundle(
        self,
        path: str | Path,
    ) -> BundleValidation:
        bundle = Path(path)
        manifest_path = bundle / "manifest.json"
        try:
            manifest = _read_json_limited(manifest_path)
        except Exception as exc:
            return BundleValidation(bundle, False, f"maintenance manifest 无法读取：{exc}")
        if not isinstance(manifest, dict):
            return BundleValidation(bundle, False, "maintenance manifest 根必须是对象。")
        if (
            manifest.get("schema") != "maintenance-v1"
            or manifest.get("status") != "complete"
            or manifest.get("stem") != bundle.name
            or not isinstance(manifest.get("operation_id"), str)
            or not manifest.get("operation_id")
            or not _is_strict_int(manifest.get("operation_generation"), minimum=1)
            or manifest.get("operation_status") != "completed"
            or manifest.get("outcome") not in {"completed", "aborted"}
        ):
            return BundleValidation(bundle, False, "maintenance manifest identity/status 无效。")
        log_name = manifest.get("log_file")
        if log_name != f"{bundle.name}.log":
            return BundleValidation(bundle, False, "maintenance log_file 无效。")
        if any(bundle.glob("*.raw")):
            return BundleValidation(bundle, False, "maintenance bundle 不得包含 raw 文件。")
        for field in (
            "step_count",
            "receipt_count",
            "log_bytes",
            "log_event_count",
            "last_sequence",
            "queue_high_water",
            "dropped_count",
        ):
            if not _is_strict_int(manifest.get(field)):
                return BundleValidation(bundle, False, f"maintenance {field} 无效。")
        fences = manifest.get("producer_fences")
        if (
            not isinstance(fences, dict)
            or set(fences) != {"actuation", "controller", "flow"}
            or any(not _is_strict_int(value) for value in fences.values())
        ):
            return BundleValidation(bundle, False, "maintenance producer_fences 无效。")
        log_path = bundle / log_name
        digest = hashlib.sha256()
        byte_count = 0
        event_count = 0
        receipt_count = 0
        first_event = ""
        last_event = ""
        try:
            with log_path.open("rb") as handle:
                for raw_line in handle:
                    byte_count += len(raw_line)
                    digest.update(raw_line)
                    if len(raw_line) > MAX_STREAM_LINE_BYTES:
                        return BundleValidation(bundle, False, "maintenance log 单行过大。")
                    record = _strict_json_loads(raw_line)
                    if (
                        not isinstance(record, dict)
                        or record.get("schema") != "maintenance-v1.event"
                        or record.get("operation_id") != manifest["operation_id"]
                        or record.get("operation_generation")
                        != manifest["operation_generation"]
                    ):
                        return BundleValidation(bundle, False, "maintenance log identity 无效。")
                    event_count += 1
                    if record.get("operation_sequence") != event_count:
                        return BundleValidation(bundle, False, "maintenance log sequence 不连续。")
                    if event_count == 1:
                        first_event = str(record.get("event", ""))
                    last_event = str(record.get("event", ""))
                    if record.get("record_type") == "receipt":
                        receipt_count += 1
        except Exception as exc:
            return BundleValidation(bundle, False, f"maintenance log 无法验证：{exc}")
        if first_event != "maintenance_started" or last_event != "maintenance_closed":
            return BundleValidation(bundle, False, "maintenance 生命周期事件不完整。")
        if (
            digest.hexdigest() != manifest.get("log_sha256")
            or byte_count != manifest["log_bytes"]
            or event_count != manifest["log_event_count"]
            or event_count != manifest["last_sequence"]
            or receipt_count != manifest["receipt_count"]
            or manifest["dropped_count"] != 0
        ):
            return BundleValidation(bundle, False, "maintenance hash/count/sequence 不一致。")
        return BundleValidation(bundle, True, last_sequence=event_count)

    @staticmethod
    def _session_manifest_identity(
        path: Path,
        *,
        expected_stem: str,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        try:
            manifest = _read_json_limited(
                path,
                cancel_event=cancel_event,
            )
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            _RecoveryReadCancelled,
            _RecoveryReadLimit,
        ):
            return None
        if not isinstance(manifest, dict):
            return None
        generation = manifest.get("session_generation")
        if (
            manifest.get("schema") != "olfactorypilot.session"
            or not _is_strict_int(
                manifest.get("schema_version"),
                minimum=1,
            )
            or manifest["schema_version"] != 1
            or manifest.get("stem") != expected_stem
            or not isinstance(manifest.get("session_id"), str)
            or not manifest.get("session_id")
            or not _is_strict_int(generation, minimum=1)
            or manifest.get("raw_file") != f"{expected_stem}.raw"
            or manifest.get("log_file") != f"{expected_stem}.log"
        ):
            return None
        return manifest

    @staticmethod
    def _maintenance_manifest_identity(
        path: Path,
        *,
        expected_stem: str,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        try:
            manifest = _read_json_limited(path, cancel_event=cancel_event)
        except Exception:
            return None
        if not isinstance(manifest, dict):
            return None
        if (
            manifest.get("schema") != "maintenance-v1"
            or manifest.get("stem") != expected_stem
            or not isinstance(manifest.get("operation_id"), str)
            or not manifest.get("operation_id")
            or not _is_strict_int(manifest.get("operation_generation"), minimum=1)
            or manifest.get("log_file") != f"{expected_stem}.log"
        ):
            return None
        return manifest

    @staticmethod
    def _maintenance_owner_identity(
        path: Path,
        *,
        expected_stem: str,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        try:
            owner = _read_json_limited(path, cancel_event=cancel_event)
        except Exception:
            return None
        if not isinstance(owner, dict):
            return None
        if (
            owner.get("schema") != "olfactorypilot.maintenance-owner"
            or owner.get("schema_version") != 1
            or owner.get("stem") != expected_stem
            or not isinstance(owner.get("operation_id"), str)
            or not owner.get("operation_id")
            or not _is_strict_int(owner.get("operation_generation"), minimum=1)
        ):
            return None
        return owner

    @staticmethod
    def _session_owner_identity(
        path: Path,
        *,
        expected_stem: str,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any] | None:
        try:
            owner = _read_json_limited(
                path,
                cancel_event=cancel_event,
            )
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            _RecoveryReadCancelled,
            _RecoveryReadLimit,
        ):
            return None
        if not isinstance(owner, dict):
            return None
        generation = owner.get("session_generation")
        if (
            owner.get("schema") != "olfactorypilot.session-owner"
            or not _is_strict_int(
                owner.get("schema_version"),
                minimum=1,
            )
            or owner["schema_version"] != 1
            or owner.get("stem") != expected_stem
            or not isinstance(owner.get("session_id"), str)
            or not owner.get("session_id")
            or not _is_strict_int(generation, minimum=1)
        ):
            return None
        return owner

    @staticmethod
    def _has_session_manifest(path: Path) -> bool:
        try:
            manifest = _read_json_limited(path)
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            _RecoveryReadCancelled,
            _RecoveryReadLimit,
        ):
            return False
        return isinstance(manifest, dict) and manifest.get("schema") == (
            "olfactorypilot.session"
        )

    def _fit_path_budget(
        self,
        *,
        root: Path,
        timestamp_text: str,
        subject_original: str,
        subject_clean: str,
        condition_original: str,
        condition_clean: str,
    ) -> tuple[str, str]:
        subject_limit = len(subject_clean)
        condition_limit = len(condition_clean)
        while True:
            subject_candidate = _truncate_component(
                subject_original,
                subject_clean,
                subject_limit,
            )
            condition_candidate = _truncate_component(
                condition_original,
                condition_clean,
                condition_limit,
            )
            stem = (
                f"{timestamp_text}_{subject_candidate}_{condition_candidate}"
                "__999"
            )
            final = root / stem
            staging = root / f".{stem}.session.part"
            paths = (
                final / f"{stem}.raw",
                final / f"{stem}.log",
                staging / f"{stem}.raw",
                staging / f"{stem}.log",
                staging / "manifest.json",
                staging / OWNER_MARKER_NAME,
            )
            if max(utf16_code_units(path) for path in paths) <= WINDOWS_PATH_BUDGET:
                return subject_candidate, condition_candidate
            if subject_limit <= 10 and condition_limit <= 10:
                raise SessionFileError(
                    "输出目录路径过长，无法满足 240 个 UTF-16 code unit 预算；"
                    "请选择更短的本地输出目录。",
                    stage="path_budget",
                    path=root,
                )
            if subject_limit >= condition_limit and subject_limit > 10:
                subject_limit -= 1
            elif condition_limit > 10:
                condition_limit -= 1

    def _create_exclusive(self, path: Path, stage: str, label: str) -> None:
        try:
            self._fault(stage, path)
            with path.open("x", encoding="utf-8", newline="\n"):
                pass
        except Exception as exc:
            raise SessionFileError(
                f"无法独占创建{label}：{exc}。会话未开始，请检查磁盘空间或目录权限。",
                stage=stage,
                path=path,
            ) from exc

    def _create_owner_marker(self, path: Path, payload: dict[str, Any]) -> None:
        try:
            self._fault("create_owner_marker", path)
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.write("\n")
        except Exception as exc:
            try:
                path.unlink(missing_ok=True)
                path.parent.rmdir()
            except OSError:
                pass
            raise SessionFileError(
                f"无法创建会话 ownership 标记：{exc}。会话未开始，请检查磁盘空间或目录权限。",
                stage="create_owner_marker",
                path=path,
            ) from exc

    def _preserve_orphan_identity(
        self,
        staging: Path,
        *,
        session_id: str,
        generation: int,
        stem: str,
        failure_stage: str,
    ) -> None:
        marker = staging / OWNER_MARKER_NAME
        manifest = staging / "manifest.json"
        if marker.is_file() or manifest.is_file():
            return
        try:
            self._create_manifest(
                manifest,
                {
                    "schema": "olfactorypilot.session",
                    "schema_version": 1,
                    "status": "recovery_required",
                    "session_id": session_id,
                    "session_generation": generation,
                    "stem": stem,
                    "raw_file": f"{stem}.raw",
                    "log_file": f"{stem}.log",
                    "failure_stage": failure_stage,
                },
            )
        except SessionFileError:
            with self._active_lock:
                self._orphan_staging.add(staging.resolve(strict=False))

    def _create_manifest(self, path: Path, payload: dict[str, Any]) -> None:
        try:
            self._fault("create_manifest", path)
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
        except Exception as exc:
            raise SessionFileError(
                f"无法独占创建 manifest：{exc}。会话未开始，请检查磁盘空间或目录权限。",
                stage="create_manifest",
                path=path,
            ) from exc

    def _quarantine(self, root: Path, source: Path) -> Path | None:
        recovery = root / "recovery"
        try:
            recovery.mkdir(exist_ok=True)
            raw_stem = source.name
            if raw_stem.startswith(".") and raw_stem.endswith(".session.part"):
                raw_stem = raw_stem[1 : -len(".session.part")]
            timestamp = self._clock().strftime("%Y%m%d-%H%M%S-%f")[:-3]
            base = f"{raw_stem}__incomplete__{timestamp}"
            for index in range(1000):
                suffix = "" if index == 0 else f"__{index:03d}"
                destination = recovery / (base + suffix)
                if destination.exists():
                    continue
                self._fault("recovery_rename", source)
                os.replace(source, destination)
                return destination
        except Exception:
            return None
        return None

    def _fault(self, stage: str, path: Path) -> None:
        if self._fault_injector is not None:
            self._fault_injector(stage, path)

    @staticmethod
    def _split_configured_target(target: str) -> tuple[str | None, str]:
        if "/" in target:
            device, line = target.split("/", 1)
            return device or None, line
        return None, target

    @staticmethod
    def _normalize_output(output_dir: str | Path, *, require_exists: bool) -> Path:
        raw = str(output_dir)
        if raw.startswith("\\\\") or raw.startswith("//"):
            raise SessionFileError(
                "v1 仅支持本地输出目录，不支持 UNC/网络路径。",
                stage="output_validation",
                path=Path(output_dir),
            )
        path = Path(output_dir).expanduser().resolve(strict=False)
        if _is_network_location(path):
            raise SessionFileError(
                "v1 仅支持本地输出目录，不支持映射网络盘或网络位置。",
                stage="output_validation",
                path=path,
            )
        if require_exists and not path.exists():
            raise SessionFileError(
                "输出目录不存在，请先选择已存在的本地目录。",
                stage="output_validation",
                path=path,
            )
        if path.exists() and not path.is_dir():
            raise SessionFileError(
                "所选输出路径不是目录，请重新选择。",
                stage="output_validation",
                path=path,
            )
        return path

    @staticmethod
    def _last_sequence(manifest: dict[str, Any]) -> int | None:
        try:
            value = manifest.get("last_session_sequence")
            return None if value is None else int(value)
        except (TypeError, ValueError):
            return None
