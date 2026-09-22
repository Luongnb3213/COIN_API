from __future__ import annotations

import logging
import sys
from pathlib import Path


_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_CONFIGURED = False


def _configure_root() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(level=logging.INFO, format=_FORMAT, handlers=[logging.StreamHandler(sys.stdout)])
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    return logging.getLogger(name)


def add_file_handler(path: str) -> None:
    _configure_root()
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    target = str(log_path.resolve())
    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", "") == target:
            return
    handler = logging.FileHandler(target, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
