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


def test_same_level_new_condition_does_not_preempt_sticky_winner() -> None:
    coordinator = NotificationCoordinator()
    first = coordinator.publish_condition(
        source="first",
        key="first",
        title="第一个警告",
        message="请先处理",
        severity="warning",
    )
    returned = coordinator.publish_condition(
        source="second",
        key="second",
        title="第二个警告",
        message="稍后处理",
        severity="warning",
    )

    assert first is not None
    assert returned is not None and returned.identity == first.identity
    assert coordinator.current == first


def test_strict_severity_order_preempts_only_upward() -> None:
    coordinator = NotificationCoordinator()
    success = coordinator.publish_event(
        source="result",
        key="saved",
        title="已保存",
        message="保存完成",
        severity="success",
        actionable=True,
    )
    info = coordinator.publish_condition(
        source="info",
        key="info",
        title="请注意",
        message="普通信息",
        severity="info",
    )
    warning = coordinator.publish_condition(
        source="warning",
        key="warning",
        title="请检查",
        message="警告信息",
        severity="warning",
    )
    error = coordinator.publish_condition(
        source="error",
        key="error",
        title="无法继续",
        message="错误信息",
        severity="error",
    )

    assert success is not None and success.severity == "success"
    assert info is not None and info.severity == "info"
    assert warning is not None and warning.severity == "warning"
    assert error is not None and error.severity == "error"
    safety_error = coordinator.publish_condition(
        source="safety",
        key=("safety", "LOW_FLOW"),
        title="气流不足",
        message="请检查供气",
        severity="error",
    )
    assert safety_error is not None and safety_error.identity == error.identity
    critical = coordinator.publish_event(
        source="critical",
        key="stop",
        title="立即停止",
        message="需要立即处理",
        severity="critical",
        actionable=True,
    )
    assert critical is not None and critical.severity == "critical"


def test_actionable_event_dismiss_silences_backlog_until_event_clears() -> None:
    coordinator = NotificationCoordinator()
    coordinator.publish_condition(
        source="warning",
        key="warning",
        title="已有警告",
        message="请稍后处理",
        severity="warning",
    )
    winner = coordinator.publish_event(
        source="action-result",
        key="failed",
        title="操作未完成",
        message="请处理失败原因",
        severity="error",
        actionable=True,
    )
    coordinator.publish_condition(
        source="equal-error",
        key="equal-error",
        title="另一项错误",
        message="已有同级问题",
        severity="error",
    )
    assert winner is not None and coordinator.current == winner

    assert coordinator.dismiss(winner.identity) is None
    assert coordinator.publish_condition(
        source="new-warning",
        key="new-warning",
        title="另一项检查",
        message="稍后处理",
        severity="warning",
    ) is None
    higher = coordinator.publish_condition(
        source="critical",
        key="critical",
        title="需要立即处理",
        message="执行安全停止",
        severity="critical",
    )
    assert higher is not None and higher.severity == "critical"
    coordinator.resolve_condition(source="critical")
    assert coordinator.current is None
    coordinator.clear_event(source="action-result")
    assert coordinator.current is not None
    assert coordinator.current.severity == "error"


def test_resolving_winner_reconsiders_remaining_active_conditions() -> None:
    coordinator = NotificationCoordinator()
    first = coordinator.publish_condition(
        source="first",
        key="first",
        title="第一个警告",
        message="请先处理",
        severity="warning",
    )
    coordinator.publish_condition(
        source="second",
        key="second",
        title="第二个警告",
        message="仍需处理",
        severity="warning",
    )

    assert first is not None and coordinator.current == first
    remaining = coordinator.resolve_condition(source="first")
    assert remaining is not None
    assert remaining.title == "第二个警告"


def test_actionable_condition_retires_blocked_transients_without_replay() -> None:
    coordinator = NotificationCoordinator()
    coordinator.publish_event(
        source="result",
        key="old",
        title="已保存",
        message="旧结果",
        severity="success",
    )
    coordinator.publish_condition(
        source="safety",
        key="blocked",
        title="当前不可操作",
        message="请检查设备",
        severity="error",
    )
    assert "result" not in coordinator._events
    coordinator.publish_event(
        source="result",
        key="while-blocked",
        title="已结束",
        message="受阻期间结果",
        severity="info",
    )
    assert "result" not in coordinator._events
    assert coordinator.resolve_condition(source="safety") is None

    fresh = coordinator.publish_event(
        source="result",
        key="fresh",
        title="已保存",
        message="新结果",
        severity="success",
    )
    assert fresh is not None and coordinator.current == fresh


def test_dismissed_actionable_event_still_suppresses_transients() -> None:
    coordinator = NotificationCoordinator()
    actionable = coordinator.publish_event(
        source="failure",
        key="failure",
        title="操作失败",
        message="请检查设备",
        severity="error",
        actionable=True,
    )
    assert actionable is not None
    assert coordinator.dismiss(actionable.identity) is None

    assert coordinator.publish_event(
        source="result",
        key="saved",
        title="已保存",
        message="普通结果",
        severity="success",
    ) is None
    assert "result" not in coordinator._events


def test_suppressed_transient_does_not_clear_existing_actionable_same_source() -> None:
    coordinator = NotificationCoordinator()
    actionable = coordinator.publish_event(
        source="status",
        key="failure",
        title="操作失败",
        message="请检查设备",
        severity="error",
        actionable=True,
    )
    assert actionable is not None

    returned = coordinator.publish_event(
        source="status",
        key="ordinary-result",
        title="已完成",
        message="普通结果",
        severity="success",
        actionable=False,
    )

    assert returned == actionable
    assert coordinator._events["status"] == actionable
    assert coordinator.current == actionable


def test_same_identity_updates_copy_and_severity_without_new_episode() -> None:
    coordinator = NotificationCoordinator()
    first = coordinator.publish_condition(
        source="device",
        key="device-condition",
        title="请检查设备",
        message="连接不稳定",
        severity="warning",
    )
    updated = coordinator.publish_condition(
        source="device",
        key="device-condition",
        title="设备连接中断",
        message="请重新连接设备",
        severity="error",
    )
    assert first is not None and updated is not None
    assert updated.identity == first.identity
    assert updated.title == "设备连接中断"
    assert updated.message == "请重新连接设备"
    assert updated.severity == "error"
