from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass

_SEVERITY_PRIORITY = {
    "success": 0,
    "info": 10,
    "warning": 20,
    "error": 30,
    "critical": 40,
}

_NON_ACTIONABLE_DURATION_MS = {
    "success": 2500,
    "info": 3000,
    "warning": 5000,
}


@dataclass(frozen=True, slots=True)
class Notification:
    """单一通知输出所需的不可变展示数据。"""

    identity: tuple[object, ...]
    title: str
    message: str
    severity: str
    actionable: bool
    order: int

    @property
    def duration_ms(self) -> int:
        """行动型通知永不因视觉 severity 的默认时长而消失。"""

        if self.actionable:
            return -1
        return _NON_ACTIONABLE_DURATION_MS.get(self.severity, -1)

    @property
    def priority(self) -> tuple[int, int]:
        level = _SEVERITY_PRIORITY.get(self.severity, 20)
        return (level, self.order)


@dataclass(slots=True)
class _ConditionEpisode:
    episode: int
    sources: dict[str, Notification]

    @property
    def notification(self) -> Notification:
        return max(self.sources.values(), key=lambda item: item.priority)


class NotificationCoordinator:
    """按语义 episode 仲裁 condition 与一次性 event。

    condition 只有在最后一个 source 恢复后才算结束；用户关闭后，同一
    episode 的任何同态更新都不会重建。下一次从 resolved 重新进入时会获得
    新 identity。event 则由调用方提供稳定 key，同一 source 的新 event 会替换
    旧 event，避免状态消息无限堆积。
    """

    def __init__(self) -> None:
        self._sequence = 0
        self._episode_by_key: dict[Hashable, int] = {}
        self._conditions: dict[Hashable, _ConditionEpisode] = {}
        self._condition_by_source: dict[str, Hashable] = {}
        self._events: dict[str, Notification] = {}
        self._dismissed: set[tuple[object, ...]] = set()
        self._dismissed_levels: dict[tuple[object, ...], int] = {}
        self._winner_identity: tuple[object, ...] | None = None

    @property
    def current(self) -> Notification | None:
        active = self._active_notifications()
        by_identity = {notice.identity: notice for notice in active}
        self._discard_inactive_dismissals(set(by_identity))
        silence_level = max(self._dismissed_levels.values(), default=None)
        eligible = [
            notice
            for notice in active
            if notice.identity not in self._dismissed
            and (
                silence_level is None or notice.priority[0] > silence_level
            )
        ]

        winner = by_identity.get(self._winner_identity)
        if winner is not None and winner.identity not in self._dismissed:
            higher = [
                notice
                for notice in eligible
                if notice.identity != winner.identity
                and notice.priority[0] > winner.priority[0]
            ]
            if not higher:
                return winner
            winner = max(higher, key=lambda item: item.priority)
            self._winner_identity = winner.identity
            return winner

        if winner is not None:
            higher = [
                notice
                for notice in eligible
                if notice.priority[0]
                > self._dismissed_levels.get(winner.identity, winner.priority[0])
            ]
            if not higher:
                return None
            winner = max(higher, key=lambda item: item.priority)
            self._winner_identity = winner.identity
            return winner

        winner = max(eligible, key=lambda item: item.priority, default=None)
        self._winner_identity = None if winner is None else winner.identity
        return winner

    def publish_condition(
        self,
        *,
        source: str,
        key: Hashable,
        title: str,
        message: str,
        severity: str = "error",
        actionable: bool = True,
    ) -> Notification | None:
        previous_key = self._condition_by_source.get(source)
        if previous_key is not None and previous_key != key:
            self.resolve_condition(source=source, key=previous_key)

        episode = self._conditions.get(key)
        if episode is None:
            next_episode = self._episode_by_key.get(key, 0) + 1
            self._episode_by_key[key] = next_episode
            episode = _ConditionEpisode(
                episode=next_episode,
                sources={},
            )
            self._conditions[key] = episode
        identity = ("condition", key, episode.episode)
        episode.sources[source] = self._notification(
            identity,
            title,
            message,
            severity,
            actionable,
        )
        if actionable:
            self._retire_transients()
        self._condition_by_source[source] = key
        return self.current

    def resolve_condition(
        self,
        *,
        source: str,
        key: Hashable | None = None,
    ) -> Notification | None:
        active_key = self._condition_by_source.get(source)
        if active_key is None or (key is not None and key != active_key):
            return self.current
        episode = self._conditions.get(active_key)
        self._condition_by_source.pop(source, None)
        if episode is None:
            return self.current
        identity = episode.notification.identity
        episode.sources.pop(source, None)
        if not episode.sources:
            self._conditions.pop(active_key, None)
            self._dismissed.discard(identity)
            self._dismissed_levels.pop(identity, None)
            if self._winner_identity == identity:
                self._winner_identity = None
        return self.current

    def publish_event(
        self,
        *,
        source: str,
        key: Hashable,
        title: str,
        message: str,
        severity: str = "info",
        actionable: bool = False,
    ) -> Notification | None:
        identity = ("event", source, key)
        if (
            not actionable
            and severity in _NON_ACTIONABLE_DURATION_MS
            and self._has_active_actionable()
        ):
            return self.current
        if actionable:
            self._retire_transients()
        previous = self._events.get(source)
        if previous is not None and previous.identity != identity:
            self._dismissed.discard(previous.identity)
            self._dismissed_levels.pop(previous.identity, None)
            if self._winner_identity == previous.identity:
                self._winner_identity = None
        self._events[source] = self._notification(
            identity,
            title,
            message,
            severity,
            actionable,
        )
        return self.current

    def clear_event(self, *, source: str) -> Notification | None:
        event = self._events.pop(source, None)
        if event is not None:
            self._dismissed.discard(event.identity)
            self._dismissed_levels.pop(event.identity, None)
            if self._winner_identity == event.identity:
                self._winner_identity = None
        return self.current

    def retire_event(self, identity: tuple[object, ...]) -> Notification | None:
        """抑制 exact event，直到 source 清除或发布新的 semantic key。"""

        if len(identity) < 3 or identity[0] != "event":
            return self.current
        source = str(identity[1])
        event = self._events.get(source)
        if event is not None and event.identity == identity:
            self._dismissed.add(identity)
            if self._winner_identity == identity:
                self._winner_identity = None
        return self.current

    def dismiss(self, identity: tuple[object, ...]) -> Notification | None:
        active = {
            notice.identity: notice for notice in self._active_notifications()
        }
        notice = active.get(identity)
        if notice is None:
            return self.current
        self._dismissed.add(identity)
        if notice.actionable:
            self._dismissed_levels[identity] = notice.priority[0]
        return self.current

    def clear(self) -> None:
        self._conditions.clear()
        self._condition_by_source.clear()
        self._events.clear()
        self._dismissed.clear()
        self._dismissed_levels.clear()
        self._winner_identity = None

    def _retire_transients(self) -> None:
        for source, event in tuple(self._events.items()):
            if (
                not event.actionable
                and event.severity in _NON_ACTIONABLE_DURATION_MS
            ):
                self._events.pop(source, None)
                self._dismissed.discard(event.identity)
                self._dismissed_levels.pop(event.identity, None)
                if self._winner_identity == event.identity:
                    self._winner_identity = None

    def _active_notifications(self) -> list[Notification]:
        notifications = [
            episode.notification for episode in self._conditions.values()
        ]
        notifications.extend(self._events.values())
        return notifications

    def _has_active_actionable(self) -> bool:
        return any(
            notice.actionable
            for episode in self._conditions.values()
            for notice in episode.sources.values()
        ) or any(event.actionable for event in self._events.values())

    def _discard_inactive_dismissals(
        self, active_identities: set[tuple[object, ...]]
    ) -> None:
        self._dismissed.intersection_update(active_identities)
        for identity in tuple(self._dismissed_levels):
            if identity not in active_identities:
                self._dismissed_levels.pop(identity, None)

    def _notification(
        self,
        identity: tuple[object, ...],
        title: str,
        message: str,
        severity: str,
        actionable: bool,
    ) -> Notification:
        self._sequence += 1
        return Notification(
            identity=identity,
            title=str(title),
            message=str(message),
            severity=str(severity),
            actionable=bool(actionable),
            order=self._sequence,
        )
