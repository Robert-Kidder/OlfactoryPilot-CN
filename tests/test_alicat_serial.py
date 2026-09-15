from __future__ import annotations

import threading
from collections import deque
from types import SimpleNamespace

import pytest

from app.models import SafeStopIdentity
from app.services.alicat_serial import (
    AlicatFrameTimeout,
    AlicatInitialSyncError,
    AlicatPartialFrame,
    AlicatResponseMismatch,
    AlicatSerialSession,
    AlicatSessionDesynchronized,
    AlicatTransportError,
)
from app.services.flow_service import FlowService
from app.workers.flow_worker import FlowWorker


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 0.0
        self.nanoseconds = 0

    def monotonic(self) -> float:
        return self.seconds

    def monotonic_ns(self) -> int:
        self.nanoseconds += 1
        return self.nanoseconds

    def sleep(self, duration: float) -> None:
        self.seconds += max(0.0, float(duration))


class StatefulFakeSerial:
    def __init__(self, responses=(), *, initial_rx: bytes = b"") -> None:
        self.responses = deque(bytes(item) for item in responses)
        self.rx = bytearray(initial_rx)
        self.writes: list[bytes] = []
        self.events: list[tuple] = []
        self.is_open = True

    @property
    def in_waiting(self) -> int:
        return len(self.rx)

    def read(self, size: int = 1) -> bytes:
        chunk = bytes(self.rx[:size])
        del self.rx[:size]
        self.events.append(("read", chunk))
        return chunk

    def write(self, payload: bytes) -> int:
        value = bytes(payload)
        self.writes.append(value)
        self.events.append(("write", value))
        return len(value)

    def flush(self) -> None:
        self.events.append(("flush",))

    def read_until(self, expected: bytes = b"\n") -> bytes:
        self.events.append(("read_until", expected))
        if self.rx:
            end = self.rx.find(expected)
            if end >= 0:
                end += len(expected)
                chunk = bytes(self.rx[:end])
                del self.rx[:end]
                return chunk
        return self.responses.popleft() if self.responses else b""

    def close(self) -> None:
        self.is_open = False
        self.events.append(("close",))


def make_session(
    port: StatefulFakeSerial, clock: FakeClock | None = None
) -> AlicatSerialSession:
    clock = clock or FakeClock()
    return AlicatSerialSession(
        port,
        baud_rate=19200,
        frame_timeout_s=0.2,
        monotonic=clock.monotonic,
        monotonic_ns=clock.monotonic_ns,
        sleeper=clock.sleep,
    )


def zero_frame(unit: str) -> bytes:
    return f"{unit} 14.7 25.0 0.0 0.0 0.000 Air\r".encode()


def make_real_hal(monkeypatch, ports: list[StatefulFakeSerial]):
    from app.services import real_hal as real_hal_module

    pending = deque(ports)

    def create_serial(*_args, **_kwargs):
        return pending.popleft()

    monkeypatch.setattr(
        real_hal_module,
        "serial",
        SimpleNamespace(
            Serial=create_serial,
            EIGHTBITS=8,
            PARITY_NONE="N",
            STOPBITS_ONE=1,
        ),
    )
    monkeypatch.setattr(real_hal_module, "_SERIAL_IMPORT_ERROR", None)
    return real_hal_module.RealHAL(
        serial_port="COM-FAKE",
        serial_timeout_s=0.001,
        setpoint_verify_delay_s=0,
    )


def test_cr_only_response_completes_without_lf() -> None:
    port = StatefulFakeSerial([b"A U\r"])

    frame = make_session(port).query_setpoint_source("a")

    assert frame.raw == b"A U\r"
    assert ("read_until", b"\r") in port.events


def test_partial_frame_without_cr_latches_session_and_blocks_next_tx() -> None:
    port = StatefulFakeSerial([b"A U", b"A U\r"])
    session = make_session(port)

    with pytest.raises(AlicatPartialFrame):
        session.query_setpoint_source("a")
    with pytest.raises(AlicatSessionDesynchronized):
        session.query_setpoint_source("a")

    assert port.writes == [b"aLSS\r"]


