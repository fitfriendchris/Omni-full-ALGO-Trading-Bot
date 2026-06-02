#!/usr/bin/env python3
"""
Configuration loader — loads YAML config with environment variable overrides.

Supports AURUM_MT5_LOGIN, AURUM_MT5_PASSWORD, and AURUM_CONFIG_PATH overrides.
"""

import os
import yaml
import logging
from typing import Dict, Any
from copy import deepcopy

logger = logging.getLogger("aurumflow.config")


def load_config(config_path: str = None) -> Dict[str, Any]:
    """
    Load configuration from YAML file with env var overrides.

    Resolution order:
    1. Default config bundled with the project
    2. Override file from AURUM_CONFIG_PATH env var (if set)
    3. CLI --config argument (highest priority file)
    4. Environment variable overrides (AURUM_MT5_LOGIN, AURUM_MT5_PASSWORD)

    Args:
        config_path: Optional explicit path to config file.
    Returns:
        Dict with full configuration.
    """
    # Determine which config file to load
    candidates = []

    # Built-in default
    default_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config", "default.yaml")
    candidates.append(default_path)

    # Environment override
    env_path = os.environ.get("AURUM_CONFIG_PATH")
    if env_path:
        candidates.append(env_path)

    # Explicit argument (highest priority file)
    if config_path and config_path != default_path:
        candidates.append(config_path)

    # Load first existing file
    loaded_path = None
    for path in candidates:
        if os.path.exists(path):
            loaded_path = path
            break

    if loaded_path is None:
        raise FileNotFoundError(
            f"No config file found. Looked at: {candidates}"
        )

    with open(loaded_path, "r") as f:
        config = yaml.safe_load(f)

    logger.info(f"Loaded config from {loaded_path}")

    # Apply env var overrides
    config = _apply_env_overrides(config)

    return config


def _apply_env_overrides(config: Dict[str, Any]) -> Dict[str, Any]:
    """Apply environment variable overrides to config."""
    config = deepcopy(config)

    # MT5 credentials
    env_login = os.environ.get("AURUM_MT5_LOGIN")
    env_password = os.environ.get("AURUM_MT5_PASSWORD")
    env_server = os.environ.get("AURUM_MT5_SERVER")

    mt5_cfg = config.setdefault("mt5", {})
    if env_login:
        try:
            mt5_cfg["login"] = int(env_login)
        except ValueError:
            logger.warning(f"AURUM_MT5_LOGIN is not a valid integer: {env_login}")
    if env_password:
        mt5_cfg["password"] = env_password
    if env_server:
        mt5_cfg["server"] = env_server

    # Trading kill switch
    env_enabled = os.environ.get("AURUM_TRADING_ENABLED")
    if env_enabled is not None:
        config.setdefault("trading", {})["enabled"] = env_enabled.lower() in ("1", "true", "yes")

    # Log level
    env_log_level = os.environ.get("AURUM_LOG_LEVEL")
    if env_log_level:
        config.setdefault("logging", {})["level"] = env_log_level.upper()

    return config


def merge_config(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge override into base config."""
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = value
    return result