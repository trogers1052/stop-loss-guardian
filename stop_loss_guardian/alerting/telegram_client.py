"""Telegram client for routine alerts (fallback channel)."""

import logging
from typing import Optional

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


class TelegramClient:
    """Client for sending Telegram messages (fallback for non-urgent alerts)."""

    def __init__(self):
        self.enabled = settings.telegram_enabled
        self.bot_token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

    async def _async_send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message via Telegram.

        Args:
            message: Message text
            parse_mode: 'HTML' or 'Markdown'

        Returns:
            True if successful
        """
        if not self.enabled:
            logger.warning("Telegram not configured")
            return False

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": message,
                        "parse_mode": parse_mode,
                    },
                    timeout=10.0,
                )

                if response.status_code == 200:
                    logger.debug(f"Telegram message sent")
                    return True
                else:
                    logger.error(f"Telegram API error: {response.status_code} - {response.text}")
                    return False

        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    def send_message_sync(self, message: str, parse_mode: str = "HTML") -> bool:
        """Synchronous version of send_message."""
        if not self.enabled:
            logger.warning("Telegram not configured")
            return False

        try:
            with httpx.Client() as client:
                response = client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": message,
                        "parse_mode": parse_mode,
                    },
                    timeout=10.0,
                )

                if response.status_code == 200:
                    logger.debug(f"Telegram message sent")
                    return True
                else:
                    logger.error(f"Telegram API error: {response.status_code} - {response.text}")
                    return False

        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False

    def send_alert(self, alert_text: str) -> bool:
        """Send an alert via Telegram with formatting."""
        return self.send_message_sync(alert_text, parse_mode="HTML")