def test_lss_timeout_fake_can_reproduce_legacy_ve_receiving_delayed_a_u() -> None:
    port = StatefulFakeSerial([b"", b"A U\r"])

    def legacy_query(payload: bytes) -> bytes:
        port.write(payload)
        port.flush()
        return port.read_until(b"\r")

    assert legacy_query(b"aLSS\r") == b""
    assert legacy_query(b"aVE\r") == b"A U\r"


def test_firmware_transaction_rejects_lss_shaped_stale_frame() -> None:
    port = StatefulFakeSerial([b"A U\r"])
    session = make_session(port)

    with pytest.raises(AlicatResponseMismatch):
        session.query_firmware("a")

    assert session.desynchronized is True


def test_valid_firmware_and_lss_frames_pass_command_specific_validation() -> None:
    port = StatefulFakeSerial(
        [b"A   10v14.0-R24 Feb 26 2024,13:06:19\r", b"A U\r"]
    )
    session = make_session(port)

    assert "10v14.0-R24" in session.query_firmware("a").text
    assert session.query_setpoint_source("a").text == "A U"


def test_wrong_unit_latches_session() -> None:
    port = StatefulFakeSerial([b"B U\r"])
    session = make_session(port)

    with pytest.raises(AlicatResponseMismatch, match="unit mismatch"):
        session.query_setpoint_source("a")

    assert session.desynchronized is True


def test_required_numeric_fields_must_be_contiguous() -> None:
    port = StatefulFakeSerial([b"A 14.7 25.0 Air 0.0 0.0 0.0\r"])
    session = make_session(port)

    with pytest.raises(AlicatResponseMismatch, match="non-numeric required field"):
        session.poll("a")

    assert session.desynchronized is True


@pytest.mark.parametrize(
    "raw",
    [
        b"A 14.7 25.0 0.0 0.0\r",
        b"A 14.7 25.0 nan 0.0 0.0 Air\r",
        b"A 14.7 25.0 inf 0.0 0.0 Air\r",
        b"A 14.7 25.0 -inf 0.0 0.0 Air\r",
        b"A 14.7 25.0 0.0 0.0 0.0\r",
    ],
)
def test_truncated_nonfinite_or_gasless_frame_latches_and_blocks_tx(raw) -> None:
    port = StatefulFakeSerial([raw, zero_frame("A")])
    session = make_session(port)

    with pytest.raises(AlicatResponseMismatch):
        session.poll("a")
    with pytest.raises(AlicatSessionDesynchronized):
        session.poll("a")

    assert port.writes == [b"a\r"]


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_frame_timeout_is_rejected(timeout) -> None:
    with pytest.raises(ValueError, match="frame_timeout_s"):
        AlicatSerialSession(
            StatefulFakeSerial(),
            baud_rate=19200,
            frame_timeout_s=timeout,
        )


def test_nonfinite_setpoint_tolerance_is_rejected_before_tx() -> None:
    port = StatefulFakeSerial([zero_frame("A")])
    session = make_session(port)

    with pytest.raises(ValueError, match="setpoint_tolerance"):
        session.poll("a", expected_setpoint=0.0, setpoint_tolerance=float("inf"))

    assert port.writes == []


def test_short_write_latches_session_and_blocks_later_tx() -> None:
    class ShortWriteSerial(StatefulFakeSerial):
        def write(self, payload: bytes) -> int:
            super().write(payload)
            return len(payload) - 1

    port = ShortWriteSerial([zero_frame("A")])
    session = make_session(port)

    with pytest.raises(AlicatTransportError, match="transport failed"):
        session.poll("a")
    with pytest.raises(AlicatSessionDesynchronized):
        session.poll("a")

    assert port.writes == [b"a\r"]


