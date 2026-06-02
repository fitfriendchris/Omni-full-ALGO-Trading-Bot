#!/usr/bin/env python3
"""
Logging utility — sets up structured logging for the AurumFlow trading bot.

Rotates logs daily, supports both file and console output.
"""

import os
import sys
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional


def setup_logging(config: dict) -> None:
    """
    Configure root logger for AurumFlow.

    Args:
        config: Full configuration dict (logging section used).
    """
    log_cfg = config.get("logging", {})
    log_level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    log_file = log_cfg.get("file", "logs/aurumflow.log")
    max_bytes = log_cfg.get("max_bytes", 10 * 1024 * 1024)
    backup_count = log_cfg.get("backup_count", 5)
    log_format = log_cfg.get("format", "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")

    # Ensure log directory exists
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Formatter
    formatter = logging.Formatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")

    # File handler (rotating)
    try:
        file_handler = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except (IOError, PermissionError) as e:
        print(f"Warning: Could not create log file {log_file}: {e}", file=sys.stderr)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    logging.getLogger("aurumflow").info(
        f"Logging initialized: level={logging.getLevelName(log_level)}, "
        f"file={log_file}"
    )


def get_logger(name: str) -> logging.Logger:
    """Get a named child logger under the aurumflow namespace."""
    return logging.getLogger(f"aurumflow.{name}")
