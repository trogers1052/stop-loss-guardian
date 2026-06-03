"""Telegram client for routine alerts (fallback channel).

Thin guardian-specific adapter over the shared
:class:`trading_commons.telegram.TelegramClient`. The library provides the
sync + async send, exponential-backoff retry, and the no-op-without-creds
behaviour; this subclass only:

- constructs itself from the guardian ``settings`` singleton (no-arg ctor),
- keeps the guardian's historical method names (``send_message_sync``,
  ``_async_send_message``, ``send_alert``) so call-sites stay unchanged,
- defaults to HTML parse mode, matching the previous httpx-based client.
"""

import logging
from typing import Optional

from trading_commons.telegram import TelegramClient as _BaseTelegramClient

from ..config import settings

logger = logging.getLogger(__name__)

# Retry/backoff parity with the previous implementation:
# 3 total attempts with 1s, 2s, 4s backoff (backoff_base * 2**(n-1)).
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 1.0


class TelegramClient(_BaseTelegramClient):
    """Client for sending Telegram messages (fallback for non-urgent alerts)."""

    # Preserved as a class attribute for callers/tests that reference it.
    _MAX_RETRIES = _MAX_RETRIES
    _RETRY_BACKOFF_SECONDS = [1, 2, 4]

    def __init__(self):
        super().__init__(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            max_retries=_MAX_RETRIES,
            backoff_base=_RETRY_BACKOFF_BASE,
        )

    async def _async_send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message via Telegram (async). Returns True on success."""
        return await self.send_message_async(message, parse_mode=parse_mode)

    def send_message_sync(self, message: str, parse_mode: str = "HTML") -> bool:
        """Synchronous send with exponential-backoff retry."""
        return self.send_message(message, parse_mode=parse_mode)

    def send_alert(self, alert_text: str) -> bool:
        """Send an alert via Telegram with HTML formatting."""
        return self.send_message_sync(alert_text, parse_mode="HTML")
