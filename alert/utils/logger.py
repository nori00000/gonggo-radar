"""로깅 유틸리티 -- RotatingFileHandler + StreamHandler 설정."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 5


def setup_logger(
    name: str,
    log_dir: str | Path = "data/logs",
    level: int = logging.INFO,
) -> logging.Logger:
    """Create and return a logger with file rotation and console output.

    Args:
        name: Logger name (also used as the log filename stem).
        log_dir: Directory where log files are stored.
                 Created automatically if it does not exist.
        level: Logging level (default ``logging.INFO``).

    Returns:
        Configured :class:`logging.Logger` instance.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers when called multiple times.
    if logger.handlers:
        return logger

    logger.setLevel(level)

    formatter = logging.Formatter(_LOG_FORMAT)

    # --- File handler (rotating) ---
    file_handler = RotatingFileHandler(
        filename=log_dir / f"{name}.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # --- Console handler ---
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger
