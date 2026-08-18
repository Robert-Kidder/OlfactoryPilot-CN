from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.services.authorized_hal import (
    AuthorizationViolation,
    AuthorizedHAL,
    AuthorizedWrite,
)
from app.services.hal import DigitalWriteAck

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hil_story45_live",
    ROOT / "scripts" / "hil_story45_live.py",
)
assert SPEC is not None and SPEC.loader is not None
LIVE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = LIVE
SPEC.loader.exec_module(LIVE)


class FakeHAL:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.flows = {"A": 0.0, "B": 0.0, "C": 0.0}
        self.digital: dict[str, bool] = {}
        self.prepared = False
        self.serial_open = False

    @property
    def serial_resources_in_use(self) -> bool:
        return self.serial_open

    def set_flow(self, channel, value, *, comp=False):
        self.calls.append(("flow", channel, float(value), bool(comp)))
        self.flows[str(channel)] = float(value)
        self.serial_open = True
        return True

    def read_flow(self):
        return self.flows["A"]

    def prepare_do_output(self):
        self.calls.append(("prepare",))
        self.prepared = True
        return True

    def release_do_output(self):
        self.calls.append(("release",))
        self.prepared = False
        return True

    def write_digital_ack(self, *, device, line, state, timeout_ms):
        assert self.prepared
        target = f"{device}/{line}"
        self.calls.append(("digital", target, bool(state), int(timeout_ms)))
        self.digital[target] = bool(state)
        started_ns = time.perf_counter_ns()
        return DigitalWriteAck(
            success=True,
            started_ns=started_ns,
            actual_ns=started_ns + 1,
            wall_timestamp=time.time(),
            message="ok",
        )

    def stop_heaters(self):
        self.calls.append(("heaters",))
        return True

    def flush_logs(self):
        self.calls.append(("flush",))

    def reset_ai_input(self):
        self.calls.append(("ai_release",))
        return True

    def release_serial_resources(self):
        self.calls.append(("serial_release",))
        self.serial_open = False


@pytest.fixture
def config():
    return LIVE.load_effective_config(
        ROOT / "config" / "default_config.json",
        ROOT / "does-not-exist.json",
    )


def _candidate():
    return {"commit": "a" * 40, "tree": "b" * 40, "clean": True, "status_porcelain": []}


def test_plan_functions_do_not_import_hardware_modules(config):
    before = set(sys.modules)
    manifest = LIVE.build_manifest("normal", candidate=_candidate(), config=config)
    imported = set(sys.modules) - before

    assert "nidaqmx" not in imported
    assert "serial" not in imported
    assert manifest["normal_parameters"]["flow_sccm"] == 2500.0
    assert manifest["normal_parameters"]["observation_seconds"] == 20.0
    assert manifest["normal_parameters"]["representative_valve"] == 2
    assert manifest["expected_a"]["full_scale_sccm"] == 5000.0
    assert manifest["authorization_token"].endswith(manifest["manifest_sha256"])


def test_clean_process_loads_plan_without_hardware_modules():
    command = (
        "import runpy,sys; "
        "runpy.run_path('scripts/hil_story45_live.py',run_name='not_main'); "
        "print('nidaqmx' in sys.modules,'serial' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", command],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.stdout.strip() == "False False"


def test_manifest_has_exact_selector_and_twenty_unique_odors(config):
    manifest = LIVE.build_manifest("normal", candidate=_candidate(), config=config)

    assert manifest["selector"] == {
        "target": "Dev2/P1.0",
        "safe_level": False,
        "odor_level": True,
    }
    assert len(manifest["odor_targets"]) == 20
    assert len(set(manifest["odor_targets"])) == 20
    assert "Dev2/P1.0" not in manifest["odor_targets"]
    assert [item["sequence"] for item in manifest["writes"]] == list(
        range(1, len(manifest["writes"]) + 1)
    )


def test_authorized_hal_consumes_exact_write_once():
    fake = FakeHAL()
    proxy = AuthorizedHAL(
        fake,
        [AuthorizedWrite(1, "shutdown", "flow", "A", 0.0)],
    )

    assert proxy.set_flow("A", 0.0) is True
    with pytest.raises(AuthorizationViolation):
        proxy.set_flow("A", 0.0)

    assert fake.calls == [("flow", "A", 0.0, False)]


