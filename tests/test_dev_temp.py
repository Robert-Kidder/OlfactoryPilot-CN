from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from scripts.dev_temp import (
    MARKER_NAME,
    MARKER_PRODUCER,
    DevTempError,
    OwnerState,
    ProcessIdentityUnknownError,
    ProcessMissingError,
    claim_session,
    claim_session_from_environment,
    cleanup_session,
    cleanup_stale_sessions,
    create_session,
    owner_state,
    path_is_in_current_session,
)


def _identity(pid: int) -> str:
    prefix = "windows-filetime" if os.name == "nt" else "proc-start-ticks"
    return f"{prefix}:{pid}"


class IdentityRegistry:
    def __init__(self) -> None:
        self.values: dict[int, str | BaseException] = {}

    def __call__(self, pid: int) -> str:
        value = self.values.get(pid, _identity(pid))
        if isinstance(value, BaseException):
            raise value
        return value


def _marker(path: Path) -> dict:
    return json.loads((path / MARKER_NAME).read_text(encoding="utf-8"))


def test_session_marker_records_required_identity_and_cleans_exact_owner(
    tmp_path: Path,
) -> None:
    identities = IdentityRegistry()
    identities.values[101] = _identity(101)
    session = create_session(
        "pytest",
        tmp_path,
        owner_pid=101,
        identity_reader=identities,
    )

    marker = _marker(session.path)
    assert session.path.parent.parent == tmp_path / ".devtmp"
    assert marker["producer"] == MARKER_PRODUCER
    assert marker["project"] == str(tmp_path.resolve())
    assert marker["purpose"] == "pytest"
    assert marker["run"] == session.run
    assert marker["pid"] == 101
    assert marker["process_start_identity"] == _identity(101)
    assert marker["created_at"].endswith("Z")
    assert marker["token"] == session.token
    assert owner_state(marker, identity_reader=identities) is OwnerState.ACTIVE

    assert cleanup_session(
        session.path,
        session.token,
        owner_pid=101,
        identity_reader=identities,
    )
    assert not (tmp_path / ".devtmp").exists()


def test_claim_updates_owner_atomically_and_rejects_wrong_token(tmp_path: Path) -> None:
    identities = IdentityRegistry()
    identities.values.update(
        {101: _identity(101), 202: _identity(202)}
    )
    session = create_session(
        "pytest",
        tmp_path,
        owner_pid=101,
        identity_reader=identities,
    )

    with pytest.raises(DevTempError, match="token 不匹配"):
        claim_session(
            session.path,
            "wrong-token",
            owner_pid=202,
            identity_reader=identities,
        )
    claimed = claim_session(
        session.path,
        session.token,
        owner_pid=202,
        identity_reader=identities,
    )

    marker = _marker(claimed.path)
    assert marker["pid"] == 202
    assert marker["process_start_identity"] == _identity(202)
    assert "claimed_at" in marker
    assert not (claimed.path / ".claim.lock").exists()
    assert cleanup_session(
        claimed.path,
        claimed.token,
        owner_pid=202,
        identity_reader=identities,
    )


def test_stale_cleanup_removes_only_dead_or_pid_reused_owners(tmp_path: Path) -> None:
    identities = IdentityRegistry()
    identities.values.update(
        {
            101: _identity(101),
            202: _identity(202),
            303: _identity(303),
        }
    )
    dead = create_session(
        "pytest",
        tmp_path,
        owner_pid=101,
        identity_reader=identities,
    )
    active = create_session(
        "pytest",
        tmp_path,
        owner_pid=202,
        identity_reader=identities,
    )
    unknown = create_session(
        "clean-clone",
        tmp_path,
        owner_pid=303,
        identity_reader=identities,
    )
    invalid = tmp_path / ".devtmp" / "pytest" / "run-not-owned"
    invalid.mkdir(parents=True)
    (invalid / "user-data.txt").write_text("preserve\n", encoding="utf-8")

    identities.values[101] = _identity(999)
    identities.values[303] = ProcessIdentityUnknownError("access denied")
    events = cleanup_stale_sessions(tmp_path, identity_reader=identities)

    assert not dead.path.exists()
    assert active.path.exists()
    assert unknown.path.exists()
    assert invalid.exists()
    assert any(event.path == dead.path and event.action == "removed" for event in events)
    assert any(
        event.path == active.path and event.detail == "owner active" for event in events
    )
    assert any(
        event.path == unknown.path and event.detail == "owner unknown"
        for event in events
    )
    assert any(event.path == invalid and event.action == "preserved" for event in events)

    identities.values[202] = ProcessMissingError(202)
    identities.values[303] = ProcessMissingError(303)
    cleanup_stale_sessions(tmp_path, identity_reader=identities)
    assert not active.path.exists()
    assert not unknown.path.exists()
    assert invalid.exists()


