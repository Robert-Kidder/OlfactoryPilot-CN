from types import SimpleNamespace

import pytest

from app.services.hal import DigitalWriteAck


class FakeDaqError(RuntimeError):
    def __init__(self, message: str, *, error_code: int) -> None:
        super().__init__(message)
        self.error_code = error_code


def _install_stateful_on_demand_fake(
    monkeypatch,
    real_hal_module,
    *,
    fail_start_indices: set[int] | None = None,
):
    """Install a fake that models auto-started single-point writes as non-persistent."""

    tasks = []
    events = []
    failing = fail_start_indices or set()

    class FakeDOChannels:
        def __init__(self, task):
            self.task = task

        def add_do_chan(self, target, *, line_grouping=None):
            self.task.target = target
            events.append((self.task.index, "add", target, line_grouping))

    class StatefulOnDemandTask:
        def __init__(self):
            self.index = len(tasks)
            self.target = ""
            self.running = False
            self.closed = False
            self.last_value = None
            self.do_channels = FakeDOChannels(self)
            tasks.append(self)
            events.append((self.index, "create"))

        def write(self, value, *, auto_start=None, timeout=None):
            if self.closed:
                raise FakeDaqError("task is closed", error_code=-200088)
            events.append(
                (self.index, "write", self.target, value, auto_start, self.running, timeout)
            )
            if auto_start:
                # A single-point On-Demand auto-started write drives the value,
                # then returns the task to a non-running state. This is the
                # behavior exposed by the real C.3b-3 -200846 failure.
                self.last_value = value
                self.running = False
                return 1
            if not self.running:
                raise FakeDaqError(
                    "Write cannot be performed when auto start is false and task is not running",
                    error_code=-200846,
                )
            self.last_value = value
            return 1

        def start(self):
            if self.closed:
                raise FakeDaqError("task is closed", error_code=-200088)
            events.append((self.index, "start", self.target))
            if self.index in failing:
                raise FakeDaqError("synthetic persistent start failure", error_code=-200220)
            if self.running:
                raise FakeDaqError("task already running", error_code=-200479)
            self.running = True

        def stop(self):
            events.append((self.index, "stop", self.target))
            self.running = False

        def close(self):
            events.append((self.index, "close", self.target, self.running))
            self.running = False
            self.closed = True

    monkeypatch.setattr(
        real_hal_module,
        "nidaqmx",
        SimpleNamespace(Task=StatefulOnDemandTask),
    )
    monkeypatch.setattr(
        real_hal_module,
        "LineGrouping",
        SimpleNamespace(CHAN_FOR_ALL_LINES="ALL", CHAN_PER_LINE="PER"),
    )
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    return StatefulOnDemandTask, tasks, events


def test_stateful_fake_reproduces_legacy_on_demand_200846(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    task_type, _, _ = _install_stateful_on_demand_fake(monkeypatch, real_hal_module)
    task = task_type()
    task.do_channels.add_do_chan("Dev1/port0/line0", line_grouping="ALL")

    task.write(False, auto_start=True, timeout=1.0)

    assert task.running is False
    with pytest.raises(FakeDaqError) as error:
        task.write(False, auto_start=False, timeout=0.1)
    assert error.value.error_code == -200846


def test_real_hal_safe_write_precedes_persistent_start_and_later_write(
    monkeypatch,
) -> None:
    import app.services.real_hal as real_hal_module

    _, tasks, events = _install_stateful_on_demand_fake(monkeypatch, real_hal_module)
    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        valve_lines=["Dev1/P0.0", "Dev1/P0.1", "Dev1/P0.2"],
        digital_safe_levels={
            "Dev1/P0.0": False,
            "Dev1/P0.1": True,
            "Dev1/P0.2": False,
        },
    )

    assert hal.prepare_do_output() is True
    assert tasks[0].running is True
    assert events[:4] == [
        (0, "create"),
        (0, "add", "Dev1/port0/line0:2", "ALL"),
        (0, "write", "Dev1/port0/line0:2", 2, True, False, 1.0),
        (0, "start", "Dev1/port0/line0:2"),
    ]

    ack = hal.write_digital_ack(
        device="Dev1",
        line="P0.0",
        state=True,
        timeout_ms=100,
    )

    assert ack.success is True
    assert events[-1][1:] == (
        "write",
        "Dev1/port0/line0:2",
        3,
        False,
        True,
        0.1,
    )