def test_none_write_result_is_not_accepted_as_complete() -> None:
    class UnknownWriteSerial(StatefulFakeSerial):
        def write(self, payload: bytes) -> None:
            super().write(payload)
            return None

    port = UnknownWriteSerial([zero_frame("A")])

    with pytest.raises(AlicatTransportError, match="transport failed"):
        make_session(port).poll("a")

    assert port.writes == [b"a\r"]


def test_read_transport_failure_latches_session_and_blocks_later_tx() -> None:
    class ReadFailureSerial(StatefulFakeSerial):
        def read_until(self, expected: bytes = b"\n") -> bytes:
            self.events.append(("read_until", expected))
            raise OSError("USB bridge disconnected")

    port = ReadFailureSerial()
    session = make_session(port)

    with pytest.raises(AlicatTransportError, match="transport failed"):
        session.poll("a")
    with pytest.raises(AlicatSessionDesynchronized):
        session.poll("a")

    assert port.writes == [b"a\r"]


def test_initial_sync_transport_failure_latches_before_any_tx() -> None:
    class SyncFailureSerial(StatefulFakeSerial):
        @property
        def in_waiting(self) -> int:
            raise OSError("cannot inspect receive queue")

    port = SyncFailureSerial()
    session = make_session(port)

    with pytest.raises(AlicatInitialSyncError, match="initial sync"):
        session.poll("a")
    with pytest.raises(AlicatSessionDesynchronized):
        session.poll("a")

    assert port.writes == []


def test_negative_in_waiting_latches_before_any_tx() -> None:
    class NegativeWaitingSerial(StatefulFakeSerial):
        @property
        def in_waiting(self) -> int:
            return -1

    port = NegativeWaitingSerial()

    with pytest.raises(AlicatInitialSyncError):
        make_session(port).poll("a")

    assert port.writes == []


def test_same_type_poll_after_timeout_has_zero_tx_until_new_session() -> None:
    old_port = StatefulFakeSerial([b""])
    old_session = make_session(old_port)

    with pytest.raises(AlicatFrameTimeout):
        old_session.poll("a")
    old_port.rx.extend(b"A 14.7 25.0 0.0 0.0 0.0 Air\r")
    with pytest.raises(AlicatSessionDesynchronized):
        old_session.poll("a")
    assert old_port.writes == [b"a\r"]

    old_port.close()
    new_port = StatefulFakeSerial(
        [b"A 14.7 25.0 0.0 0.0 0.0 Air\r"],
        initial_rx=b"A 14.7 25.0 9.0 9.0 9.0 Air\r",
    )
    new_session = make_session(new_port)
    frame = new_session.poll("a")

    assert frame.text == "A 14.7 25.0 0.0 0.0 0.0 Air"
    assert new_port.writes == [b"a\r"]
    assert any(event[0] == "read" for event in new_port.events)


def test_initial_sync_restarts_quiet_window_for_late_stale_frame() -> None:
    clock = FakeClock()

    class DelayedStaleSerial(StatefulFakeSerial):
        injected = False

        @property
        def in_waiting(self) -> int:
            if not self.injected and clock.seconds >= 0.25:
                self.rx.extend(b"A U\r")
                self.injected = True
            return len(self.rx)

    port = DelayedStaleSerial([zero_frame("A")])
    session = make_session(port, clock)

    frame = session.poll("a")

    assert frame.raw == zero_frame("A")
    assert clock.seconds >= 0.65
    assert port.writes == [b"a\r"]
    assert ("read", b"A U\r") in port.events


def test_initial_sync_never_transmits_when_late_frames_prevent_quiet() -> None:
    clock = FakeClock()

    class RepeatedLateSerial(StatefulFakeSerial):
        next_injection_s = 0.25

        @property
        def in_waiting(self) -> int:
            if clock.seconds >= self.next_injection_s:
                self.rx.extend(b"A U\r")
                self.next_injection_s += 0.3
            return len(self.rx)

    port = RepeatedLateSerial([zero_frame("A")])
    session = make_session(port, clock)

    with pytest.raises(AlicatInitialSyncError, match="bounded quiet"):
        session.poll("a")

    assert session.desynchronized is True
    assert port.writes == []


