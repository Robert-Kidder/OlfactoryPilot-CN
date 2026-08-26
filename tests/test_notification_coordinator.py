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


def test_actionable_is_final_duration_authority_for_every_visual_severity() -> None:
    coordinator = NotificationCoordinator()
    for severity in ("success", "info", "warning", "error"):
        notice = coordinator.publish_event(
            source=severity,
            key="action",
            title="需要处理",
            message="请人工确认。",
            severity=severity,
            actionable=True,
        )
        assert notice is not None
        assert notice.actionable
        assert notice.duration_ms == -1


def test_non_actionable_duration_policy_and_exact_event_retirement() -> None:
    coordinator = NotificationCoordinator()
    expected = {"success": 2500, "info": 3000, "warning": 5000, "error": -1}
    notices = {}
    for severity, duration in expected.items():
        notice = coordinator.publish_event(
            source=severity,
            key="ordinary",
            title="状态",
            message="操作结果。",
            severity=severity,
            actionable=False,
        )
        assert notice is not None
        event = coordinator._events[severity]
        assert event.duration_ms == duration
        notices[severity] = event

    retirement = NotificationCoordinator()
    original = retirement.publish_event(
        source="result",
        key="completed",
        title="已完成",
        message="普通结果。",
        severity="success",
        actionable=False,
    )
    assert original is not None
    retirement.retire_event(original.identity)
    assert retirement.current is None
    assert (
        retirement.publish_event(
            source="result",
            key="completed",
            title="已完成",
            message="普通结果。",
            severity="success",
            actionable=False,
        )
        is None
    )
    replacement = retirement.publish_event(
        source="result",
        key="saved",
        title="已保存",
        message="新的普通结果。",
        severity="success",
        actionable=False,
    )
    assert replacement is not None
    assert replacement.identity != original.identity
    retirement.retire_event(replacement.identity)
    assert retirement.current is None
    retirement.clear_event(source="result")
    repeated_after_clear = retirement.publish_event(
        source="result",
        key="saved",
        title="已保存",
        message="新的普通结果。",
        severity="success",
        actionable=False,
    )
    assert repeated_after_clear is not None
    assert repeated_after_clear.identity == replacement.identity

    coordinator.retire_event(("event", "warning", "stale-key"))
    assert coordinator._events["warning"] == notices["warning"]


def test_transient_timeout_never_resolves_or_hides_actionable_condition() -> None:
    coordinator = NotificationCoordinator()
    condition = coordinator.publish_condition(
        source="safety",
        key=("safety", "LOW_FLOW"),
        title="气流不足",
        message="请检查供气。",
        severity="warning",
    )
    transient = coordinator.publish_event(
        source="result",
        key="done",
        title="已保存",
        message="普通结果。",
        severity="success",
        actionable=False,
    )
    assert condition is not None and transient is not None
    assert coordinator.current == condition
    coordinator.retire_event(transient.identity)
    assert coordinator.current == condition
