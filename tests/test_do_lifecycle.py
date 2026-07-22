from types import SimpleNamespace

from app.services.hal import DigitalWriteAck


def test_real_hal_prebuilds_one_task_per_device_port_and_reuses_it(monkeypatch) -> None:
    import app.services.real_hal as real_hal_module

    tasks = []
    clock_values = iter((1_000, 1_100, 2_000, 2_100))

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
            "Dev1/P0.0",
            "Dev1/P0.7",
            "Dev1/P1.0",
            "Dev1/P1.3",
            "Dev2/P0.0",
            "Dev2/P0.7",
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

    assert isinstance(first, DigitalWriteAck)
    assert (first.started_ns, first.actual_ns) == (1_000, 1_100)
    assert (second.started_ns, second.actual_ns) == (2_000, 2_100)
    assert len(tasks) == 4
    port0_task = next(task for task in tasks if task.do_target == "Dev1/port0/line0:7")
    assert port0_task.writes == [
        ([True, False, False, False, False, False, False, False], False, 0.1),
        ([True, False, False, False, False, False, False, True], False, 0.1),
    ]

    hal.flush_logs()
    assert all(task.closed is False for task in tasks)
    hal.release_do_output()
    assert all(task.closed is True for task in tasks)


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