def test_pending_frame_after_success_latches_before_second_tx() -> None:
    port = StatefulFakeSerial([zero_frame("A")])
    session = make_session(port)

    session.poll("a")
    port.rx.extend(zero_frame("A"))
    with pytest.raises(AlicatResponseMismatch, match="unattributed pending"):
        session.poll("a")

    assert session.desynchronized is True
    assert port.writes == [b"a\r"]


def test_setpoint_response_is_consumed_before_independent_verify_poll() -> None:
    response = b"A 14.7 25.0 0.0 0.0 0.123 Air\r"
    port = StatefulFakeSerial([response, response])
    session = make_session(port)

    set_frame = session.set_setpoint("a", 0.123, tolerance=0.00005)
    poll_frame = session.poll("a")

    assert set_frame.raw == response
    assert poll_frame.raw == response
    assert port.events[-6:] == [
        ("write", b"as0.123\r"),
        ("flush",),
        ("read_until", b"\r"),
        ("write", b"a\r"),
        ("flush",),
        ("read_until", b"\r"),
    ]


def test_delayed_setpoint_response_cannot_be_consumed_by_verify_poll() -> None:
    port = StatefulFakeSerial([b"", b"A 14.7 25.0 0.0 0.0 0.123 Air\r"])
    session = make_session(port)

    with pytest.raises(AlicatFrameTimeout):
        session.set_setpoint("a", 0.123, tolerance=0.00005)
    with pytest.raises(AlicatSessionDesynchronized):
        session.poll("a")

    assert port.writes == [b"as0.123\r"]


def test_setpoint_response_must_contain_commanded_value() -> None:
    port = StatefulFakeSerial([b"A 14.7 25.0 0.0 0.0 0.000 Air\r"])

    with pytest.raises(AlicatResponseMismatch, match="setpoint response mismatch"):
        make_session(port).set_setpoint("a", 0.123, tolerance=0.00005)


def test_setpoint_response_and_poll_use_exact_quantized_wire_target(
    monkeypatch,
) -> None:
    frame = b"A 14.7 25.0 0.0 0.0 0.123 Air\r"
    port = StatefulFakeSerial([frame, frame])
    hal = make_real_hal(monkeypatch, [port])
    hal._setpoint_verify_tolerance = 0.00005

    assert hal.set_flow("A", 123.4) is True
    assert port.writes == [b"as0.123\r", b"a\r"]
    assert hal.last_setpoint_readback_sccm("A") == 123.0


def test_transaction_lock_covers_write_read_and_validation() -> None:
    first_write = threading.Event()
    allow_first_read = threading.Event()

    class BlockingSerial(StatefulFakeSerial):
        def write(self, payload: bytes) -> int:
            result = super().write(payload)
            if len(self.writes) == 1:
                first_write.set()
            return result

        def read_until(self, expected: bytes = b"\n") -> bytes:
            if len(self.writes) == 1:
                assert allow_first_read.wait(1.0)
            return super().read_until(expected)

    port = BlockingSerial(
        [
            b"A 14.7 25.0 0.0 0.0 0.0 Air\r",
            b"B 14.7 25.0 0.0 0.0 0.0 Air\r",
        ]
    )
    session = make_session(port)
    errors: list[Exception] = []

    def poll(unit: str) -> None:
        try:
            session.poll(unit)
        except Exception as exc:  # pragma: no cover - assertion capture
            errors.append(exc)

    first = threading.Thread(target=poll, args=("a",))
    second = threading.Thread(target=poll, args=("b",))
    first.start()
    assert first_write.wait(1.0)
    second.start()
    assert port.writes == [b"a\r"]
    allow_first_read.set()
    first.join(1.0)
    second.join(1.0)

    assert errors == []
    assert port.writes == [b"a\r", b"b\r"]


