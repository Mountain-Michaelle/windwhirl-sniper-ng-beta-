import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

# Track whether the root OMS logger has been configured
# Prevents duplicate handlers if get_logger is called multiple times
_configured = False


def get_logger(name: str, log_dir: Optional[str] = None) -> logging.Logger:
    '''
    Get a named logger for an OMS module.

    First call configures the root "oms" logger with console and
    rotating file handlers. All subsequent calls return child loggers
    that inherit those handlers automatically.

    Usage in any OMS module:
        from app.oms.shared.logger import get_logger
        log = get_logger(__name__)
        log.info("Order received")
        log.warning("Duplicate detected")
        log.error("Browser failed", exc_info=True)

    Args:
        name:    Module name. Always pass __name__ for correct attribution.
        log_dir: Optional path to log directory.
                 Defaults to "logs/" relative to working directory.
                 Only used on first call — subsequent calls ignore it.

    Returns:
        A configured logging.Logger instance.
    '''
    global _configured

    if not _configured:
        _configure_root_logger(log_dir)
        _configured = True

    # Return a child logger named "oms.{module_name}"
    # e.g. "oms.app.oms.domain.entities"
    # This appears in log files so you can filter by module
    if name.startswith("app.oms."):
        logger_name = "oms." + name[len("app.oms."):]
    elif name == "__main__":
        logger_name = "oms.main"
    else:
        logger_name = f"oms.{name}"

    return logging.getLogger(logger_name)


def _configure_root_logger(log_dir: Optional[str] = None):
    '''
    Configure the root "oms" logger with console and file handlers.
    Called once automatically by get_logger() on first use.
    '''
    log_path = Path(log_dir) if log_dir else Path("logs")
    log_path.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("oms")
    root.setLevel(logging.DEBUG)

    # Prevent propagation to root Python logger
    # (avoids duplicate output if other loggers are configured)
    root.propagate = False

    # ── Console handler ─────────────────────────────────────────
    # INFO and above — clean, readable output for the operator
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%H:%M:%S"
    ))
    root.addHandler(console)

    # ── Rotating file handler ───────────────────────────────────
    # DEBUG and above — full trace for troubleshooting
    # 5MB per file, keeps 5 backups → up to 30MB of logs retained
    file_handler = logging.handlers.RotatingFileHandler(
        log_path / "oms.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root.addHandler(file_handler)