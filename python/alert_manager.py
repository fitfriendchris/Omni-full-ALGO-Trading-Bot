#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""alert_manager.py — OMNI ICT Production Bot v28.0
Phase 5C: Multi-severity alert routing.
  CRITICAL: Immediate push (Telegram priority), logs, potentially SMS
  WARNING:  Telegram chat message
  INFO:     Dashboard logs (not Telegram to avoid spam)
"""
from __future__ import annotations
import logging
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable
from pathlib import Path

logger = logging.getLogger(__name__)


class Severity:
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"
    DEBUG = "DEBUG"


class AlertManager:
    """
    Routes alerts by severity to appropriate channels.
    
    Channels (configurable):
      - telegram_priority:  immediate push to Chris (CRITICAL)
      - telegram_chat:      normal chat messages (WARNING)
      - dashboard_log:      file append (INFO+)
      - pushover/desktop:   optional for CRITICAL (future)
    """
    
    def __init__(self, telegram_bot=None, log_dir: str = None,
                 cooldown_critical_sec: int = 300, cooldown_warning_sec: int = 60):
        """
        Args:
            telegram_bot: Instance with send_message(chat_id, text) method
            log_dir: Where to write alert_log.jsonl
        """
        self.telegram_bot = telegram_bot
        self.log_dir = Path(log_dir) if log_dir else Path.home() / "Omni-full-ALGO-Trading-Bot" / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.cooldown_critical = cooldown_critical_sec
        self.cooldown_warning = cooldown_warning_sec
        self._last_sent: Dict[str, datetime] = {}
    
    def send(self, message: str, severity: str = Severity.INFO,
             channels: Optional[List[str]] = None) -> None:
        """
        Send alert with severity-based routing.
        
        Args:
            message: Alert text
            severity: CRITICAL | WARNING | INFO | DEBUG
            channels: Override channels, e.g. ["telegram_priority", "dashboard_log"]
        """
        now = datetime.now(timezone.utc)
        
        # Deduplication / cooldown by severity+hash
        msg_key = f"{severity}:{hash(message) % 10000}"
        last = self._last_sent.get(msg_key)
        cooldown = self.cooldown_critical if severity == Severity.CRITICAL else self.cooldown_warning
        if last and (now - last).total_seconds() < cooldown:
            logger.debug(f"Alert cooldown: {message[:60]}...")
            return
        self._last_sent[msg_key] = now
        
        # Write to local log regardless
        self._log_to_file(now, severity, message)
        
        # Determine channels
        if channels is None:
            channels = self._default_channels(severity)
        
        for ch in channels:
            try:
                if ch == "telegram_priority" and self.telegram_bot:
                    self._send_telegram(message, priority=True)
                elif ch == "telegram_chat" and self.telegram_bot:
                    self._send_telegram(message, priority=False)
                elif ch == "dashboard_log":
                    pass  # Already logged above
                elif ch == "stdout":
                    print(f"[{severity}] {message}")
            except Exception as e:
                logger.error(f"Alert channel {ch} failed: {e}")
    
    def _default_channels(self, severity: str) -> List[str]:
        if severity == Severity.CRITICAL:
            return ["telegram_priority", "dashboard_log", "stdout"]
        elif severity == Severity.WARNING:
            return ["telegram_chat", "dashboard_log"]
        elif severity == Severity.INFO:
            return ["dashboard_log"]
        return ["dashboard_log"]
    
    def _send_telegram(self, message: str, priority: bool) -> None:
        """Send via Telegram bot instance."""
        if not self.telegram_bot:
            return
        # Default chat IDs — override in production
        chat_id = os.getenv("OMNI_TELEGRAM_CHAT_ID", "5786598754")
        prefix = "🚨 CRITICAL" if priority else "⚠️ WARNING"
        full_msg = f"{prefix}\n{message}\n\n— OMNI Bot {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
        try:
            self.telegram_bot.send_message(chat_id=chat_id, text=full_msg)
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
    
    def _log_to_file(self, now: datetime, severity: str, message: str) -> None:
        record = {
            "time": now.isoformat(),
            "severity": severity,
            "message": message,
        }
        path = self.log_dir / "alert_log.jsonl"
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    # Demo without Telegram
    alerts = AlertManager(telegram_bot=None)
    alerts.send("Test CRITICAL alert", Severity.CRITICAL, channels=["stdout", "dashboard_log"])
    alerts.send("Test WARNING alert", Severity.WARNING, channels=["stdout", "dashboard_log"])
    alerts.send("Test INFO alert", Severity.INFO, channels=["stdout", "dashboard_log"])
    print("Alert manager test OK")