def test_transaction_lock_remains_held_until_validator_finishes() -> None:
    validation_started = threading.Event()
    allow_validation = threading.Event()
    port = StatefulFakeSerial([zero_frame("A"), zero_frame("B")])
    session = make_session(port)
    errors: list[Exception] = []

    def blocking_validator(_text: str) -> None:
        validation_started.set()
        assert allow_validation.wait(1.0)

    def first_transaction() -> None:
        try:
            session.transact(
                b"a\r",
                unit_id="a",
                command_kind="blocking-test",
                validator=blocking_validator,
            )
        except Exception as exc:  # pragma: no cover - assertion capture
            errors.append(exc)

    def second_transaction() -> None:
        try:
            session.poll("b")
        except Exception as exc:  # pragma: no cover - assertion capture
            errors.append(exc)

    first = threading.Thread(target=first_transaction)
    second = threading.Thread(target=second_transaction)
    first.start()
    assert validation_started.wait(1.0)
    second.start()
    assert port.writes == [b"a\r"]
    allow_validation.set()
    first.join(1.0)
    second.join(1.0)

    assert errors == []
    assert port.writes == [b"a\r", b"b\r"]


def test_complete_frame_arriving_after_deadline_latches_timeout() -> None:
    clock = FakeClock()

    class SlowResponseSerial(StatefulFakeSerial):
        def read_until(self, expected: bytes = b"\n") -> bytes:
            clock.sleep(0.201)
            return super().read_until(expected)

    port = SlowResponseSerial([zero_frame("A")])
    session = make_session(port, clock)

    with pytest.raises(AlicatFrameTimeout, match="exceeded"):
        session.poll("a")

    assert session.desynchronized is True
    assert port.timeout == 0.2


def test_real_hal_read_flow_timeout_is_not_published_as_zero(monkeypatch) -> None:
    port = StatefulFakeSerial([b""])
    hal = make_real_hal(monkeypatch, [port])

    with pytest.raises(AlicatFrameTimeout):
        hal.read_flow()
    with pytest.raises(AlicatSessionDesynchronized):
        hal.read_flow()

    assert port.writes == [b"a\r"]


def test_real_hal_reads_three_channel_snapshot_serially_on_one_session(monkeypatch) -> None:
    port = StatefulFakeSerial(
        [
            b"A 14.7 25.0 0.500 0.500 0.500 Air\r",
            b"B 14.7 25.0 0.000 0.000 0.000 Air\r",
            b"C 14.7 25.0 0.000 0.000 0.000 Air\r",
        ]
    )
    hal = make_real_hal(monkeypatch, [port])

    snapshot = hal.read_flow_snapshot()

    assert tuple(reading.channel for reading in snapshot.readings) == ("A", "B", "C")
    assert tuple(reading.unit_id for reading in snapshot.readings) == ("a", "b", "c")
    assert snapshot.by_channel["A"].setpoint_sccm == 500.0
    assert snapshot.by_channel["A"].mass_flow_sccm == 500.0
    assert snapshot.by_channel["A"].gas == "Air"
    assert snapshot.fresh
    assert snapshot.monotonic_ns == snapshot.readings[-1].monotonic_ns
    assert port.writes == [b"a\r", b"b\r", b"c\r"]


def test_real_hal_set_flow_consumes_command_response_before_poll(monkeypatch) -> None:
    frame = b"A 14.7 25.0 0.0 0.0 0.123 Air\r"
    port = StatefulFakeSerial([frame, frame])
    hal = make_real_hal(monkeypatch, [port])

    assert hal.set_flow("A", 123.0) is True
    assert port.writes == [b"as0.123\r", b"a\r"]
    assert hal.last_setpoint_readback_sccm("A") == 123.0


