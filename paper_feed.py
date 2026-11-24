"""Reconnect public market sockets without stopping the paper process.

A socket error is logged and retried with exponential backoff. Only the stop
file or a process signal ends the loop. The supervisor never sets that flag.
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FeedSupervisor:
    def __init__(
        self,
        streams: list[tuple[str, str]],
        feed_factory: Callable[[str], Any],
        stop_path: Path,
        *,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> None:
        self.streams = streams
        self.feed_factory = feed_factory
        self.stop_path = stop_path
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self.stopping = False
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._sockets: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._backoff = {label: initial_backoff for label, _url in streams}

    def start(self) -> None:
        for label, url in self.streams:
            thread = threading.Thread(target=self._run, args=(label, url), name=f"feed-{label}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self.stopping = True
        with self._lock:
            sockets = list(self._sockets.values())
        for sock in sockets:
            _close_quietly(sock)
        for thread in self._threads:
            thread.join(timeout=2)

    def request_reconnect(self, label: str) -> None:
        with self._lock:
            sock = self._sockets.get(label)
        _close_quietly(sock)

    def _run(self, label: str, url: str) -> None:
        while not self.stopping and not self.stop_path.exists():
            sock = self.feed_factory(url)
            with self._lock:
                self._sockets[label] = sock
            try:
                sock.connect()
                self.messages.put(
                    {
                        "type": "feed_reconnect",
                        "stream": label,
                        "ts": _iso(_now_ms()),
                        "orders_sent": 0,
                    }
                )
                self._backoff[label] = self.initial_backoff
                while not self.stopping and not self.stop_path.exists():
                    text = sock.read_text(1.0)
                    if text:
                        self.messages.put({"type": "raw", "stream": label, "text": text})
            except Exception as exc:  # noqa: BLE001 - logged, then retried
                self.messages.put(
                    {
                        "type": "feed_error",
                        "stream": label,
                        "error": f"{type(exc).__name__}: {exc}",
                        "ts": _iso(_now_ms()),
                        "orders_sent": 0,
                    }
                )
            finally:
                _close_quietly(sock)
                with self._lock:
                    if self._sockets.get(label) is sock:
                        self._sockets[label] = None
            if self.stopping or self.stop_path.exists():
                break
            delay = self._backoff[label]
            self.messages.put(
                {
                    "type": "feed_disconnect",
                    "stream": label,
                    "reason": "socket_closed",
                    "retry_in_seconds": delay,
                    "ts": _iso(_now_ms()),
                    "orders_sent": 0,
                }
            )
            _sleep_until_stop(delay, self)
            self._backoff[label] = min(delay * 2, self.max_backoff)


def _close_quietly(sock: Any) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except Exception:
        return


def _sleep_until_stop(delay: float, supervisor: FeedSupervisor) -> None:
    deadline = time.time() + delay
    while time.time() < deadline:
        if supervisor.stopping or supervisor.stop_path.exists():
            return
        time.sleep(0.05)