def test_authorized_hal_rejects_unknown_delegate_method():
    proxy = AuthorizedHAL(FakeHAL(), [])

    with pytest.raises(AttributeError, match="未显式审计"):
        proxy.self_check()


@pytest.mark.parametrize(
    ("channel", "value", "comp"),
    [("B", 0.0, False), ("A", 1.0, False), ("A", 0.0, True)],
)
def test_authorized_hal_rejects_mismatch_before_delegate(channel, value, comp):
    fake = FakeHAL()
    proxy = AuthorizedHAL(
        fake,
        [AuthorizedWrite(1, "shutdown", "flow", "A", 0.0)],
    )

    with pytest.raises(AuthorizationViolation):
        proxy.set_flow(channel, value, comp=comp)

    assert fake.calls == []
    assert len(proxy.violations) == 1


def test_advance_to_shutdown_only_skips_optional_setup():
    fake = FakeHAL()
    proxy = AuthorizedHAL(
        fake,
        [
            AuthorizedWrite(1, "setup", "flow", "A", 2500.0, optional=True),
            AuthorizedWrite(2, "shutdown", "flow", "A", 0.0),
        ],
    )

    proxy.advance_to_phase("shutdown")
    assert proxy.set_flow("A", 0.0)
    proxy.assert_required_consumed()


def test_advance_to_shutdown_cannot_skip_required_setup():
    proxy = AuthorizedHAL(
        FakeHAL(),
        [
            AuthorizedWrite(1, "setup", "flow", "A", 2500.0),
            AuthorizedWrite(2, "shutdown", "flow", "A", 0.0),
        ],
    )

    with pytest.raises(AuthorizationViolation):
        proxy.advance_to_phase("shutdown")


def test_validate_manifest_rejects_token_before_hardware_import(monkeypatch, config):
    manifest = LIVE.build_manifest("normal", candidate=_candidate(), config=config)
    before = set(sys.modules)
    monkeypatch.setattr(
        LIVE,
        "candidate_snapshot",
        lambda **_kwargs: _candidate(),
    )

    with pytest.raises(ValueError, match="授权 token"):
        LIVE.validate_manifest(manifest, "wrong")

    imported = set(sys.modules) - before
    assert "nidaqmx" not in imported
    assert "serial" not in imported


def test_effective_config_drift_is_rejected(config):
    manifest = LIVE.build_manifest("normal", candidate=_candidate(), config=config)
    changed = {**config, "alicat_setpoint_scale": 0.002}

    with pytest.raises(ValueError, match="有效硬件配置"):
        LIVE.validate_effective_config(manifest, changed)


def test_full_scale_parser_does_not_confuse_statistic_id_with_value():
    assert LIVE._parse_full_scale_nlpm("A 5 5.0000 71 NLPM", "a") == 5.0
    assert LIVE._parse_full_scale_nlpm("A 5 1.0000 71 NLPM", "a") == 1.0


@pytest.mark.parametrize(
    "response",
    [
        "A +014.57 +029.93 +0.0000 nan +0.0000 Air",
        "A +014.57 +029.93 +0.0000 +0.0000 nan Air",
    ],
)
def test_status_parser_rejects_non_finite_values(response):
    with pytest.raises(ValueError, match="非有限"):
        LIVE._parse_status(response, "a")