def test_real_hal_persistent_start_failure_rolls_back_after_safe_image(
    monkeypatch,
) -> None:
    import app.services.real_hal as real_hal_module

    _, tasks, events = _install_stateful_on_demand_fake(
        monkeypatch,
        real_hal_module,
        fail_start_indices={0},
    )
    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        valve_lines=["Dev1/P0.0"],
    )

    assert hal.prepare_do_output() is False
    assert events[:5] == [
        (0, "create"),
        (0, "add", "Dev1/port0/line0", "ALL"),
        (0, "write", "Dev1/port0/line0", False, True, False, 1.0),
        (0, "start", "Dev1/port0/line0"),
        (0, "close", "Dev1/port0/line0", False),
    ]
    assert tasks[0].last_value is False
    assert tasks[0].closed is True
    assert hal.do_resources_in_use is False


def test_real_hal_late_port_start_failure_releases_prior_running_session(
    monkeypatch,
) -> None:
    import app.services.real_hal as real_hal_module

    _, tasks, events = _install_stateful_on_demand_fake(
        monkeypatch,
        real_hal_module,
        fail_start_indices={1},
    )
    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        valve_lines=["Dev1/P0.0", "Dev2/P1.0"],
    )

    assert hal.prepare_do_output() is False
    assert len(tasks) == 2
    assert all(task.closed for task in tasks)
    first_start = events.index((0, "start", "Dev1/port0/line0"))
    second_safe_write = next(
        index
        for index, event in enumerate(events)
        if event[:2] == (1, "write") and event[4] is True
    )
    first_close = next(
        index for index, event in enumerate(events) if event[:2] == (0, "close")
    )
    assert first_start < second_safe_write < first_close
    assert hal.do_resources_in_use is False


def test_real_hal_unexpected_task_stop_fails_without_restart_or_rewrite(
    monkeypatch,
) -> None:
    import app.services.real_hal as real_hal_module

    _, tasks, events = _install_stateful_on_demand_fake(monkeypatch, real_hal_module)
    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        valve_lines=["Dev1/P0.0"],
    )
    assert hal.prepare_do_output() is True
    task = tasks[0]
    task.stop()
    event_count = len(events)

    ack = hal.write_digital_ack(
        device="Dev1",
        line="P0.0",
        state=False,
        timeout_ms=100,
    )

    assert ack.success is False
    assert ack.uncertain is True
    assert "not running" in ack.message
    assert events[event_count:] == [
        (0, "write", "Dev1/port0/line0", False, False, False, 0.1)
    ]
    assert hal.prepare_do_output() is False
    second_ack = hal.write_digital_ack(
        device="Dev1",
        line="P0.0",
        state=False,
        timeout_ms=100,
    )
    assert second_ack.success is False
    assert second_ack.uncertain is True
    assert "不在可写运行状态" in second_ack.message
    assert events[event_count:] == [
        (0, "write", "Dev1/port0/line0", False, False, False, 0.1)
    ]
    assert sum(event[1] == "start" for event in events) == 1


def test_global_stop_style_safe_writes_finish_before_do_release(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    _, tasks, events = _install_stateful_on_demand_fake(monkeypatch, real_hal_module)
    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        valve_lines=["Dev1/P0.0", "Dev1/P0.1", "Dev1/P0.2"],
        digital_safe_levels={
            "Dev1/P0.0": False,
            "Dev1/P0.1": True,
            "Dev1/P0.2": False,
        },
    )
    assert hal.prepare_do_output() is True
    assert hal.write_digital_ack(
        device="Dev1", line="P0.0", state=True, timeout_ms=100
    ).success
    assert hal.write_digital_ack(
        device="Dev1", line="P0.2", state=True, timeout_ms=100
    ).success

    for line, safe_level in (("P0.0", False), ("P0.1", True), ("P0.2", False)):
        assert hal.write_digital_ack(
            device="Dev1",
            line=line,
            state=safe_level,
            timeout_ms=100,
        ).success
    last_safe_write = len(events) - 1

    assert hal.release_do_output() is True
    close_index = next(
        index for index, event in enumerate(events) if event[1] == "close"
    )
    assert last_safe_write < close_index
    later_writes = [event for event in events if event[1] == "write"][1:]
    assert all(event[4] is False and event[5] is True for event in later_writes)
    assert tasks[0].last_value == 2


