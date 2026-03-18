from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from uto_routing.storage import ApplicationStore


@dataclass
class AuditEvent:
    event_id: str
    timestamp: datetime
    action: str
    strategy: str | None
    summary: str
    request: dict[str, Any]
    response: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "strategy": self.strategy,
            "summary": self.summary,
            "request": self.request,
            "response": self.response,
        }


class AuditTrailStore:
    def __init__(self, max_entries: int = 200, *, backend: ApplicationStore | None = None) -> None:
        self._events: deque[AuditEvent] = deque(maxlen=max_entries)
        self._backend = backend

    def record(
        self,
        *,
        action: str,
        summary: str,
        request: dict[str, Any],
        response: dict[str, Any],
        strategy: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=str(uuid4()),
            timestamp=datetime.now(UTC),
            action=action,
            strategy=strategy,
            summary=summary,
            request=request,
            response=response,
        )
        self._events.appendleft(event)
        if self._backend is not None:
            self._backend.record_audit_event(
                event_id=event.event_id,
                timestamp=event.timestamp.isoformat(),
                action=event.action,
                strategy=event.strategy,
                summary=event.summary,
                request=event.request,
                response=event.response,
            )
        return event

    def list(self, *, limit: int = 50, action: str | None = None) -> list[dict[str, Any]]:
        if self._backend is not None:
            return self._backend.list_audit_events(limit=limit, action=action)
        events = list(self._events)
        if action is not None:
            events = [event for event in events if event.action == action]
        return [event.to_dict() for event in events[:limit]]

    def latest(self, action: str | None = None) -> dict[str, Any] | None:
        listed = self.list(limit=1, action=action)
        return listed[0] if listed else None

    def clear(self) -> None:
        self._events.clear()
        if self._backend is not None:
            self._backend.clear_audit_events()

