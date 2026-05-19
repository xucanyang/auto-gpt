from __future__ import annotations

from collections import deque
import queue
import threading
from datetime import datetime
from typing import Callable


def _format_log_line(message: str) -> str:
    text = str(message or "").strip()
    return f"[{datetime.now().strftime('%H:%M:%S')}] {text}" if text else ""


class PipelineLogBus:
    """In-memory log bus with snapshot history and live subscriptions."""

    def __init__(self, limit: int = 500) -> None:
        self.limit = limit
        self.buffer: deque[str] = deque(maxlen=limit)
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[str]] = set()
        self._persist_callback: Callable[[str], None] | None = None

    def publish(self, message: str) -> None:
        line = _format_log_line(message)
        if not line:
            return
        with self._lock:
            self.buffer.append(line)
            stale: list[queue.Queue[str]] = []
            for subscriber in self._subscribers:
                try:
                    subscriber.put_nowait(line)
                except Exception:
                    stale.append(subscriber)
            for subscriber in stale:
                self._subscribers.discard(subscriber)
        if callable(self._persist_callback):
            try:
                self._persist_callback(line)
            except Exception:
                pass

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.buffer)

    def subscribe(self, *, replay: bool = True, max_queue_size: int = 1000) -> queue.Queue[str]:
        subscriber: queue.Queue[str] = queue.Queue(maxsize=max(1, int(max_queue_size or 1)))
        with self._lock:
            self._subscribers.add(subscriber)
            history = list(self.buffer) if replay else []
        for line in history:
            try:
                subscriber.put_nowait(line)
            except Exception:
                break
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[str]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def set_persist_callback(self, callback: Callable[[str], None] | None) -> None:
        with self._lock:
            self._persist_callback = callback