def test_read_only_preflight_uses_only_documented_query_frames(monkeypatch, config):
    commands = []
    responses = {
        "a??D*\r": [b"Mass Flow NLPM\r\n", b"Setpoint NLPM\r\n", b"Gas\r\n"],
        "b??D*\r": [b"Mass Flow NLPM\r\n", b"Setpoint NLPM\r\n", b"Gas\r\n"],
        "c??D*\r": [b"Mass Flow NLPM\r\n", b"Setpoint NLPM\r\n", b"Gas\r\n"],
        "a\r": [b"A +014.57 +029.93 +0.0000 +0.0000 +0.0000 Air\r\n"],
        "b\r": [b"B +014.57 +030.65 +0.0000 +0.0000 +0.0000 Air\r\n"],
        "c\r": [b"C +002.63 +029.39 +0.0000 +0.0000 +0.0000 Air\r\n"],
        "a??M*\r": [
            b"MODEL MC-5NLPM-D\r\n",
            b"SERIAL 486285\r\n",
        ],
        "aFPF 5\r": [b"A 5 5.0000 71 NLPM\r\n"],
    }

    class FakePort:
        def __init__(self, *_args, **_kwargs):
            self.pending = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def reset_input_buffer(self):
            return None

        def write(self, payload):
            command = payload.decode("ascii")
            commands.append(command)
            self.pending = list(responses[command])

        def flush(self):
            return None

        def readline(self):
            return self.pending.pop(0) if self.pending else b""

    nidaqmx = ModuleType("nidaqmx")
    nidaqmx_system = ModuleType("nidaqmx.system")
    nidaqmx_system.System = SimpleNamespace(
        local=lambda: SimpleNamespace(
            devices=[
                SimpleNamespace(name="Dev1", product_type="USB-6001", serial_num=34887710),
                SimpleNamespace(name="Dev2", product_type="USB-6001", serial_num=34887797),
            ]
        )
    )
    serial = ModuleType("serial")
    serial.Serial = FakePort
    monkeypatch.setitem(sys.modules, "nidaqmx", nidaqmx)
    monkeypatch.setitem(sys.modules, "nidaqmx.system", nidaqmx_system)
    monkeypatch.setitem(sys.modules, "serial", serial)
    config = {**config, "serial_port": "COM6"}

    result = LIVE.read_only_preflight(config)

    assert result["hardware_access"] == "read_only_queries"
    assert commands == [
        "a??D*\r", "a\r", "b??D*\r", "b\r", "c??D*\r", "c\r",
        "a??M*\r", "aFPF 5\r",
    ]
    assert all("s" not in command.lower() for command in commands)


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("normal", "success"),
        ("a_zero_failure", "recovery_required"),
        ("a_zero_timeout", "recovery_required"),
        ("stale_a_receipt", "recovery_required"),
        ("late_a_receipt", "recovery_required"),
        ("stale_selector_receipt", "recovery_required"),
        ("late_selector_receipt", "recovery_required"),
        ("selector_uncertain", "recovery_required"),
        ("owner_handoff_failed", "recovery_required"),
    ],
)
def test_all_scenarios_use_production_shutdown_and_expected_result(
    tmp_path,
    monkeypatch,
    config,
    scenario,
    expected,
):
    monkeypatch.setattr(LIVE, "OBSERVATION_SECONDS", 0.0)
    monkeypatch.setattr(LIVE.time, "sleep", lambda _seconds: None)
    manifest = LIVE.build_manifest(scenario, candidate=_candidate(), config=config)
    fake = FakeHAL()

    summary = LIVE.execute_live(
        manifest,
        output_dir=tmp_path / scenario,
        config=config,
        delegate_hal=fake,
        preflight_result={"hardware_access": False},
        sleep_func=lambda _seconds: None,
        operator_observation=(
            LIVE.ALLOWED_OPERATOR_OBSERVATIONS[0] if scenario == "normal" else None
        ),
    )

    assert summary["actual_result"] == expected
    assert summary["fault_oracle_passed"] is True
    assert summary["automated_verification_passed"] is True
    assert summary["verification_passed"] is (scenario == "normal")
    assert summary["authorization_violations"] == []
    assert fake.flows == {"A": 0.0, "B": 0.0, "C": 0.0}
    assert summary["owner_handoff"]["do_handed_off"] is True
    assert summary["owner_handoff"]["serial_resources_in_use"] is False
    assert (tmp_path / scenario / "shutdown-event.json").exists()
    assert (tmp_path / scenario / "timeline.jsonl").exists()


@pytest.mark.parametrize(
    "scenario",
    ["a_zero_failure", "a_zero_timeout", "stale_a_receipt", "late_a_receipt"],
)
def test_a_receipt_faults_never_write_selector(tmp_path, monkeypatch, config, scenario):
    monkeypatch.setattr(LIVE.time, "sleep", lambda _seconds: None)
    manifest = LIVE.build_manifest(scenario, candidate=_candidate(), config=config)
    fake = FakeHAL()

    LIVE.execute_live(
        manifest,
        output_dir=tmp_path / scenario,
        config=config,
        delegate_hal=fake,
        preflight_result={},
    )

    selector_writes = [
        call
        for call in fake.calls
        if call[0] == "digital"
        and call[1].lower() in {"dev2/p1.0", "dev2/port1/line0"}
    ]
    assert selector_writes == []


