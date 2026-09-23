"""Low-overhead per-order phase timing."""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class OrderMetrics:
    timings_ms: dict[str, float] = field(default_factory=dict)
    _started: dict[str, float] = field(default_factory=dict, repr=False)

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.timings_ms[name] = round(
                (time.perf_counter() - started) * 1000, 2
            )

    def total(self) -> float:
        return round(sum(self.timings_ms.values()), 2)
