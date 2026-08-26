from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass

_SEVERITY_PRIORITY = {
    "info": 10,
    "success": 10,
    "warning": 20,
    "error": 30,
    "critical": 40,
}


@dataclass(frozen=True, slots=True)
class Notification:
    """单一通知输出所需的不可变展示数据。"""

    identity: tuple[object, ...]
    title: str
    message: str
    severity: str
    order: int

    @property
    def priority(self) -> tuple[int, int]:
        level = _SEVERITY_PRIORITY.get(self.severity, 20)
        if (
            len(self.identity) >= 2
            and self.identity[0] == "condition"
            and isinstance(self.identity[1], tuple)
            and self.identity[1]
            and self.identity[1][0] == "safety"
        ):
            level = max(level, 35)
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

    @property
    def current(self) -> Notification | None:
        candidates = [
            episode.notification
            for episode in self._conditions.values()
            if episode.notification.identity not in self._dismissed
        ]
        candidates.extend(
            event
            for event in self._events.values()
            if event.identity not in self._dismissed
        )
        return max(candidates, key=lambda item: item.priority, default=None)

    def publish_condition(
        self,
        *,
        source: str,
        key: Hashable,
        title: str,
        message: str,
        severity: str = "error",
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
        )
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
        return self.current

    def publish_event(
        self,
        *,
        source: str,
        key: Hashable,
        title: str,
        message: str,
        severity: str = "info",
    ) -> Notification | None:
        identity = ("event", source, key)
        previous = self._events.get(source)
        if previous is not None and previous.identity != identity:
            self._dismissed.discard(previous.identity)
        self._events[source] = self._notification(
            identity,
            title,
            message,
            severity,
        )
        return self.current

    def clear_event(self, *, source: str) -> Notification | None:
        event = self._events.pop(source, None)
        if event is not None:
            self._dismissed.discard(event.identity)
        return self.current

    def dismiss(self, identity: tuple[object, ...]) -> Notification | None:
        self._dismissed.add(identity)
        return self.current

    def clear(self) -> None:
        self._conditions.clear()
        self._condition_by_source.clear()
        self._events.clear()
        self._dismissed.clear()

    def _notification(
        self,
        identity: tuple[object, ...],
        title: str,
        message: str,
        severity: str,
    ) -> Notification:
        self._sequence += 1
        return Notification(
            identity=identity,
            title=str(title),
            message=str(message),
            severity=str(severity),
            order=self._sequence,
        )
