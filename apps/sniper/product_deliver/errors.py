"""Flow-control exceptions for the deliver run."""

from __future__ import annotations




class WatchdogTimeout(RuntimeError):
    """doc §22: one customer exceeded its execution deadline."""



class ShutdownRequested(RuntimeError):
    """doc §29: SIGINT/SIGTERM -- persist and exit cleanly."""



class DeliverError(RuntimeError):
    """Retryable flow failure with a machine-readable step."""

    def __init__(self, step: str, reason: str, retryable: bool = True):
        super().__init__(f"{step}: {reason}")
        self.step = step
        self.reason = reason
        self.retryable = retryable



class BrowserRecoveryNeeded(RuntimeError):
    """doc §21: page/browser died -- rebuild and resume."""



class StopRun(Exception):
    """Internal: max-success reached -- leave the loop gracefully and
    still run the final sanity check + report."""
