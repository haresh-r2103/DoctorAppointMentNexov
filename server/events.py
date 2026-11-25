"""
A tiny in-process pub/sub used to push live updates to the dashboard.

When a tool books or cancels an appointment it calls ``publish(...)``. Every
browser tab that has the dashboard open holds a Server-Sent Events connection
(``GET /api/events``), and each connection owns one asyncio.Queue here. That is
how a booking made by voice makes a row light up on the dashboard instantly,
in every open tab, without polling.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

_subscribers: set[asyncio.Queue[str]] = set()


def subscribe() -> asyncio.Queue[str]:
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue[str]) -> None:
    _subscribers.discard(q)


def publish(event: str, data: Any) -> None:
    payload = json.dumps({"event": event, "data": data}, default=str)
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:  # a stuck tab should never block the agent
            pass
