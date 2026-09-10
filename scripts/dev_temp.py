from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

MARKER_NAME = ".devtmp-owner.json"
MARKER_SCHEMA = 1
MARKER_PRODUCER = "OlfactoryPilot"
SESSION_ENV = "OLFACTORYPILOT_DEVTEMP_SESSION"
TOKEN_ENV = "OLFACTORYPILOT_DEVTEMP_TOKEN"
CURRENT_SESSION_ENV = "OLFACTORYPILOT_CURRENT_DEVTEMP_SESSION"
CURRENT_TOKEN_ENV = "OLFACTORYPILOT_CURRENT_DEVTEMP_TOKEN"
_PURPOSE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_RUN_PATTERN = re.compile(r"run-[0-9a-f]{32}")
_IDENTITY_PATTERN = re.compile(
    r"(?:windows-filetime|proc-start-ticks):[1-9][0-9]*"
)
_DELETING_PATTERN = re.compile(r"\.deleting-(run-[0-9a-f]{32})\.json")


class DevTempError(RuntimeError):
    """开发临时目录不满足所有权合同时抛出的错误。"""


class ProcessMissingError(ProcessLookupError):
    """目标 PID 已不存在。"""


class ProcessIdentityUnknownError(OSError):
    """无法可靠读取目标进程的启动 identity。"""


class OwnerState(StrEnum):
    ACTIVE = "active"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Session:
    path: Path
    purpose: str
    run: str
    token: str

    @property
    def basetemp(self) -> Path:
        return self.path / "basetemp"


@dataclass(frozen=True)
class CleanupEvent:
    path: Path
    action: str
    detail: str


IdentityReader = Callable[[int], str]


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _same_path(left: Path | str, right: Path | str) -> bool:
    return _path_key(left) == _path_key(right)


def _reject_redirected_path(path: Path, label: str) -> None:
    if _path_key(path) != _path_key(path.resolve()):
        raise DevTempError(f"{label} 经过 junction/链接重定向，已保留：{path}")


def _validate_process_identity(identity: str) -> str:
    if not isinstance(identity, str) or not _IDENTITY_PATTERN.fullmatch(identity):
        raise ProcessIdentityUnknownError(f"进程启动 identity 格式无效：{identity!r}")
    expected_prefix = "windows-filetime:" if os.name == "nt" else "proc-start-ticks:"
    if not identity.startswith(expected_prefix):
        raise ProcessIdentityUnknownError(
            f"进程启动 identity 与当前平台不匹配：{identity!r}"
        )
    return identity