def test_real_hal_reconnect_uses_new_running_task_after_safe_release(
    monkeypatch,
) -> None:
    import app.services.real_hal as real_hal_module

    _, tasks, events = _install_stateful_on_demand_fake(monkeypatch, real_hal_module)
    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        valve_lines=["Dev1/P0.0"],
    )
    assert hal.prepare_do_output() is True
    first = tasks[0]
    assert hal.write_digital_ack(
        device="Dev1", line="P0.0", state=False, timeout_ms=100
    ).success
    last_safe_write_index = len(events) - 1

    assert hal.release_do_output() is True
    first_close_index = next(
        index for index, event in enumerate(events) if event[1] == "close"
    )
    assert last_safe_write_index < first_close_index
    assert first.closed is True

    assert hal.prepare_do_output() is True
    assert len(tasks) == 2
    assert tasks[1] is not first
    assert tasks[1].running is True
    assert tasks[1].last_value is False
    assert sum(event[1] == "start" for event in events) == 2


def test_real_hal_safe_image_audit_is_emitted_once_per_port(
    monkeypatch,
    caplog,
) -> None:
    import app.services.real_hal as real_hal_module

    _install_stateful_on_demand_fake(monkeypatch, real_hal_module)
    clock = iter((101, 102, 201, 202))
    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        valve_lines=["Dev1/P0.0", "Dev1/P0.1", "Dev2/P1.0"],
        digital_safe_levels={
            "Dev1/P0.0": False,
            "Dev1/P0.1": True,
            "Dev2/P1.0": False,
        },
        monotonic_ns_clock=lambda: next(clock),
    )

    with caplog.at_level("INFO", logger="app.services.real_hal"):
        assert hal.prepare_do_output() is True
        assert hal.prepare_do_output() is True

    safe_lines = [line for line in caplog.messages if line.startswith("DO safe image |")]
    assert len(safe_lines) == 2
    assert any(
        "device=Dev1" in line
        and "port=port0" in line
        and "lines=0:1" in line
        and "logical_safe_states=01" in line
        and "packed=0x2" in line
        and "result=success" in line
        and "started_ns=101" in line
        and "actual_ns=102" in line
        for line in safe_lines
    )
    assert any(
        "device=Dev2" in line
        and "port=port1" in line
        and "lines=0:0" in line
        and "packed=0x0" in line
        and "result=success" in line
        for line in safe_lines
    )


def test_real_hal_release_audit_proves_safe_write_precedes_each_task_close(
    monkeypatch,
    caplog,
) -> None:
    import app.services.real_hal as real_hal_module

    _, tasks, events = _install_stateful_on_demand_fake(monkeypatch, real_hal_module)
    clock = iter((101, 102, 201, 202, 301, 302, 401, 402, 501, 502, 601, 602))
    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        valve_lines=["Dev1/P0.0", "Dev2/P1.0"],
        digital_safe_levels={"Dev1/P0.0": False, "Dev2/P1.0": False},
        monotonic_ns_clock=lambda: next(clock),
    )

    with caplog.at_level("INFO", logger="app.services.real_hal"):
        assert hal.prepare_do_output() is True
        dev1_safe_ack = hal.write_digital_ack(
            device="Dev1", line="P0.0", state=False, timeout_ms=100
        )
        dev2_safe_ack = hal.write_digital_ack(
            device="Dev2", line="P1.0", state=False, timeout_ms=100
        )
        assert dev1_safe_ack.success
        assert dev2_safe_ack.success
        assert hal.release_do_output() is True

    release_lines = [
        line for line in caplog.messages if line.startswith("DO session release |")
    ]
    assert len(release_lines) == 2
    assert any(
        "session=do-1-Dev1-port0" in line
        and "device=Dev1" in line
        and "port=port0" in line
        and "reason=owner_handoff" in line
        and "close_started_ns=501" in line
        and "close_actual_ns=502" in line
        and "result=success" in line
        for line in release_lines
    )
    assert any(
        "session=do-1-Dev2-port1" in line
        and "device=Dev2" in line
        and "port=port1" in line
        and "close_started_ns=601" in line
        and "close_actual_ns=602" in line
        and "result=success" in line
        for line in release_lines
    )
    assert dev1_safe_ack.actual_ns == 302
    assert dev2_safe_ack.actual_ns == 402
    assert dev1_safe_ack.actual_ns < 501
    assert dev2_safe_ack.actual_ns < 601
    assert [event[1] for event in events].count("close") == 2
    assert all(task.closed for task in tasks)


