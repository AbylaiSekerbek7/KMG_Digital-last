from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket


logger = logging.getLogger("uto_routing.realtime")


@dataclass
class PlaybackStreamConfig:
    frame_delay_ms: int = 400


class RealtimeHub:
    def __init__(self, *, playback_config: PlaybackStreamConfig | None = None) -> None:
        self._connections: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._playback_task: asyncio.Task[None] | None = None
        self._playback_config = playback_config or PlaybackStreamConfig()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)
        await self.send(
            websocket,
            "connection",
            {
                "status": "connected",
                "connections": len(self._connections),
            },
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def send(self, websocket: WebSocket, message_type: str, payload: dict[str, Any]) -> None:
        await websocket.send_json(
            {
                "type": message_type,
                "payload": payload,
            }
        )

    async def broadcast(self, message_type: str, payload: dict[str, Any]) -> None:
        if not self._connections:
            return
        stale_connections: list[WebSocket] = []
        for websocket in list(self._connections):
            try:
                await self.send(websocket, message_type, payload)
            except Exception:  # pragma: no cover - network cleanup
                stale_connections.append(websocket)
        for websocket in stale_connections:
            self._connections.discard(websocket)

    def schedule_broadcast(self, message_type: str, payload: dict[str, Any]) -> None:
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(message_type, payload), self._loop)

    def schedule_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.schedule_broadcast("snapshot", snapshot)

    def schedule_audit(self, audit_payload: dict[str, Any]) -> None:
        self.schedule_broadcast("audit_trail", audit_payload)

    def schedule_playback_stream(self, replay_result: dict[str, Any]) -> None:
        if self._loop is None:
            return
        if self._playback_task is not None:
            self._loop.call_soon_threadsafe(self._playback_task.cancel)
        asyncio.run_coroutine_threadsafe(
            self._create_playback_task(replay_result),
            self._loop,
        )

    async def _create_playback_task(self, replay_result: dict[str, Any]) -> None:
        if self._playback_task is not None:
            self._playback_task.cancel()
        self._playback_task = asyncio.create_task(self._stream_playback(replay_result))

    async def _stream_playback(self, replay_result: dict[str, Any]) -> None:
        playback = replay_result.get("playback", {})
        frames = playback.get("frames", [])
        metadata = {
            "strategy": replay_result.get("strategy"),
            "reference_time": replay_result.get("reference_time"),
            "frame_interval_minutes": playback.get("frame_interval_minutes"),
            "start_time": playback.get("start_time"),
            "end_time": playback.get("end_time"),
            "total_frames": len(frames),
        }
        await self.broadcast("playback_started", metadata)
        try:
            for frame_index, frame in enumerate(frames):
                await self.broadcast(
                    "playback_frame",
                    {
                        **metadata,
                        "frame_index": frame_index,
                        "frame": frame,
                    },
                )
                await asyncio.sleep(self._playback_config.frame_delay_ms / 1000.0)
            await self.broadcast("playback_completed", metadata)
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            await self.broadcast("playback_stopped", metadata)
            raise