def test_cleanup_preserves_active_foreign_owner_and_unknown_marker(
    tmp_path: Path,
) -> None:
    identities = IdentityRegistry()
    identities.values.update(
        {101: _identity(101), 202: _identity(202)}
    )
    session = create_session(
        "ui-capture",
        tmp_path,
        owner_pid=101,
        identity_reader=identities,
    )

    with pytest.raises(DevTempError, match="owner active"):
        cleanup_session(
            session.path,
            session.token,
            owner_pid=202,
            identity_reader=identities,
        )
    assert session.path.exists()

    marker_path = session.path / MARKER_NAME
    marker_path.write_text("not-json\n", encoding="utf-8")
    events = cleanup_stale_sessions(tmp_path, identity_reader=identities)
    assert session.path.exists()
    assert any(event.path == session.path and event.action == "preserved" for event in events)


def test_marker_write_failure_preserves_missing_marker_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import dev_temp

    identities = IdentityRegistry()
    identities.values[101] = _identity(101)

    def fail_marker_write(_session_path: Path, _marker: dict) -> None:
        raise OSError("simulated marker write failure")

    monkeypatch.setattr(dev_temp, "_atomic_write_marker", fail_marker_write)

    with pytest.raises(DevTempError, match="missing-marker session 已保留"):
        create_session(
            "pytest",
            tmp_path,
            owner_pid=101,
            identity_reader=identities,
        )

    sessions = list((tmp_path / ".devtmp" / "pytest").glob("run-*"))
    assert len(sessions) == 1
    assert sessions[0].is_dir()
    assert not (sessions[0] / MARKER_NAME).exists()
    events = cleanup_stale_sessions(tmp_path, identity_reader=identities)
    assert sessions[0].exists()
    assert any(event.path == sessions[0] and event.action == "preserved" for event in events)