def test_normal_timeline_proves_a_zero_before_selector_low(
    tmp_path,
    monkeypatch,
    config,
):
    monkeypatch.setattr(LIVE, "OBSERVATION_SECONDS", 0.0)
    manifest = LIVE.build_manifest("normal", candidate=_candidate(), config=config)

    LIVE.execute_live(
        manifest,
        output_dir=tmp_path / "normal",
        config=config,
        delegate_hal=FakeHAL(),
        preflight_result={},
        sleep_func=lambda _seconds: None,
    )
    events = [
        json.loads(line)
        for line in (tmp_path / "normal" / "timeline.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    a_zero = next(
        item
        for item in events
        if item.get("event") == "hardware_write_result"
        and item["write"]["phase"] == "shutdown"
        and item["write"]["kind"] == "flow"
        and item["write"]["target"] == "A"
    )
    selector = next(
        item
        for item in events
        if item.get("event") == "hardware_write_result"
        and item["write"]["phase"] == "shutdown"
        and item["write"]["kind"] == "digital"
        and item["write"]["target"] == "Dev2/P1.0"
    )
    assert a_zero["sequence"] < selector["sequence"]
    assert a_zero["success"] is True


def test_selector_uncertain_is_injected_at_ack_boundary(
    tmp_path,
    monkeypatch,
    config,
):
    manifest = LIVE.build_manifest(
        "selector_uncertain",
        candidate=_candidate(),
        config=config,
    )
    fake = FakeHAL()

    summary = LIVE.execute_live(
        manifest,
        output_dir=tmp_path / "selector-uncertain",
        config=config,
        delegate_hal=fake,
        preflight_result={},
    )
    events = [
        json.loads(line)
        for line in (tmp_path / "selector-uncertain" / "timeline.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    selector = next(
        item
        for item in events
        if item.get("event") == "hardware_write_result"
        and item["write"]["phase"] == "shutdown"
        and item["write"]["target"] == "Dev2/P1.0"
    )

    assert selector["delegate_success"] is True
    assert selector["success"] is False
    assert selector["uncertain"] is True
    assert summary["final_state"]["selector"]["software_evidence"] == "unknown"
    assert len(
        [
            call
            for call in fake.calls
            if call[0] == "digital"
            and call[1].lower() in {"dev2/p1.0", "dev2/port1/line0"}
        ]
    ) == 1


@pytest.mark.parametrize(
    "samples",
    [
        [float("inf")],
        [3000.0],
        [2500.0, 2500.0, 2500.0, 2000.0],
    ],
)
def test_normal_flow_supervision_aborts_and_still_shuts_down(
    tmp_path,
    monkeypatch,
    config,
    samples,
):
    monkeypatch.setattr(LIVE, "OBSERVATION_SECONDS", 1.0)

    class SampleHAL(FakeHAL):
        def __init__(self):
            super().__init__()
            self.samples = iter(samples)

        def read_flow(self):
            return next(self.samples, samples[-1])

    summary = LIVE.execute_live(
        LIVE.build_manifest("normal", candidate=_candidate(), config=config),
        output_dir=tmp_path / f"flow-{len(samples)}-{samples[-1]}",
        config=config,
        delegate_hal=SampleHAL(),
        preflight_result={},
        sleep_func=lambda _seconds: None,
        operator_observation=LIVE.ALLOWED_OPERATOR_OBSERVATIONS[0],
    )

    assert summary["overall_result"] == "recovery_required"
    assert summary["verification_passed"] is False
    assert summary["owner_handoff"]["do_handed_off"] is True


def test_normal_requires_post_stop_operator_observation(tmp_path, monkeypatch, config):
    monkeypatch.setattr(LIVE, "OBSERVATION_SECONDS", 0.0)
    output_dir = tmp_path / "observation"
    summary = LIVE.execute_live(
        LIVE.build_manifest("normal", candidate=_candidate(), config=config),
        output_dir=output_dir,
        config=config,
        delegate_hal=FakeHAL(),
        preflight_result={},
        sleep_func=lambda _seconds: None,
    )

    assert summary["automated_verification_passed"] is True
    assert summary["verification_passed"] is False
    args = SimpleNamespace(
        evidence_dir=output_dir,
        observation=LIVE.ALLOWED_OPERATOR_OBSERVATIONS[0],
    )
    assert LIVE._command_record_observation(args) == 0
    recorded = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert recorded["verification_passed"] is True


def test_cli_has_no_live_all_mode():
    parser = LIVE.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--all"])
