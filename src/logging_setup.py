"""Logging setup: rotating file handler plus console."""
import logging
import os
from logging.handlers import RotatingFileHandler

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(log_file_path: str, level: int = logging.INFO) -> None:
    """Configure the root logger. Safe to call repeatedly (no duplicate handlers)."""
    os.makedirs(os.path.dirname(os.path.abspath(log_file_path)), exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    target = os.path.abspath(log_file_path)
    for h in root.handlers:
        if getattr(h, "_monitor_bot", False) and getattr(h, "baseFilename", None) in (target, None):
            if getattr(h, "baseFilename", None) == target:
                return
    for h in [h for h in root.handlers if getattr(h, "_monitor_bot", False)]:
        root.removeHandler(h)
        h.close()
    fh = RotatingFileHandler(target, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(logging.Formatter(_FORMAT))
        h._monitor_bot = True  # type: ignore[attr-defined]
        root.addHandler(h)