def test_real_hal_timeout_blocks_verify_poll_and_later_commands(monkeypatch) -> None:
    port = StatefulFakeSerial(
        [b"", b"A 14.7 25.0 0.0 0.0 0.123 Air\r"]
    )
    hal = make_real_hal(monkeypatch, [port])

    assert hal.set_flow("A", 123.0) is False
    assert hal.set_flow("A", 0.0) is False

    assert port.writes == [b"as0.123\r"]
    assert hal.last_setpoint_readback_sccm("A") is None


def test_real_hal_release_and_reopen_creates_fresh_synchronized_session(monkeypatch) -> None:
    first = StatefulFakeSerial([b""])
    second = StatefulFakeSerial(
        [b"A 14.7 25.0 0.0 0.0 0.0 Air\r"],
        initial_rx=b"A U\r",
    )
    hal = make_real_hal(monkeypatch, [first, second])

    with pytest.raises(AlicatFrameTimeout):
        hal.read_flow()
    hal.release_serial_resources()
    assert first.is_open is False

    assert hal.read_flow() == 0.0
    assert second.writes == [b"a\r"]


def test_real_hal_does_not_auto_reopen_desynchronized_closed_transport(
    monkeypatch,
) -> None:
    first = StatefulFakeSerial([b""])
    second = StatefulFakeSerial([zero_frame("A")])
    hal = make_real_hal(monkeypatch, [first, second])

    with pytest.raises(AlicatFrameTimeout):
        hal.read_flow()
    first.is_open = False

    with pytest.raises(AlicatSessionDesynchronized, match="explicit release"):
        hal.read_flow()
    assert second.writes == []

    hal.release_serial_resources()
    assert hal.read_flow() == 0.0
    assert second.writes == [b"a\r"]


def test_flow_service_startup_zero_keeps_b_c_a_transaction_order(monkeypatch) -> None:
    port = StatefulFakeSerial(
        [
            zero_frame("B"),
            zero_frame("B"),
            zero_frame("C"),
            zero_frame("C"),
            zero_frame("A"),
            zero_frame("A"),
        ]
    )
    hal = make_real_hal(monkeypatch, [port])

    result = FlowService(hal).apply_zero()

    assert result.success is True
    assert port.writes == [
        b"bs0.000\r",
        b"b\r",
        b"cs0.000\r",
        b"c\r",
        b"as0.000\r",
        b"a\r",
    ]


def test_flow_service_global_stop_zero_paths_use_same_transactions(monkeypatch) -> None:
    port = StatefulFakeSerial(
        [
            zero_frame("A"),
            zero_frame("A"),
            zero_frame("B"),
            zero_frame("B"),
            zero_frame("C"),
            zero_frame("C"),
            zero_frame("A"),
            zero_frame("A"),
        ]
    )
    hal = make_real_hal(monkeypatch, [port])
    service = FlowService(hal)

    a_zero = service.apply_a_zero()
    assert a_zero.success is True
    assert a_zero.a_setpoint_readback_sccm == 0.0
    assert service.apply_zero().success is True

    assert port.writes == [
        b"as0.000\r",
        b"a\r",
        b"bs0.000\r",
        b"b\r",
        b"cs0.000\r",
        b"c\r",
        b"as0.000\r",
        b"a\r",
    ]


def test_flow_worker_safe_stop_does_not_reopen_or_transmit_after_desync(
    monkeypatch,
) -> None:
    port = StatefulFakeSerial([b""])
    hal = make_real_hal(monkeypatch, [port])
    worker = FlowWorker(FlowService(hal))
    identity = SafeStopIdentity("serial-desync", 1, 1)

    a_receipt = worker.zero_a_for_safe_stop(identity, 1000)
    all_zero = worker.zero_all_for_safe_stop(identity, 1000)

    assert a_receipt is not None
    assert a_receipt.success is False
    assert all_zero is False
    assert port.writes == [b"as0.000\r"]

    result = FlowService(hal).apply_zero()
    assert result.success is False
    assert result.zero_confirmed is False
    assert result.recovery_required is True
    assert result.error == "serial_desync"
    assert port.writes == [b"as0.000\r"]


