"""Twilio client for SMS and phone call alerts."""

import logging
from typing import Optional

from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

from ..config import settings

logger = logging.getLogger(__name__)


class TwilioClient:
    """Client for sending SMS and making phone calls via Twilio."""

    def __init__(self):
        self.client: Optional[Client] = None
        self.enabled = settings.twilio_enabled

    def connect(self):
        """Initialize Twilio client."""
        if not self.enabled:
            logger.warning("Twilio not configured - SMS/phone alerts disabled")
            return

        try:
            self.client = Client(
                settings.twilio_account_sid,
                settings.twilio_auth_token,
            )
            # Verify credentials by fetching account info
            account = self.client.api.accounts(settings.twilio_account_sid).fetch()
            logger.info(f"Twilio connected - Account: {account.friendly_name}")
        except TwilioRestException as e:
            logger.error(f"Failed to connect to Twilio: {e}")
            self.enabled = False
            raise

    def send_sms(self, message: str) -> Optional[str]:
        """Send an SMS message.

        Args:
            message: Text message to send

        Returns:
            Twilio message SID if successful, None otherwise
        """
        if not self.enabled or not self.client:
            logger.warning("Twilio not enabled, cannot send SMS")
            return None

        try:
            # Truncate message if too long (SMS limit is 1600 chars for concatenated)
            if len(message) > 1500:
                message = message[:1497] + "..."

            msg = self.client.messages.create(
                body=message,
                from_=settings.twilio_phone_number,
                to=settings.alert_phone_number,
            )

            logger.info(f"SMS sent: {msg.sid}")
            return msg.sid

        except TwilioRestException as e:
            logger.error(f"Failed to send SMS: {e}")
            return None

    def make_call(self, message: str) -> Optional[str]:
        """Make a phone call with text-to-speech message.

        Args:
            message: Message to speak during the call

        Returns:
            Twilio call SID if successful, None otherwise
        """
        if not self.enabled or not self.client:
            logger.warning("Twilio not enabled, cannot make call")
            return None

        try:
            # Create TwiML for the voice message
            # Repeat the message twice for clarity
            twiml = f"""
            <Response>
                <Say voice="alice">
                    Alert from Stop Loss Guardian.
                    {message}
                </Say>
                <Pause length="2"/>
                <Say voice="alice">
                    Repeating: {message}
                </Say>
                <Pause length="1"/>
                <Say voice="alice">
                    Please check your trading app immediately.
                </Say>
            </Response>
            """

            call = self.client.calls.create(
                twiml=twiml,
                from_=settings.twilio_phone_number,
                to=settings.alert_phone_number,
            )

            logger.info(f"Phone call initiated: {call.sid}")
            return call.sid

        except TwilioRestException as e:
            logger.error(f"Failed to make call: {e}")
            return None

    def send_urgent_alert(self, message: str, include_call: bool = False) -> dict:
        """Send an urgent alert via SMS, and optionally phone call.

        Args:
            message: Alert message
            include_call: Whether to also make a phone call

        Returns:
            Dict with sms_sid and call_sid
        """
        result = {"sms_sid": None, "call_sid": None}

        # Always send SMS for urgent alerts
        result["sms_sid"] = self.send_sms(message)

        # Optionally make a call for critical alerts
        if include_call:
            # Shorten message for phone (TTS is slow)
            short_message = message[:200] if len(message) > 200 else message
            result["call_sid"] = self.make_call(short_message)

        return result

    def test_connection(self) -> bool:
        """Test Twilio connection by sending a test SMS."""
        if not self.enabled:
            return False

        try:
            msg = self.client.messages.create(
                body="Stop Loss Guardian test message - connection verified.",
                from_=settings.twilio_phone_number,
                to=settings.alert_phone_number,
            )
            logger.info(f"Test SMS sent: {msg.sid}")
            return True
        except Exception as e:
            logger.error(f"Twilio test failed: {e}")
            return False
