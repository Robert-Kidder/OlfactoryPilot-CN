from types import SimpleNamespace

from app.services.hal import DigitalWriteAck


def test_real_hal_prebuilds_one_task_per_device_port_and_reuses_it(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    tasks = []
    clock_values = iter((1_000, 1_100, 2_000, 2_100, 3_000, 3_100))

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
        (1, False, 0.1),
        (129, False, 0.1),
    ]
    single_line_task = next(task for task in tasks if task.do_target == "Dev2/port1/line0")
    assert single_line_task.writes == [(False, False, 0.1)]

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
    clock_values = iter((1_000, 1_100, 2_000, 3_000, 3_100))

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

    assert tasks[0].writes == [1, 0, 0]


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
