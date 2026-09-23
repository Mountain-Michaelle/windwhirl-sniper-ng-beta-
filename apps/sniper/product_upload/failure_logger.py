"""
failure_logger.py

Implements business logic spec section 16: failed orders must NEVER
disappear. Every failure is appended to a human-readable TXT file (the
manual upload list) and a machine-readable JSONL file (for reprocessing).

Never overwrites -- always appends (business logic spec section 19).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class FailedOrder:
    customer: str
    phone: str
    address: str
    product: str
    quantity: str
    intended_price: str
    reason: str
    row_number: Optional[int] = None
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))


class FailureLogger:
    def __init__(self, output_dir: str = "recordings", session_name: Optional[str] = None):
        os.makedirs(output_dir, exist_ok=True)
        session_name = session_name or time.strftime("run_%Y%m%d_%H%M%S")
        self.txt_path = os.path.join(output_dir, f"{session_name}_failures.txt")
        self.jsonl_path = os.path.join(output_dir, f"{session_name}_failures.jsonl")
        self._count = 0

    def log(self, failure: FailedOrder) -> None:
        self._count += 1
        with open(self.txt_path, "a", encoding="utf-8") as f:
            f.write(
                "-" * 78 + "\n"
                f"Timestamp     : {failure.timestamp}\n"
                f"Row           : {failure.row_number}\n"
                f"Customer      : {failure.customer}\n"
                f"Phone         : {failure.phone}\n"
                f"Address       : {failure.address}\n"
                f"Product       : {failure.product}\n"
                f"Quantity      : {failure.quantity}\n"
                f"Intended price: {failure.intended_price}\n"
                f"Reason        : {failure.reason}\n"
            )
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(failure), ensure_ascii=False) + "\n")

    @property
    def count(self) -> int:
        return self._count