def test_real_hal_prebuilds_one_task_per_device_port_and_reuses_it(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    tasks = []
    clock_values = iter(
        (
            100,
            110,
            200,
            210,
            300,
            310,
            400,
            410,
            1_000,
            1_100,
            2_000,
            2_100,
            3_000,
            3_100,
            5_000,
            5_100,
            6_000,
            6_100,
            7_000,
            7_100,
            8_000,
            8_100,
        )
    )

    class FakeAIChannels:
        def add_ai_voltage_chan(self, name, *, terminal_config=None):
            return None

    class FakeDOChannels:
        def __init__(self, task):
            self.task = task

        def add_do_chan(self, target, *, line_grouping=None):
            self.task.do_target = target
            self.task.line_grouping = line_grouping

    class FakeTask:
        def __init__(self):
            self.ai_channels = FakeAIChannels()
            self.do_channels = FakeDOChannels(self)
            self.timing = SimpleNamespace(cfg_samp_clk_timing=lambda **kwargs: None)
            self.do_target = None
            self.writes = []
            self.closed = False
            tasks.append(self)

        def start(self):
            return None

        def write(self, values, *, auto_start=None, timeout=None):
            self.writes.append((values, auto_start, timeout))

        def close(self):
            self.closed = True

    monkeypatch.setattr(real_hal_module, "nidaqmx", SimpleNamespace(Task=FakeTask))
    monkeypatch.setattr(real_hal_module, "TerminalConfiguration", SimpleNamespace(RSE="RSE"))
    monkeypatch.setattr(real_hal_module, "AcquisitionType", SimpleNamespace(CONTINUOUS="CONTINUOUS"))
    monkeypatch.setattr(
        real_hal_module,
        "LineGrouping",
        SimpleNamespace(CHAN_FOR_ALL_LINES="ALL", CHAN_PER_LINE="PER"),
    )
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        valve_lines=[
            *[f"Dev1/P0.{bit}" for bit in range(8)],
            *[f"Dev1/P1.{bit}" for bit in range(4)],
            *[f"Dev2/P0.{bit}" for bit in range(8)],
            "Dev2/P1.0",
        ],
        monotonic_ns_clock=lambda: next(clock_values),
        wall_clock=lambda: 10.0,
    )
    assert hal.prepare_do_output() is True
    assert len(tasks) == 4
    assert {task.do_target for task in tasks} == {
        "Dev1/port0/line0:7",
        "Dev1/port1/line0:3",
        "Dev2/port0/line0:7",
        "Dev2/port1/line0",
    }

    first = hal.write_digital_ack(
        device="Dev1",
        line="P0.0",
        state=True,
        timeout_ms=100,
    )
    second = hal.write_digital_ack(
        device="Dev1",
        line="P0.7",
        state=True,
        timeout_ms=100,
    )
    single_line_close = hal.write_digital_ack(
        device="Dev2",
        line="P1.0",
        state=False,
        timeout_ms=100,
    )

    assert isinstance(first, DigitalWriteAck)
    assert (first.started_ns, first.actual_ns) == (1_000, 1_100)
    assert (second.started_ns, second.actual_ns) == (2_000, 2_100)
    assert (single_line_close.started_ns, single_line_close.actual_ns) == (3_000, 3_100)
    assert len(tasks) == 4
    port0_task = next(task for task in tasks if task.do_target == "Dev1/port0/line0:7")
    assert port0_task.writes == [
        (0, True, 1.0),
        (1, False, 0.1),
        (129, False, 0.1),
    ]
    single_line_task = next(task for task in tasks if task.do_target == "Dev2/port1/line0")
    assert single_line_task.writes == [
        (False, True, 1.0),
        (False, False, 0.1),
    ]

    hal.flush_logs()
    assert all(task.closed is False for task in tasks)
    assert hal.release_do_output() is True
    assert all(task.closed is True for task in tasks)


