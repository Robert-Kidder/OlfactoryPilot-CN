from __future__ import annotations

import sys
from types import SimpleNamespace

from scripts import probe_alicat


class _Port:
    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


class _Session:
    instances: list[_Session] = []

    def __init__(self, port, *, baud_rate: int, frame_timeout_s: float) -> None:
        self.port = port
        self.baud_rate = baud_rate
        self.frame_timeout_s = frame_timeout_s
        self.calls: list[tuple] = []
        self.instances.append(self)

    def poll(
        self,
        unit: str,
        *,
        expected_setpoint: float | None = None,
        setpoint_tolerance: float = 0.0,
    ):
        self.calls.append(
            ("poll", unit, expected_setpoint, setpoint_tolerance)
        )
        return SimpleNamespace(raw=f"{unit.upper()} 1 2 3 4 0 Air\r".encode())

    def set_setpoint(self, unit: str, value: float, *, tolerance: float):
        self.calls.append(("set", unit, value, tolerance))
        return SimpleNamespace(raw=f"{unit.upper()} 1 2 3 4 {value:.3f} Air\r".encode())


def _install_fakes(monkeypatch) -> None:
    _Session.instances.clear()
    monkeypatch.setattr(probe_alicat, "AlicatSerialSession", _Session)
    monkeypatch.setattr(
        probe_alicat,
        "serial",
        SimpleNamespace(Serial=lambda *_args, **_kwargs: _Port()),
    )
    monkeypatch.setattr(probe_alicat.time, "sleep", lambda _duration: None)


def test_probe_default_is_read_only_poll_for_each_requested_id(monkeypatch) -> None:
    _install_fakes(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe_alicat.py", "--port", "COM-FAKE", "--ids", "a,b,c"],
    )

    assert probe_alicat.main() == 0

    assert _Session.instances[0].calls == [
        ("poll", "a", None, 0.0),
        ("poll", "b", None, 0.0),
        ("poll", "c", None, 0.0),
    ]


def test_probe_set_consumes_quantized_response_then_verifies_poll(monkeypatch) -> None:
    _install_fakes(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe_alicat.py",
            "--port",
            "COM-FAKE",
            "--ids",
            "a",
            "--set",
            "a",
            "123.4",
        ],
    )

    assert probe_alicat.main() == 0

    assert _Session.instances[0].calls == [
        ("poll", "a", None, 0.0),
        ("set", "a", 0.123, 0.00005),
        ("poll", "a", 0.123, 0.00005),
    ]