@contextmanager
def _lifecycle_lock(project_root: Path):
    """用项目目录上的 OS lock 串行化全部 lifecycle mutation。"""
    project = Path(os.path.abspath(project_root))
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        digest = hashlib.sha256(_path_key(project).encode("utf-8")).hexdigest()
        handle = kernel32.CreateMutexW(None, False, f"Local\\OlfactoryPilot-{digest}")
        if not handle:
            raise OSError(ctypes.get_last_error(), "无法创建 dev-temp lifecycle mutex")
        wait_result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        if wait_result not in {0, 0x80}:
            kernel32.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), "无法取得 dev-temp lifecycle mutex")
        try:
            yield
        finally:
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        return

    import fcntl

    descriptor = os.open(project, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _managed_root(project_root: Path) -> Path:
    project = Path(os.path.abspath(project_root))
    root = project / ".devtmp"
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise DevTempError(f"受管临时根不是普通目录，已保留：{root}")
    if root.exists():
        _reject_redirected_path(root, "受管临时根")
    root.mkdir(exist_ok=True)
    _reject_redirected_path(root, "受管临时根")
    return root


def _project_from_session_path(session_path: Path) -> Path:
    session_path = Path(os.path.abspath(session_path))
    try:
        project = session_path.parents[2]
    except IndexError as error:
        raise DevTempError(f"session 路径层级不足：{session_path}") from error
    if session_path.parent.parent.name != ".devtmp":
        raise DevTempError(f"session 不在 .devtmp/<purpose>/ 下：{session_path}")
    _reject_redirected_path(session_path.parent.parent, "受管临时根")
    _reject_redirected_path(session_path.parent, "用途路径")
    _reject_redirected_path(session_path, "session 路径")
    return project


def _windows_process_start_identity(pid: int) -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    process_query_limited_information = 0x1000
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error in {87, 1168}:
            raise ProcessMissingError(pid)
        raise ProcessIdentityUnknownError(error, f"无法打开 PID {pid}")
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            error = ctypes.get_last_error()
            raise ProcessIdentityUnknownError(error, f"无法读取 PID {pid} 的启动时间")
        value = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return f"windows-filetime:{value}"
    finally:
        kernel32.CloseHandle(handle)


def _proc_process_start_identity(pid: int) -> str:
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        stat = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ProcessMissingError(pid) from error
    except OSError as error:
        raise ProcessIdentityUnknownError(str(error)) from error
    closing_paren = stat.rfind(")")
    fields_after_name = stat[closing_paren + 2 :].split()
    if closing_paren < 0 or len(fields_after_name) < 20:
        raise ProcessIdentityUnknownError(f"PID {pid} 的 /proc stat 无法解析")
    return f"proc-start-ticks:{fields_after_name[19]}"


def _read_process_start_identity(pid: int) -> str:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ProcessMissingError(pid)
    if os.name == "nt":
        return _validate_process_identity(_windows_process_start_identity(pid))
    if Path("/proc").is_dir():
        return _validate_process_identity(_proc_process_start_identity(pid))
    try:
        os.kill(pid, 0)
    except ProcessLookupError as error:
        raise ProcessMissingError(pid) from error
    except PermissionError as error:
        raise ProcessIdentityUnknownError(str(error)) from error
    raise ProcessIdentityUnknownError("当前平台无法读取进程启动 identity")


def _atomic_write_marker(session_path: Path, marker: dict[str, Any]) -> None:
    temporary = session_path / f".{MARKER_NAME}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(
        json.dumps(marker, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, session_path / MARKER_NAME)


def _load_valid_marker(session_path: Path, project_root: Path) -> dict[str, Any]:
    project = Path(os.path.abspath(project_root))
    root = project / ".devtmp"
    session_path = Path(os.path.abspath(session_path))
    _reject_redirected_path(root, "受管临时根")
    _reject_redirected_path(session_path.parent, "用途路径")
    _reject_redirected_path(session_path, "session 路径")
    try:
        relative = session_path.relative_to(root)
    except ValueError as error:
        raise DevTempError(f"session 不在受管临时根内：{session_path}") from error
    if len(relative.parts) != 2:
        raise DevTempError(f"session 层级无效，已保留：{session_path}")
    purpose, run = relative.parts
    if not _PURPOSE_PATTERN.fullmatch(purpose) or not _RUN_PATTERN.fullmatch(run):
        raise DevTempError(f"session 名称无效，已保留：{session_path}")
    marker_path = session_path / MARKER_NAME
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DevTempError(f"marker 无法读取，已保留：{session_path}") from error
    if not isinstance(marker, dict):
        raise DevTempError(f"marker 不是对象，已保留：{session_path}")
    required_types = {
        "schema": int,
        "producer": str,
        "project": str,
        "purpose": str,
        "run": str,
        "created_at": str,
        "pid": int,
        "process_start_identity": str,
        "token": str,
    }
    for field, expected_type in required_types.items():
        value = marker.get(field)
        if not isinstance(value, expected_type) or isinstance(value, bool):
            raise DevTempError(f"marker 字段 {field} 无效，已保留：{session_path}")
    if marker["schema"] != MARKER_SCHEMA:
        raise DevTempError(f"marker schema 未知，已保留：{session_path}")
    if marker["producer"] != MARKER_PRODUCER:
        raise DevTempError(f"marker producer 不匹配，已保留：{session_path}")
    if not _same_path(marker["project"], project):
        raise DevTempError(f"marker 项目不匹配，已保留：{session_path}")
    if marker["purpose"] != purpose or marker["run"] != run:
        raise DevTempError(f"marker 路径 identity 不匹配，已保留：{session_path}")
    if marker["pid"] <= 0 or not marker["token"]:
        raise DevTempError(f"marker ownership 无效，已保留：{session_path}")
    try:
        _validate_process_identity(marker["process_start_identity"])
    except ProcessIdentityUnknownError as error:
        raise DevTempError(f"marker ownership 无效，已保留：{session_path}") from error
    try:
        datetime.fromisoformat(marker["created_at"].replace("Z", "+00:00"))
    except ValueError as error:
        raise DevTempError(f"marker 时间无效，已保留：{session_path}") from error
    return marker


def owner_state(
    marker: dict[str, Any],
    *,
    identity_reader: IdentityReader = _read_process_start_identity,
) -> OwnerState:
    try:
        expected_identity = _validate_process_identity(
            marker["process_start_identity"]
        )
    except (KeyError, ProcessIdentityUnknownError):
        return OwnerState.UNKNOWN
    try:
        current_identity = _validate_process_identity(identity_reader(marker["pid"]))
    except ProcessMissingError:
        return OwnerState.DEAD
    except (OSError, RuntimeError):
        return OwnerState.UNKNOWN
    if current_identity == expected_identity:
        return OwnerState.ACTIVE
    return OwnerState.DEAD


def _deleting_marker_path(session_path: Path) -> Path:
    return session_path.parent / f".deleting-{session_path.name}.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load_valid_deleting_marker(
    marker_path: Path,
    project_root: Path,
) -> tuple[dict[str, Any], Path]:
    match = _DELETING_PATTERN.fullmatch(marker_path.name)
    if match is None:
        raise DevTempError(f"deleting marker 名称无效，已保留：{marker_path}")
    purpose_path = marker_path.parent
    session_path = purpose_path / match.group(1)
    _reject_redirected_path(project_root / ".devtmp", "受管临时根")
    _reject_redirected_path(purpose_path, "用途路径")
    if session_path.exists():
        _reject_redirected_path(session_path, "session 路径")
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DevTempError(f"deleting marker 无法读取，已保留：{marker_path}") from error
    required = {
        "schema": int,
        "producer": str,
        "project": str,
        "purpose": str,
        "run": str,
        "pid": int,
        "process_start_identity": str,
        "token": str,
        "state": str,
        "deletion_started_at": str,
    }
    if not isinstance(payload, dict):
        raise DevTempError(f"deleting marker 格式无效，已保留：{marker_path}")
    for field, expected in required.items():
        value = payload.get(field)
        if not isinstance(value, expected) or isinstance(value, bool):
            raise DevTempError(
                f"deleting marker 字段 {field} 无效，已保留：{marker_path}"
            )
    if (
        payload["schema"] != MARKER_SCHEMA
        or payload["producer"] != MARKER_PRODUCER
        or payload["state"] != "deleting"
        or not _same_path(payload["project"], project_root)
        or payload["purpose"] != purpose_path.name
        or payload["run"] != session_path.name
        or payload["pid"] <= 0
        or not payload["token"]
    ):
        raise DevTempError(f"deleting marker identity 无效，已保留：{marker_path}")
    try:
        _validate_process_identity(payload["process_start_identity"])
        datetime.fromisoformat(payload["deletion_started_at"].replace("Z", "+00:00"))
    except (ProcessIdentityUnknownError, ValueError) as error:
        raise DevTempError(f"deleting marker ownership 无效，已保留：{marker_path}") from error
    return payload, session_path