def test_config_rejects_frame_deadline_that_exceeds_global_stop_budget(
    monkeypatch,
) -> None:
    from app.services import real_hal as real_hal_module

    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    monkeypatch.setattr(real_hal_module, "_SERIAL_IMPORT_ERROR", None)

    with pytest.raises(ValueError, match="Global Stop deadline"):
        real_hal_module.RealHAL.from_config(
            {
                "serial_port": "COM-FAKE",
                "alicat_timeout_s": 0.3,
                "alicat_setpoint_verify_delay_s": 0.1,
                "actuation_shutdown_timeout_ms": 2000,
            }
        )


def test_config_rejects_startup_serial_budget_that_exceeds_connect_budget(
    monkeypatch,
) -> None:
    from app.services import real_hal as real_hal_module

    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    monkeypatch.setattr(real_hal_module, "_SERIAL_IMPORT_ERROR", None)

    with pytest.raises(ValueError, match="Connect budget"):
        real_hal_module.RealHAL.from_config(
            {
                "serial_port": "COM-FAKE",
                "alicat_timeout_s": 0.2,
                "alicat_setpoint_verify_delay_s": 0.05,
                "actuation_shutdown_timeout_ms": 2000,
                "hardware_connect_timeout_ms": 2000,
            }
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("actuation_shutdown_timeout_ms", float("nan")),
        ("actuation_shutdown_timeout_ms", float("-inf")),
        ("hardware_connect_timeout_ms", float("nan")),
        ("hardware_connect_timeout_ms", 0.0),
    ],
)
def test_config_rejects_nonfinite_or_nonpositive_owner_deadline(
    monkeypatch,
    key,
    value,
) -> None:
    from app.services import real_hal as real_hal_module

    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    monkeypatch.setattr(real_hal_module, "_SERIAL_IMPORT_ERROR", None)
    config = {
        "serial_port": "COM-FAKE",
        "actuation_shutdown_timeout_ms": 2000,
        "hardware_connect_timeout_ms": 10000,
    }
    config[key] = value

    with pytest.raises(ValueError, match=key):
        real_hal_module.RealHAL.from_config(config)


def test_invalid_alicat_unit_mapping_is_rejected_passively(monkeypatch) -> None:
    from app.services import real_hal as real_hal_module

    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    monkeypatch.setattr(real_hal_module, "_SERIAL_IMPORT_ERROR", None)

    with pytest.raises(ValueError, match="Unit ID"):
        real_hal_module.RealHAL.from_config(
            {"serial_port": "COM-FAKE", "alicat_unit_ids": {"A": "bad"}}
        )


def test_current_provisional_frame_deadline_fits_existing_safety_budgets(
    monkeypatch,
) -> None:
    from app.services import real_hal as real_hal_module

    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    monkeypatch.setattr(real_hal_module, "_SERIAL_IMPORT_ERROR", None)

    hal = real_hal_module.RealHAL.from_config(
        {
            "serial_port": "COM-FAKE",
            "alicat_timeout_s": 0.2,
            "alicat_setpoint_verify_delay_s": 0.1,
            "actuation_shutdown_timeout_ms": 2000,
            "hardware_connect_timeout_ms": 10000,
        }
    )

    assert hal.serial_timeout_s == 0.2
    assert hal.serial_resources_in_use is False


def test_commissioning_readback_tolerance_does_not_replace_global_alicat_tolerance(
    monkeypatch,
) -> None:
    from app.services import real_hal as real_hal_module

    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    monkeypatch.setattr(real_hal_module, "_SERIAL_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL.from_config(
        {
            "serial_port": "COM-FAKE",
            "alicat_setpoint_tolerance": 0.05,
            "real_supply_policy": {
                "enabled": False,
                "accepted_readback_tolerance_sccm": 1.0,
            },
        }
    )

    assert hal._setpoint_verify_tolerance == 0.05