def test_real_hal_closes_current_task_when_channel_creation_fails(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    tasks = []

    class FailingChannels:
        def add_do_chan(self, target, *, line_grouping=None):
            raise RuntimeError("add failed")

    class FakeTask:
        def __init__(self):
            self.do_channels = FailingChannels()
            self.closed = False
            tasks.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(real_hal_module, "nidaqmx", SimpleNamespace(Task=FakeTask))
    monkeypatch.setattr(
        real_hal_module,
        "LineGrouping",
        SimpleNamespace(CHAN_FOR_ALL_LINES="ALL"),
    )
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(serial_port="COM1", valve_lines=["Dev1/P0.0"])

    assert hal.prepare_do_output() is False
    assert len(tasks) == 1
    assert tasks[0].closed is True


def test_real_hal_preserves_failed_rollback_task_and_blocks_rebuild(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    tasks = []

    class FailingChannels:
        def add_do_chan(self, target, *, line_grouping=None):
            raise RuntimeError("add failed")

    class FakeTask:
        def __init__(self):
            self.do_channels = FailingChannels()
            self.fail_close = True
            tasks.append(self)

        def close(self):
            if self.fail_close:
                raise RuntimeError("rollback close failed")

    monkeypatch.setattr(real_hal_module, "nidaqmx", SimpleNamespace(Task=FakeTask))
    monkeypatch.setattr(
        real_hal_module,
        "LineGrouping",
        SimpleNamespace(CHAN_FOR_ALL_LINES="ALL"),
    )
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(serial_port="COM1", valve_lines=["Dev1/P0.0"])

    assert hal.prepare_do_output() is False
    assert len(tasks) == 1
    assert hal._do_sessions
    assert hal.do_resources_in_use is True
    assert hal._do_owner_thread_id is not None
    assert hal._do_prepare_failed is True

    # The same owner must not mistake the partial session for a prepared task
    # or create a second task while the first may still reserve the device.
    assert hal.prepare_do_output() is False
    assert len(tasks) == 1

    tasks[0].fail_close = False
    assert hal.release_do_output() is True
    assert hal._do_sessions == {}
    assert hal._do_owner_thread_id is None
    assert hal.do_resources_in_use is False


def test_real_hal_preserves_owner_when_task_close_fails(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    class FakeChannels:
        def add_do_chan(self, target, *, line_grouping=None):
            return None

    class FakeTask:
        def __init__(self):
            self.do_channels = FakeChannels()
            self.fail_close = True

        def start(self):
            return None

        def write(self, value, **kwargs):
            return None

        def close(self):
            if self.fail_close:
                raise RuntimeError("still reserved")

    monkeypatch.setattr(real_hal_module, "nidaqmx", SimpleNamespace(Task=FakeTask))
    monkeypatch.setattr(
        real_hal_module,
        "LineGrouping",
        SimpleNamespace(CHAN_FOR_ALL_LINES="ALL"),
    )
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(serial_port="COM1", valve_lines=["Dev1/P0.0"])
    assert hal.prepare_do_output() is True

    assert hal.release_do_output() is False
    assert hal._do_sessions
    assert hal._do_owner_thread_id is not None

    session = next(iter(hal._do_sessions.values()))
    session.task.fail_close = False
    assert hal.release_do_output() is True
    assert hal._do_sessions == {}
    assert hal._do_owner_thread_id is None


def test_real_hal_rejects_do_write_from_non_owner_thread(monkeypatch) -> None:
    import threading

    import app.services.real_hal as real_hal_module

    class FakeChannels:
        def add_ai_voltage_chan(self, name, *, terminal_config=None):
            return None

        def add_do_chan(self, target, *, line_grouping=None):
            return None

    class FakeTask:
        def __init__(self):
            self.ai_channels = FakeChannels()
            self.do_channels = FakeChannels()
            self.timing = SimpleNamespace(cfg_samp_clk_timing=lambda **kwargs: None)

        def start(self):
            return None

        def write(self, values, **kwargs):
            return None

        def close(self):
            return None

    monkeypatch.setattr(real_hal_module, "nidaqmx", SimpleNamespace(Task=FakeTask))
    monkeypatch.setattr(real_hal_module, "TerminalConfiguration", SimpleNamespace(RSE="RSE"))
    monkeypatch.setattr(real_hal_module, "AcquisitionType", SimpleNamespace(CONTINUOUS="CONTINUOUS"))
    monkeypatch.setattr(real_hal_module, "LineGrouping", SimpleNamespace(CHAN_FOR_ALL_LINES="ALL"))
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(serial_port="COM1", valve_lines=["Dev1/P0.0"])
    hal.prepare_do_output()
    results = []

    thread = threading.Thread(
        target=lambda: results.append(
            hal.write_digital_ack(device="Dev1", line="P0.0", state=True, timeout_ms=100)
        )
    )
    thread.start()
    thread.join()

    assert results[0].success is False
    assert "所有权" in results[0].message


def test_failed_packed_close_cannot_be_reasserted_by_later_port_write(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    tasks = []
    clock_values = iter((100, 110, 1_000, 1_100, 2_000, 3_000, 3_100))

    class FakeChannels:
        def add_do_chan(self, target, *, line_grouping=None):
            return None

    class FakeTask:
        def __init__(self):
            self.do_channels = FakeChannels()
            self.writes = []
            self.fail_next = False
            tasks.append(self)

        def start(self):
            return None

        def write(self, value, **_kwargs):
            self.writes.append(value)
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("uncertain close")

        def close(self):
            return None

    monkeypatch.setattr(real_hal_module, "nidaqmx", SimpleNamespace(Task=FakeTask))
    monkeypatch.setattr(
        real_hal_module,
        "LineGrouping",
        SimpleNamespace(CHAN_FOR_ALL_LINES="ALL"),
    )
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        valve_lines=["Dev1/P0.0", "Dev1/P0.1"],
        monotonic_ns_clock=lambda: next(clock_values),
        wall_clock=lambda: 10.0,
    )
    assert hal.prepare_do_output() is True

    assert hal.write_digital_ack(
        device="Dev1", line="P0.0", state=True, timeout_ms=100
    ).success
    tasks[0].fail_next = True
    failed_close = hal.write_digital_ack(
        device="Dev1", line="P0.0", state=False, timeout_ms=100
    )
    assert failed_close.success is False
    assert failed_close.uncertain is True
    assert hal.write_digital_ack(
        device="Dev1", line="P0.1", state=False, timeout_ms=100
    ).success

    assert tasks[0].writes == [0, 1, 0, 0]


def test_failed_active_low_close_keeps_safe_level_in_next_packed_write(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    tasks = []

    class FakeChannels:
        def add_do_chan(self, target, *, line_grouping=None):
            return None

    class FakeTask:
        def __init__(self):
            self.do_channels = FakeChannels()
            self.writes = []
            self.fail_next = False
            tasks.append(self)

        def start(self):
            return None

        def write(self, value, **_kwargs):
            self.writes.append(value)
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("uncertain active-low close")

        def close(self):
            return None

    monkeypatch.setattr(real_hal_module, "nidaqmx", SimpleNamespace(Task=FakeTask))
    monkeypatch.setattr(
        real_hal_module,
        "LineGrouping",
        SimpleNamespace(CHAN_FOR_ALL_LINES="ALL"),
    )
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        valve_lines=["Dev1/P0.0", "Dev1/P0.1"],
        digital_safe_levels={"Dev1/P0.0": True, "Dev1/P0.1": False},
    )
    assert hal.prepare_do_output() is True
    assert hal.write_digital_ack(
        device="Dev1", line="P0.0", state=False, timeout_ms=100
    ).success

    tasks[0].fail_next = True
    failed_close = hal.write_digital_ack(
        device="Dev1", line="P0.0", state=True, timeout_ms=100
    )
    assert failed_close.success is False
    assert hal.write_digital_ack(
        device="Dev1", line="P0.1", state=False, timeout_ms=100
    ).success

    assert tasks[0].writes == [1, 0, 1, 1]


def test_collect_valve_lines_prepares_cross_variant_safety_union() -> None:
    import app.services.real_hal as real_hal_module

    mapping = {
        "selector": {"target": "Dev2/P1.0"},
        "variants": {
            "10-channel": {"1": "Dev1/P0.0"},
            "20-channel": {"1": "Dev2/P0.0"},
        },
    }

    assert real_hal_module._collect_valve_lines(
        mapping, hardware_variant="10-channel"
    ) == ["Dev1/P0.0", "Dev2/P0.0"]
    assert real_hal_module._collect_selector_line(mapping) == "Dev2/P1.0"


def test_real_hal_close_all_never_writes_selector_line(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    monkeypatch.setattr(real_hal_module, "_SERIAL_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        valve_lines=["Dev1/P0.0", "Dev2/P1.0"],
        odor_valve_lines=["Dev1/P0.0"],
    )
    calls = []
    monkeypatch.setattr(hal, "prepare_do_output", lambda: True)
    monkeypatch.setattr(
        hal,
        "write_digital",
        lambda *, device, line, state: calls.append((device, line, state)) or True,
    )

    assert hal.close_all()
    assert calls == [("Dev1", "P0.0", False)]


def test_real_hal_rejects_noncontiguous_packed_port_mapping(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    monkeypatch.setattr(
        real_hal_module,
        "LineGrouping",
        SimpleNamespace(CHAN_FOR_ALL_LINES="ALL"),
    )
    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        valve_lines=["Dev1/P0.0", "Dev1/P0.2"],
    )

    assert hal.prepare_do_output() is False


def test_first_do_drive_is_complete_safe_image_with_polarity(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    events = []

    class FakeChannels:
        def __init__(self, task):
            self.task = task

        def add_do_chan(self, target, *, line_grouping=None):
            self.task.target = target
            events.append(("add", target))

    class FakeTask:
        def __init__(self):
            self.target = ""
            self.do_channels = FakeChannels(self)

        def write(self, value, *, auto_start=None, timeout=None):
            events.append(("write", self.target, value, auto_start))

        def start(self):
            events.append(("start", self.target))

        def close(self):
            return None

    monkeypatch.setattr(real_hal_module, "nidaqmx", SimpleNamespace(Task=FakeTask))
    monkeypatch.setattr(
        real_hal_module,
        "LineGrouping",
        SimpleNamespace(CHAN_FOR_ALL_LINES="ALL"),
    )
    monkeypatch.setattr(real_hal_module, "_NIDAQMX_IMPORT_ERROR", None)
    hal = real_hal_module.RealHAL(
        serial_port="COM1",
        valve_lines=["Dev1/P0.0", "Dev1/P0.1", "Dev1/P0.2"],
        digital_safe_levels={
            "Dev1/P0.0": False,
            "Dev1/P0.1": True,
            "Dev1/P0.2": False,
        },
    )

    assert hal.prepare_do_output() is True
    assert events == [
        ("add", "Dev1/port0/line0:2"),
        ("write", "Dev1/port0/line0:2", 2, True),
        ("start", "Dev1/port0/line0:2"),
    ]


def test_profile_safe_image_and_pull_down_compatibility() -> None:
    import app.services.real_hal as real_hal_module

    config = {
        "serial_port": "COM1",
        "valve_mapping": {
            "variants": {"20-channel": {"1": "Dev1/P0.0", "2": "Dev1/P0.1"}},
            "selector": {"target": "Dev1/P0.2", "safe_level": False},
        },
        "hardware_profile": {
            "channels": [
                {"target": "Dev1/P0.0", "active_high": True},
                {"target": "Dev1/P0.1", "active_high": False},
            ],
            "selector": {"target": "Dev1/P0.2", "safe_level": False},
        },
    }

    levels = real_hal_module._collect_digital_safe_levels(
        config,
        odor_valve_lines=["Dev1/P0.0", "Dev1/P0.1"],
        selector_line="Dev1/P0.2",
    )

    assert levels == {
        "dev1/port0/line0": False,
        "dev1/port0/line1": True,
        "dev1/port0/line2": False,
    }

    config["hardware_profile"]["selector"]["safe_level"] = True
    selector_high_levels = real_hal_module._collect_digital_safe_levels(
        config,
        odor_valve_lines=["Dev1/P0.0", "Dev1/P0.1"],
        selector_line="Dev1/P0.2",
    )
    assert selector_high_levels["dev1/port0/line2"] is True

    config["hardware_profile"]["channels"] = config["hardware_profile"]["channels"][:1]
    legacy_levels = real_hal_module._collect_digital_safe_levels(
        config,
        odor_valve_lines=["Dev1/P0.0", "Dev1/P0.1"],
        selector_line="Dev1/P0.2",
    )
    assert legacy_levels["dev1/port0/line1"] is False
