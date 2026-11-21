from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class CircuitBreaker:
    """closed -> (threshold failures within window) -> open for cooldown -> half-open (one probe) -> closed on success / open on failure."""

    def __init__(self, threshold: int = 3, window: float = 60.0, cooldown: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold, self.window, self.cooldown, self.clock = threshold, window, cooldown, clock
        self.failures: deque[float] = deque()
        self.opened_at: float | None = None
        self.probing = False
        self.total_failures = self.total_successes = 0

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if self.clock() - self.opened_at >= self.cooldown else "open"

    def allow(self) -> bool:
        st = self.state
        if st == "closed":
            return True
        if st == "half_open" and not self.probing:
            self.probing = True            # exactly one probe request goes through
            return True
        return False

    def success(self) -> None:
        self.total_successes += 1
        self.failures.clear()
        self.opened_at, self.probing = None, False

    def failure(self) -> None:
        self.total_failures += 1
        now = self.clock()
        if self.opened_at is not None:     # failed probe: re-open for a full cooldown
            self.opened_at, self.probing = now, False
            return
        self.failures.append(now)
        while self.failures and now - self.failures[0] > self.window:
            self.failures.popleft()
        if len(self.failures) >= self.threshold:
            self.opened_at = now