def _begin_recoverable_deletion(
    session_path: Path,
    marker: dict[str, Any],
    *,
    deleter_pid: int,
    deleter_identity: str,
) -> Path:
    deletion_marker = _deleting_marker_path(session_path)
    payload = dict(marker)
    payload.update(
        {
            "pid": deleter_pid,
            "process_start_identity": _validate_process_identity(deleter_identity),
            "state": "deleting",
            "deletion_started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    )
    _atomic_write_json(deletion_marker, payload)
    return deletion_marker


def _remove_owned_session(session_path: Path, project_root: Path) -> None:
    deletion_path = session_path
    if os.name == "nt":
        deletion_path = Path(f"\\\\?\\{session_path.resolve()}")
    for attempt in range(6):
        try:
            shutil.rmtree(deletion_path, onerror=_retry_remove_readonly)
            break
        except FileNotFoundError:
            break
        except OSError as error:
            directory_not_empty = error.errno == errno.ENOTEMPTY or getattr(
                error, "winerror", None
            ) == 145
            if not directory_not_empty or attempt == 5:
                raise
            time.sleep(0.05)
    _remove_empty_parents(session_path.parent, project_root)


def _remove_empty_parents(purpose_dir: Path, project_root: Path) -> None:
    root = Path(os.path.abspath(project_root)) / ".devtmp"
    for directory in (purpose_dir, root):
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            pass


def _retry_remove_readonly(function, path: str, error_info) -> None:
    error = error_info[1]
    if isinstance(error, FileNotFoundError):
        return
    if not isinstance(error, PermissionError):
        raise error
    try:
        mode = os.stat(path, follow_symlinks=False).st_mode
        os.chmod(path, mode | 0o200)
        function(path)
    except FileNotFoundError:
        return


def _delete_session_locked(
    session_path: Path,
    marker: dict[str, Any],
    project: Path,
    *,
    deleter_pid: int,
    deleter_identity: str,
) -> None:
    deletion_marker = _begin_recoverable_deletion(
        session_path,
        marker,
        deleter_pid=deleter_pid,
        deleter_identity=deleter_identity,
    )
    _remove_owned_session(session_path, project)
    deletion_marker.unlink()
    _remove_empty_parents(session_path.parent, project)


def _cleanup_stale_sessions_locked(
    project: Path,
    *,
    identity_reader: IdentityReader,
) -> tuple[CleanupEvent, ...]:
    root = project / ".devtmp"
    if not root.exists():
        return ()
    try:
        if root.is_symlink() or not root.is_dir():
            return (CleanupEvent(root, "preserved", "受管临时根不是普通目录"),)
        _reject_redirected_path(root, "受管临时根")
        purpose_paths = sorted(root.iterdir(), key=lambda item: item.name)
    except (DevTempError, OSError) as error:
        return (CleanupEvent(root, "preserved", str(error)),)

    events: list[CleanupEvent] = []
    for purpose_path in purpose_paths:
        try:
            if purpose_path.is_symlink() or not purpose_path.is_dir():
                events.append(CleanupEvent(purpose_path, "preserved", "未知根项目"))
                continue
            _reject_redirected_path(purpose_path, "用途路径")
            if not _PURPOSE_PATTERN.fullmatch(purpose_path.name):
                events.append(CleanupEvent(purpose_path, "preserved", "未知用途目录"))
                continue
            items = sorted(purpose_path.iterdir(), key=lambda item: item.name)
        except (DevTempError, OSError) as error:
            events.append(CleanupEvent(purpose_path, "preserved", str(error)))
            continue

        deleting_names = {
            item.name for item in items if _DELETING_PATTERN.fullmatch(item.name)
        }
        deleting_runs = {
            match.group(1)
            for item in items
            if (match := _DELETING_PATTERN.fullmatch(item.name)) is not None
        }
        for deletion_marker in (
            item for item in items if item.name in deleting_names
        ):
            try:
                marker, session_path = _load_valid_deleting_marker(
                    deletion_marker,
                    project,
                )
                state = owner_state(marker, identity_reader=identity_reader)
                if state is not OwnerState.DEAD:
                    events.append(
                        CleanupEvent(
                            deletion_marker,
                            "preserved",
                            f"deleting owner {state.value}",
                        )
                    )
                    continue
                deleter_pid = os.getpid()
                deleter_identity = _validate_process_identity(
                    identity_reader(deleter_pid)
                )
                marker.update(
                    {
                        "pid": deleter_pid,
                        "process_start_identity": deleter_identity,
                        "deletion_started_at": datetime.now(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                    }
                )
                _atomic_write_json(deletion_marker, marker)
                if session_path.exists():
                    _remove_owned_session(session_path, project)
                deletion_marker.unlink()
                _remove_empty_parents(purpose_path, project)
                events.append(
                    CleanupEvent(session_path, "removed", "恢复 owner 已失活的 partial deletion")
                )
            except (DevTempError, OSError) as error:
                events.append(CleanupEvent(deletion_marker, "preserved", str(error)))

        for session_path in items:
            if session_path.name in deleting_names or not session_path.exists():
                continue
            if session_path.name in deleting_runs:
                events.append(
                    CleanupEvent(
                        session_path,
                        "preserved",
                        "存在 deleting ownership marker",
                    )
                )
                continue
            try:
                if session_path.is_symlink() or not session_path.is_dir():
                    events.append(CleanupEvent(session_path, "preserved", "未知用途项目"))
                    continue
                marker = _load_valid_marker(session_path, project)
                state = owner_state(marker, identity_reader=identity_reader)
                if state is OwnerState.DEAD:
                    deleter_pid = os.getpid()
                    deleter_identity = _validate_process_identity(
                        identity_reader(deleter_pid)
                    )
                    _delete_session_locked(
                        session_path,
                        marker,
                        project,
                        deleter_pid=deleter_pid,
                        deleter_identity=deleter_identity,
                    )
                    events.append(CleanupEvent(session_path, "removed", "owner 已失活"))
                else:
                    events.append(
                        CleanupEvent(session_path, "preserved", f"owner {state.value}")
                    )
            except (DevTempError, OSError) as error:
                events.append(CleanupEvent(session_path, "preserved", str(error)))
    try:
        root.rmdir()
    except (FileNotFoundError, OSError):
        pass
    return tuple(events)


def cleanup_stale_sessions(
    project_root: Path | str | None = None,
    *,
    identity_reader: IdentityReader = _read_process_start_identity,
) -> tuple[CleanupEvent, ...]:
    project = Path(os.path.abspath(project_root or repository_root()))
    with _lifecycle_lock(project):
        return _cleanup_stale_sessions_locked(project, identity_reader=identity_reader)


def create_session(
    purpose: str,
    project_root: Path | str | None = None,
    *,
    owner_pid: int | None = None,
    identity_reader: IdentityReader = _read_process_start_identity,
) -> Session:
    if not _PURPOSE_PATTERN.fullmatch(purpose):
        raise DevTempError(f"用途名称无效：{purpose!r}")
    try:
        pid = owner_pid if owner_pid is not None else os.getpid()
        identity = _validate_process_identity(identity_reader(pid))
    except (ProcessMissingError, ProcessIdentityUnknownError) as error:
        raise DevTempError(f"无法建立 PID {pid} 的可靠 ownership") from error
    project = Path(os.path.abspath(project_root or repository_root()))
    with _lifecycle_lock(project):
        _emit_cleanup_events(
            _cleanup_stale_sessions_locked(project, identity_reader=identity_reader)
        )
        root = _managed_root(project)
        purpose_dir = root / purpose
        if purpose_dir.exists():
            if purpose_dir.is_symlink() or not purpose_dir.is_dir():
                raise DevTempError(f"用途路径不是普通目录，已保留：{purpose_dir}")
            _reject_redirected_path(purpose_dir, "用途路径")
        purpose_dir.mkdir(exist_ok=True)
        _reject_redirected_path(purpose_dir, "用途路径")
        run = f"run-{uuid.uuid4().hex}"
        session_path = purpose_dir / run
        session_path.mkdir()
        _reject_redirected_path(session_path, "session 路径")
        token = uuid.uuid4().hex
        marker = {
            "schema": MARKER_SCHEMA,
            "producer": MARKER_PRODUCER,
            "project": str(project),
            "purpose": purpose,
            "run": run,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "pid": pid,
            "process_start_identity": identity,
            "token": token,
        }
        try:
            _atomic_write_marker(session_path, marker)
        except OSError as error:
            raise DevTempError(
                f"marker 写入失败，missing-marker session 已保留：{session_path}"
            ) from error
        return Session(session_path, purpose, run, token)


def claim_session(
    session_path: Path | str,
    token: str,
    *,
    owner_pid: int | None = None,
    identity_reader: IdentityReader = _read_process_start_identity,
) -> Session:
    path = Path(os.path.abspath(session_path))
    project = _project_from_session_path(path)
    with _lifecycle_lock(project):
        if _deleting_marker_path(path).exists():
            raise DevTempError(f"session 正处于可恢复删除状态，已保留：{path}")
        marker = _load_valid_marker(path, project)
        if marker["token"] != token:
            raise DevTempError(f"session token 不匹配，已保留：{path}")
        pid = owner_pid if owner_pid is not None else os.getpid()
        try:
            identity = _validate_process_identity(identity_reader(pid))
        except (ProcessMissingError, ProcessIdentityUnknownError) as error:
            raise DevTempError(f"无法建立 PID {pid} 的可靠 ownership") from error
        marker["pid"] = pid
        marker["process_start_identity"] = identity
        marker["claimed_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        _atomic_write_marker(path, marker)
        return Session(path, marker["purpose"], marker["run"], token)


def cleanup_session(
    session_path: Path | str,
    token: str,
    *,
    owner_pid: int | None = None,
    identity_reader: IdentityReader = _read_process_start_identity,
) -> bool:
    path = Path(os.path.abspath(session_path))
    if not path.exists():
        return False
    project = _project_from_session_path(path)
    requester_pid = owner_pid if owner_pid is not None else os.getpid()
    try:
        requester_identity = _validate_process_identity(identity_reader(requester_pid))
    except (ProcessMissingError, ProcessIdentityUnknownError) as error:
        raise DevTempError(f"无法验证 cleanup 请求者 PID {requester_pid}") from error
    with _lifecycle_lock(project):
        if not path.exists():
            return False
        if _deleting_marker_path(path).exists():
            raise DevTempError(f"session 正处于可恢复删除状态，已保留：{path}")
        marker = _load_valid_marker(path, project)
        if marker["token"] != token:
            raise DevTempError(f"session token 不匹配，已保留：{path}")
        requester_is_owner = (
            marker["pid"] == requester_pid
            and marker["process_start_identity"] == requester_identity
        )
        state = owner_state(marker, identity_reader=identity_reader)
        if not requester_is_owner and state is not OwnerState.DEAD:
            raise DevTempError(f"session owner {state.value}，已保留：{path}")
        _delete_session_locked(
            path,
            marker,
            project,
            deleter_pid=requester_pid,
            deleter_identity=requester_identity,
        )
        return True


def _is_active_clean_clone_worktree(
    project: Path,
    owning_project: Path,
    *,
    identity_reader: IdentityReader,
) -> bool:
    """Allow a clean clone to consume a short sibling pytest session safely."""
    clean_clone_root = owning_project / ".devtmp" / "clean-clone"
    try:
        relative = project.relative_to(clean_clone_root)
    except ValueError:
        return False
    if (
        len(relative.parts) != 2
        or not _RUN_PATTERN.fullmatch(relative.parts[0])
        or relative.parts[1] != "worktree"
    ):
        return False
    outer_session = clean_clone_root / relative.parts[0]
    try:
        _reject_redirected_path(project, "clean-clone worktree")
        marker = _load_valid_marker(outer_session, owning_project)
    except (DevTempError, OSError):
        return False
    return (
        marker["purpose"] == "clean-clone"
        and owner_state(marker, identity_reader=identity_reader) is OwnerState.ACTIVE
    )


def claim_session_from_environment(
    project_root: Path | str | None = None,
    *,
    identity_reader: IdentityReader = _read_process_start_identity,
) -> Session | None:
    path = os.environ.get(SESSION_ENV)
    token = os.environ.get(TOKEN_ENV)
    if path is None and token is None:
        return None
    if not path or not token:
        raise DevTempError("开发临时 session 环境变量不完整")
    project = Path(os.path.abspath(project_root or repository_root()))
    session_path = Path(os.path.abspath(path))
    owning_project = _project_from_session_path(session_path)
    if not _same_path(owning_project, project) and not _is_active_clean_clone_worktree(
        project,
        owning_project,
        identity_reader=identity_reader,
    ):
        raise DevTempError(f"开发临时 session 不属于当前项目：{session_path}")
    session = claim_session(
        session_path,
        token,
        identity_reader=identity_reader,
    )
    return session


def path_is_in_current_session(
    candidate: Path | str,
    project_root: Path | str | None = None,
    *,
    identity_reader: IdentityReader = _read_process_start_identity,
) -> bool:
    session_value = os.environ.get(CURRENT_SESSION_ENV)
    token = os.environ.get(CURRENT_TOKEN_ENV)
    if not session_value or not token:
        return False
    session_path = Path(os.path.abspath(session_value))
    project = Path(os.path.abspath(project_root or repository_root()))
    try:
        owning_project = _project_from_session_path(session_path)
        if not _same_path(owning_project, project) and not _is_active_clean_clone_worktree(
            project,
            owning_project,
            identity_reader=identity_reader,
        ):
            return False
        Path(candidate).resolve().relative_to(session_path)
        marker = _load_valid_marker(session_path, owning_project)
    except (DevTempError, OSError, ValueError):
        return False
    return (
        marker["token"] == token
        and owner_state(marker, identity_reader=identity_reader) is OwnerState.ACTIVE
    )


def _emit_cleanup_events(events: tuple[CleanupEvent, ...]) -> None:
    for event in events:
        print(
            f"dev-temp {event.action}: {event.path} ({event.detail})",
            file=sys.stderr,
        )


def _session_payload(session: Session) -> str:
    return json.dumps(
        {
            "path": str(session.path),
            "purpose": session.purpose,
            "run": session.run,
            "token": session.token,
            "basetemp": str(session.basetemp),
        },
        ensure_ascii=False,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OlfactoryPilot 开发临时目录管理")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="创建 owned session")
    create.add_argument("--purpose", required=True)
    create.add_argument("--project-root", type=Path, default=repository_root())
    create.add_argument("--owner-pid", type=int)

    claim = subparsers.add_parser("claim", help="原子认领 existing session")
    claim.add_argument("--session", type=Path, required=True)
    claim.add_argument("--token", required=True)
    claim.add_argument("--owner-pid", type=int)

    cleanup = subparsers.add_parser("cleanup", help="清理当前 owned/dead session")
    cleanup.add_argument("--session", type=Path, required=True)
    cleanup.add_argument("--token", required=True)
    cleanup.add_argument("--owner-pid", type=int)

    stale = subparsers.add_parser("cleanup-stale", help="清理可证 owner dead 的 session")
    stale.add_argument("--project-root", type=Path, default=repository_root())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "create":
            session = create_session(
                args.purpose,
                args.project_root,
                owner_pid=args.owner_pid,
            )
            print(_session_payload(session))
        elif args.command == "claim":
            session = claim_session(
                args.session,
                args.token,
                owner_pid=args.owner_pid,
            )
            print(_session_payload(session))
        elif args.command == "cleanup":
            cleanup_session(
                args.session,
                args.token,
                owner_pid=args.owner_pid,
            )
        else:
            _emit_cleanup_events(cleanup_stale_sessions(args.project_root))
    except (DevTempError, OSError) as error:
        print(f"dev-temp error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
