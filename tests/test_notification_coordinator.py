from app.views.notification_coordinator import NotificationCoordinator


def test_condition_episode_dismisses_until_every_source_resolves() -> None:
    coordinator = NotificationCoordinator()
    first = coordinator.publish_condition(
        source="telemetry",
        key=("safety", "LOW_FLOW"),
        title="气流不足",
        message="请检查供气。",
        severity="error",
    )
    assert first is not None

    duplicate = coordinator.publish_condition(
        source="manual",
        key=("safety", "LOW_FLOW"),
        title="气流不足",
        message="请检查供气。",
        severity="error",
    )
    assert duplicate is not None
    assert duplicate.identity == first.identity

    assert coordinator.dismiss(first.identity) is None
    assert (
        coordinator.publish_condition(
            source="telemetry",
            key=("safety", "LOW_FLOW"),
            title="气流不足",
            message="请检查供气。",
            severity="error",
        )
        is None
    )
    assert coordinator.resolve_condition(source="telemetry") is None
    assert coordinator.resolve_condition(source="manual") is None

    next_episode = coordinator.publish_condition(
        source="telemetry",
        key=("safety", "LOW_FLOW"),
        title="气流不足",
        message="请检查供气。",
        severity="error",
    )
    assert next_episode is not None
    assert next_episode.identity != first.identity


def test_safety_condition_preempts_ordinary_and_error_events() -> None:
    coordinator = NotificationCoordinator()
    coordinator.publish_event(
        source="status",
        key="ordinary",
        title="状态",
        message="操作完成。",
        severity="info",
    )
    safety = coordinator.publish_condition(
        source="telemetry",
        key=("safety", "DATA_STALE"),
        title="设备数据中断",
        message="请检查设备连接。",
        severity="error",
    )
    assert safety is not None
    coordinator.publish_event(
        source="status",
        key="later-error",
        title="操作未完成",
        message="普通操作失败。",
        severity="error",
    )
    assert coordinator.current is not None
    assert coordinator.current.identity == safety.identity


def test_condition_recomputes_from_remaining_source_without_downgrade() -> None:
    coordinator = NotificationCoordinator()
    critical = coordinator.publish_condition(
        source="actuation",
        key=("safety", "RECOVERY_REQUIRED"),
        title="需要立即处理",
        message="执行安全恢复。",
        severity="critical",
    )
    coordinator.publish_condition(
        source="telemetry",
        key=("safety", "RECOVERY_REQUIRED"),
        title="当前不可操作",
        message="请检查设备。",
        severity="error",
    )

    assert coordinator.current is not None
    assert coordinator.current.identity == critical.identity
    assert coordinator.current.severity == "critical"
    coordinator.resolve_condition(source="actuation")
    assert coordinator.current is not None
    assert coordinator.current.severity == "error"
    assert coordinator.current.message == "请检查设备。"


def test_intervening_event_retires_old_dismissal_identity() -> None:
    coordinator = NotificationCoordinator()
    first = coordinator.publish_event(
        source="status",
        key="first",
        title="第一次",
        message="第一次事件",
    )
    assert first is not None
    assert coordinator.dismiss(first.identity) is None
    second = coordinator.publish_event(
        source="status",
        key="second",
        title="第二次",
        message="第二次事件",
    )
    assert second is not None
    repeated = coordinator.publish_event(
        source="status",
        key="first",
        title="第一次",
        message="再次发生",
    )
    assert repeated is not None
    assert repeated.identity == first.identity