def test_create_rejects_symlinked_purpose_directory(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    purpose = tmp_path / ".devtmp" / "pytest"
    purpose.parent.mkdir()
    try:
        purpose.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("当前宿主不允许创建目录符号链接")

    with pytest.raises(DevTempError, match="用途路径不是普通目录"):
        create_session("pytest", tmp_path)

    assert not list(target.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="junction 仅在 Windows 上验证")
def test_create_rejects_junctioned_managed_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = tmp_path / ".devtmp"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("当前宿主不允许创建目录 junction")

    with pytest.raises(DevTempError, match="junction/链接重定向"):
        create_session("pytest", tmp_path)
    assert not list(outside.iterdir())


def test_identity_failure_precedes_directory_creation(tmp_path: Path) -> None:
    identities = IdentityRegistry()
    identities.values[101] = ProcessIdentityUnknownError("access denied")

    with pytest.raises(DevTempError, match="可靠 ownership"):
        create_session(
            "pytest", tmp_path, owner_pid=101, identity_reader=identities
        )

    assert not (tmp_path / ".devtmp").exists()


def test_invalid_marker_identity_is_preserved_and_cannot_authorize_deletion(
    tmp_path: Path,
) -> None:
    identities = IdentityRegistry()
    identities.values[101] = _identity(101)
    session = create_session(
        "pytest", tmp_path, owner_pid=101, identity_reader=identities
    )
    marker = _marker(session.path)
    marker["process_start_identity"] = "arbitrary-nonempty-value"
    (session.path / MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
    identities.values[101] = ProcessMissingError(101)

    events = cleanup_stale_sessions(tmp_path, identity_reader=identities)

    assert session.path.exists()
    assert any(event.path == session.path and event.action == "preserved" for event in events)


def test_stale_cleanup_reports_one_oserror_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import dev_temp

    identities = IdentityRegistry()
    identities.values.update(
        {101: _identity(101), 202: _identity(202)}
    )
    first = create_session(
        "pytest", tmp_path, owner_pid=101, identity_reader=identities
    )
    second = create_session(
        "pytest", tmp_path, owner_pid=202, identity_reader=identities
    )
    identities.values[101] = ProcessMissingError(101)
    identities.values[202] = ProcessMissingError(202)
    original = dev_temp._remove_owned_session

    def fail_first(path: Path, project: Path) -> None:
        if path == first.path:
            raise OSError("simulated per-item failure")
        original(path, project)

    monkeypatch.setattr(dev_temp, "_remove_owned_session", fail_first)
    events = cleanup_stale_sessions(tmp_path, identity_reader=identities)

    assert first.path.exists()
    assert not second.path.exists()
    assert any(
        event.path == first.path
        and event.action == "preserved"
        and "simulated per-item failure" in event.detail
        for event in events
    )
    assert any(event.path == second.path and event.action == "removed" for event in events)


def test_create_and_stale_cleanup_are_serialized_by_lifecycle_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import dev_temp

    identities = IdentityRegistry()
    identities.values[101] = _identity(101)
    entered = threading.Event()
    release = threading.Event()
    stale_done = threading.Event()
    original = dev_temp._atomic_write_marker
    created: list = []

    def blocked_write(path: Path, marker: dict) -> None:
        entered.set()
        assert release.wait(5)
        original(path, marker)

    monkeypatch.setattr(dev_temp, "_atomic_write_marker", blocked_write)
    creator = threading.Thread(
        target=lambda: created.append(
            create_session("pytest", tmp_path, owner_pid=101, identity_reader=identities)
        )
    )
    creator.start()
    assert entered.wait(5)
    sweeper = threading.Thread(
        target=lambda: (
            cleanup_stale_sessions(tmp_path, identity_reader=identities),
            stale_done.set(),
        )
    )
    sweeper.start()
    time.sleep(0.1)
    assert not stale_done.is_set()
    release.set()
    creator.join(5)
    sweeper.join(5)

    assert created[0].path.exists()
    assert stale_done.is_set()


@pytest.mark.parametrize("competing_operation", ["stale", "final"])
def test_claim_is_serialized_against_stale_and_final_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    competing_operation: str,
) -> None:
    from scripts import dev_temp

    identities = IdentityRegistry()
    identities.values.update(
        {101: _identity(101), 202: _identity(202)}
    )
    session = create_session(
        "pytest", tmp_path, owner_pid=101, identity_reader=identities
    )
    entered = threading.Event()
    release = threading.Event()
    competing_done = threading.Event()
    original = dev_temp._atomic_write_marker

    def blocked_claim_write(path: Path, marker: dict) -> None:
        if "claimed_at" in marker:
            entered.set()
            assert release.wait(5)
        original(path, marker)

    monkeypatch.setattr(dev_temp, "_atomic_write_marker", blocked_claim_write)
    claimer = threading.Thread(
        target=lambda: claim_session(
            session.path,
            session.token,
            owner_pid=202,
            identity_reader=identities,
        )
    )
    claimer.start()
    assert entered.wait(5)

    def compete() -> None:
        if competing_operation == "stale":
            cleanup_stale_sessions(tmp_path, identity_reader=identities)
        else:
            cleanup_session(
                session.path,
                session.token,
                owner_pid=202,
                identity_reader=identities,
            )
        competing_done.set()

    competitor = threading.Thread(target=compete)
    competitor.start()
    time.sleep(0.1)
    assert not competing_done.is_set()
    release.set()
    claimer.join(5)
    competitor.join(5)

    assert competing_done.is_set()
    assert session.path.exists() is (competing_operation == "stale")


def test_partial_deletion_retains_recoverable_external_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import dev_temp

    identities = IdentityRegistry()
    identities.values[101] = _identity(101)
    session = create_session(
        "pytest", tmp_path, owner_pid=101, identity_reader=identities
    )
    original = dev_temp._remove_owned_session
    monkeypatch.setattr(
        dev_temp,
        "_remove_owned_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("partial delete")),
    )
    with pytest.raises(OSError, match="partial delete"):
        cleanup_session(
            session.path,
            session.token,
            owner_pid=101,
            identity_reader=identities,
        )
    deleting_marker = session.path.parent / f".deleting-{session.run}.json"
    assert deleting_marker.is_file()
    assert session.path.exists()

    monkeypatch.setattr(dev_temp, "_remove_owned_session", original)
    identities.values[101] = ProcessMissingError(101)
    events = cleanup_stale_sessions(tmp_path, identity_reader=identities)
    assert not session.path.exists()
    assert not deleting_marker.exists()
    assert any("partial deletion" in event.detail for event in events)


def test_concurrent_sessions_are_unique_and_empty_parent_cleanup_is_non_recursive(
    tmp_path: Path,
) -> None:
    identities = IdentityRegistry()
    identities.values[101] = _identity(101)
    first = create_session(
        "pytest",
        tmp_path,
        owner_pid=101,
        identity_reader=identities,
    )
    second = create_session(
        "pytest",
        tmp_path,
        owner_pid=101,
        identity_reader=identities,
    )
    sentinel = tmp_path / ".devtmp" / "user-owned.txt"
    sentinel.write_text("preserve\n", encoding="utf-8")

    assert first.path != second.path
    cleanup_session(
        first.path,
        first.token,
        owner_pid=101,
        identity_reader=identities,
    )
    assert second.path.exists()
    cleanup_session(
        second.path,
        second.token,
        owner_pid=101,
        identity_reader=identities,
    )
    assert sentinel.read_text(encoding="utf-8") == "preserve\n"


def test_current_session_path_requires_active_marker_and_matching_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.dev_temp import CURRENT_SESSION_ENV, CURRENT_TOKEN_ENV

    identities = IdentityRegistry()
    identities.values[101] = _identity(101)
    session = create_session(
        "pytest",
        tmp_path,
        owner_pid=101,
        identity_reader=identities,
    )
    monkeypatch.setenv(CURRENT_SESSION_ENV, str(session.path))
    monkeypatch.setenv(CURRENT_TOKEN_ENV, session.token)

    assert path_is_in_current_session(
        session.path / "basetemp" / "case",
        tmp_path,
        identity_reader=identities,
    )
    assert not path_is_in_current_session(
        tmp_path / "ordinary",
        tmp_path,
        identity_reader=identities,
    )
    monkeypatch.setenv(CURRENT_TOKEN_ENV, "wrong")
    assert not path_is_in_current_session(
        session.path,
        tmp_path,
        identity_reader=identities,
    )
    cleanup_session(
        session.path,
        session.token,
        owner_pid=101,
        identity_reader=identities,
    )


def test_cleanup_handles_deep_windows_test_artifacts(tmp_path: Path) -> None:
    identities = IdentityRegistry()
    identities.values[101] = _identity(101)
    session = create_session(
        "pytest",
        tmp_path,
        owner_pid=101,
        identity_reader=identities,
    )
    artifact = session.path / ("a" * 90) / ("b" * 90) / "result.json"
    if os.name == "nt":
        extended_artifact = f"\\\\?\\{artifact}"
        os.makedirs(str(Path(extended_artifact).parent))
        with open(extended_artifact, "w", encoding="utf-8") as stream:
            stream.write("{}\n")
    else:
        artifact.parent.mkdir(parents=True)
        artifact.write_text("{}\n", encoding="utf-8")

    cleanup_session(
        session.path,
        session.token,
        owner_pid=101,
        identity_reader=identities,
    )

    assert not session.path.exists()


def test_active_clean_clone_can_claim_short_sibling_pytest_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.dev_temp import SESSION_ENV, TOKEN_ENV

    identities = IdentityRegistry()
    owner_pid = os.getpid()
    outer = create_session(
        "clean-clone",
        tmp_path,
        owner_pid=owner_pid,
        identity_reader=identities,
    )
    worktree = outer.path / "worktree"
    worktree.mkdir()
    pytest_session = create_session(
        "pytest",
        tmp_path,
        owner_pid=owner_pid,
        identity_reader=identities,
    )
    monkeypatch.setenv(SESSION_ENV, str(pytest_session.path))
    monkeypatch.setenv(TOKEN_ENV, pytest_session.token)

    claimed = claim_session_from_environment(
        worktree,
        identity_reader=identities,
    )

    assert claimed is not None
    assert claimed.path == pytest_session.path
    cleanup_session(
        claimed.path,
        claimed.token,
        owner_pid=owner_pid,
        identity_reader=identities,
    )
    cleanup_session(
        outer.path,
        outer.token,
        owner_pid=owner_pid,
        identity_reader=identities,
    )


def test_inactive_clean_clone_cannot_authorize_cross_project_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.dev_temp import SESSION_ENV, TOKEN_ENV

    identities = IdentityRegistry()
    owner_pid = os.getpid()
    outer = create_session(
        "clean-clone",
        tmp_path,
        owner_pid=owner_pid,
        identity_reader=identities,
    )
    worktree = outer.path / "worktree"
    worktree.mkdir()
    pytest_session = create_session(
        "pytest",
        tmp_path,
        owner_pid=owner_pid,
        identity_reader=identities,
    )
    monkeypatch.setenv(SESSION_ENV, str(pytest_session.path))
    monkeypatch.setenv(TOKEN_ENV, pytest_session.token)
    identities.values[owner_pid] = ProcessMissingError(owner_pid)

    with pytest.raises(DevTempError, match="不属于当前项目"):
        claim_session_from_environment(
            worktree,
            identity_reader=identities,
        )

    assert outer.path.exists()
    assert pytest_session.path.exists()
