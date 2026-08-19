"""Clock abstraction.

Every wait in CrSDKPy goes through a clock so that tests can reproduce a 28
second reconnect without spending 28 seconds. The simulator drives a
:class:`VirtualClock`; a native backend uses :class:`RealClock`.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

__all__ = ["Clock", "RealClock", "VirtualClock"]


class Clock:
    """Monotonic millisecond clock with a sleep primitive."""

    def now_ms(self) -> int:  # pragma: no cover - interface
        raise NotImplementedError

    def sleep_ms(self, duration_ms: float) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def is_virtual(self) -> bool:
        return False


class RealClock(Clock):
    """Wall-clock implementation used with real hardware."""

    def __init__(self) -> None:
        self._origin = time.monotonic()

    def now_ms(self) -> int:
        return int((time.monotonic() - self._origin) * 1000)

    def sleep_ms(self, duration_ms: float) -> None:
        if duration_ms > 0:
            time.sleep(duration_ms / 1000.0)


class VirtualClock(Clock):
    """Deterministic clock whose time only moves when advanced.

    Sleeping is instantaneous: it advances the clock and runs any callbacks
    registered for the elapsed interval. This makes long vendor latencies free
    to test and removes timing flakiness entirely.
    """

    def __init__(self, start_ms: int = 0) -> None:
        self._now = int(start_ms)
        self._lock = threading.RLock()
        self._listeners: list[Callable[[int], None]] = []

    @property
    def is_virtual(self) -> bool:
        return True

    def now_ms(self) -> int:
        with self._lock:
            return self._now

    def sleep_ms(self, duration_ms: float) -> None:
        self.advance(duration_ms)

    def advance(self, duration_ms: float) -> int:
        """Move time forward and notify listeners of the new time."""
        step = max(0, int(duration_ms))
        with self._lock:
            self._now += step
            now = self._now
            listeners = list(self._listeners)
        for listener in listeners:
            listener(now)
        return now

    def advance_to(self, target_ms: int) -> int:
        with self._lock:
            delta = int(target_ms) - self._now
        return self.advance(delta) if delta > 0 else self.now_ms()

    def add_listener(self, listener: Callable[[int], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[int], None]) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)


def default_clock(virtual: Optional[bool] = None) -> Clock:
    """Return a virtual clock when asked, otherwise a real one."""
    return VirtualClock() if virtual else RealClock()
