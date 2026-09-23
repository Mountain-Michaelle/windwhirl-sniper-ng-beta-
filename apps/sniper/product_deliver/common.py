"""Small shared helpers: time, atomic JSON, fingerprints."""

from __future__ import annotations

import json
import os
import random
from datetime import (
    datetime,
    timezone,
)
from pathlib import (
    Path,
)


# ============================ small helpers =================================


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")



def atomic_write_json(path: Path, value) -> None:
    """Atomic checkpoint (doc §23): tmp write + rename, so power loss can
    never leave a half-written state file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)



def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default



def fingerprint(client: str, period: str, order_id: str) -> str:
    """doc §17: identity is client|period|orderId -- never name+amount,
    because two different orders can share both."""
    return f"{client}|{period}|{order_id}"



def backoff_delay(attempt: int) -> float:
    """doc §19: 500ms, 1s, 2s, 4s, 8s capped, with small jitter."""
    base = min(0.5 * (2 ** max(0, attempt - 1)), 8.0)
    return base + random.uniform(0.0, 0.15)
