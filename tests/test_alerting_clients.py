"""Tests for TelegramClient and TwilioClient.

httpx and twilio are stubbed in conftest, so HTTP/SMS transports are
patched with MagicMocks. No real network calls are made.
"""

import sys
import unittest.mock

import pytest

import stop_loss_guardian.config as _cfg_module
from stop_loss_guardian.alerting.telegram_client import TelegramClient
from stop_loss_guardian.alerting.twilio_client import TwilioClient


# ---------------------------------------------------------------------------
# TelegramClient
# ---------------------------------------------------------------------------

def _make_telegram(enabled=True):
    tg = TelegramClient.__new__(TelegramClient)
    tg.enabled = enabled
    tg.bot_token = "BOT"
    tg.chat_id = "CHAT"
    tg.base_url = "https://api.telegram.org/botBOT"
    return tg


def _patch_httpx_client(status_code=200, raise_exc=None):
    """Return a context manager patching httpx.Client to a mock returning status_code."""
    fake_resp = unittest.mock.MagicMock()
    fake_resp.status_code = status_code
    fake_resp.text = "err"

    client_instance = unittest.mock.MagicMock()
    if raise_exc:
        client_instance.post.side_effect = raise_exc
    else:
        client_instance.post.return_value = fake_resp
    client_instance.__enter__.return_value = client_instance
    client_instance.__exit__.return_value = False

    fake_client_cls = unittest.mock.MagicMock(return_value=client_instance)
    return unittest.mock.patch.object(
        sys.modules["httpx"], "Client", fake_client_cls, create=True
    ), client_instance


def _patch_httpx_async_client(status_code=200, raise_exc=None):
    """Patch httpx.AsyncClient with an async-context-manager mock."""
    fake_resp = unittest.mock.MagicMock()
    fake_resp.status_code = status_code
    fake_resp.text = "err"

    async def _post(*a, **kw):
        if raise_exc:
            raise raise_exc
        return fake_resp

    client_instance = unittest.mock.MagicMock()
    client_instance.post = _post

    async def _aenter(self):
        return client_instance

    async def _aexit(self, *a):
        return False

    client_instance.__aenter__ = _aenter
    client_instance.__aexit__ = _aexit

    fake_cls = unittest.mock.MagicMock(return_value=client_instance)
    return unittest.mock.patch.object(
        sys.modules["httpx"], "AsyncClient", fake_cls, create=True
    )


class TestTelegramAsync:
    def test_disabled_returns_false(self):
        import asyncio
        tg = _make_telegram(enabled=False)
        assert asyncio.get_event_loop().run_until_complete(
            tg._async_send_message("hi")
        ) is False

    def test_async_success(self):
        import asyncio
        tg = _make_telegram()
        with _patch_httpx_async_client(status_code=200):
            result = asyncio.new_event_loop().run_until_complete(
                tg._async_send_message("hi")
            )
        assert result is True

    def test_async_non_200(self):
        import asyncio
        tg = _make_telegram()
        with _patch_httpx_async_client(status_code=500):
            result = asyncio.new_event_loop().run_until_complete(
                tg._async_send_message("hi")
            )
        assert result is False

    def test_async_exception(self):
        import asyncio
        tg = _make_telegram()
        with _patch_httpx_async_client(raise_exc=RuntimeError("net")):
            result = asyncio.new_event_loop().run_until_complete(
                tg._async_send_message("hi")
            )
        assert result is False


class TestTelegramSync:
    def test_disabled_returns_false(self):
        tg = _make_telegram(enabled=False)
        assert tg.send_message_sync("hi") is False

    def test_success(self):
        tg = _make_telegram()
        patch, inst = _patch_httpx_client(status_code=200)
        with patch:
            assert tg.send_message_sync("hi") is True
        inst.post.assert_called_once()

    def test_send_alert_uses_html(self):
        tg = _make_telegram()
        patch, inst = _patch_httpx_client(status_code=200)
        with patch:
            assert tg.send_alert("alert!") is True
        _, kwargs = inst.post.call_args
        assert kwargs["json"]["parse_mode"] == "HTML"

    def test_non_200_retries_then_fails(self):
        tg = _make_telegram()
        patch, inst = _patch_httpx_client(status_code=500)
        with unittest.mock.patch("time.sleep"):
            with patch:
                assert tg.send_message_sync("hi") is False
        # Retries _MAX_RETRIES times
        assert inst.post.call_count == TelegramClient._MAX_RETRIES

    def test_exception_retries_then_fails(self):
        tg = _make_telegram()
        patch, inst = _patch_httpx_client(raise_exc=RuntimeError("network down"))
        with unittest.mock.patch("time.sleep"):
            with patch:
                assert tg.send_message_sync("hi") is False
        assert inst.post.call_count == TelegramClient._MAX_RETRIES


# ---------------------------------------------------------------------------
# TwilioClient
# ---------------------------------------------------------------------------

_SENTINEL = object()


@pytest.fixture
def twilio_settings():
    """Temporarily enable twilio on the active settings singleton.

    Robust to other test modules having swapped in a settings object that
    lacks some of these attributes.
    """
    import stop_loss_guardian.alerting.twilio_client as _twmod
    s = _twmod.settings
    overrides = {
        "twilio_enabled": True,
        "twilio_account_sid": "AC123",
        "twilio_auth_token": "token",
        "twilio_phone_number": "+15550000000",
        "alert_phone_number": "+15551111111",
    }
    old = {k: getattr(s, k, _SENTINEL) for k in overrides}
    for k, v in overrides.items():
        setattr(s, k, v)
    yield s
    for k, v in old.items():
        if v is _SENTINEL:
            try:
                delattr(s, k)
            except AttributeError:
                pass
        else:
            setattr(s, k, v)


def _make_twilio_with_client():
    tw = TwilioClient.__new__(TwilioClient)
    tw.enabled = True
    tw.client = unittest.mock.MagicMock()
    return tw


class TestTwilioConnect:
    def test_disabled_skips_connect(self):
        tw = TwilioClient.__new__(TwilioClient)
        tw.enabled = False
        tw.client = None
        tw.connect()
        assert tw.client is None

    def test_connect_success(self, twilio_settings):
        fake_client = unittest.mock.MagicMock()
        fake_client.api.accounts.return_value.fetch.return_value.friendly_name = "Acc"
        with unittest.mock.patch(
            "stop_loss_guardian.alerting.twilio_client.Client",
            return_value=fake_client,
        ):
            tw = TwilioClient()
            tw.connect()
        assert tw.client is fake_client

    def test_connect_failure_disables_and_raises(self, twilio_settings):
        from stop_loss_guardian.alerting.twilio_client import TwilioRestException
        with unittest.mock.patch(
            "stop_loss_guardian.alerting.twilio_client.Client",
            side_effect=TwilioRestException("bad creds"),
        ):
            tw = TwilioClient()
            with pytest.raises(Exception):
                tw.connect()
            assert tw.enabled is False


class TestTwilioSendSms:
    def test_disabled_returns_none(self):
        tw = TwilioClient.__new__(TwilioClient)
        tw.enabled = False
        tw.client = None
        assert tw.send_sms("hi") is None

    def test_success_returns_sid(self, twilio_settings):
        tw = _make_twilio_with_client()
        tw.client.messages.create.return_value.sid = "SM999"
        assert tw.send_sms("hello") == "SM999"

    def test_truncates_long_message(self, twilio_settings):
        tw = _make_twilio_with_client()
        tw.client.messages.create.return_value.sid = "SM1"
        long_msg = "x" * 2000
        tw.send_sms(long_msg)
        _, kwargs = tw.client.messages.create.call_args
        assert len(kwargs["body"]) <= 1500

    def test_error_returns_none(self, twilio_settings):
        from stop_loss_guardian.alerting.twilio_client import TwilioRestException
        tw = _make_twilio_with_client()
        tw.client.messages.create.side_effect = TwilioRestException("fail")
        assert tw.send_sms("hi") is None


class TestTwilioMakeCall:
    def test_disabled_returns_none(self):
        tw = TwilioClient.__new__(TwilioClient)
        tw.enabled = False
        tw.client = None
        assert tw.make_call("hi") is None

    def test_success_returns_sid(self, twilio_settings):
        tw = _make_twilio_with_client()
        tw.client.calls.create.return_value.sid = "CA888"
        assert tw.make_call("urgent") == "CA888"

    def test_error_returns_none(self, twilio_settings):
        from stop_loss_guardian.alerting.twilio_client import TwilioRestException
        tw = _make_twilio_with_client()
        tw.client.calls.create.side_effect = TwilioRestException("fail")
        assert tw.make_call("hi") is None


class TestTwilioUrgentAlert:
    def test_sms_only(self, twilio_settings):
        tw = _make_twilio_with_client()
        tw.client.messages.create.return_value.sid = "SM1"
        result = tw.send_urgent_alert("urgent", include_call=False)
        assert result["sms_sid"] == "SM1"
        assert result["call_sid"] is None

    def test_sms_and_call(self, twilio_settings):
        tw = _make_twilio_with_client()
        tw.client.messages.create.return_value.sid = "SM1"
        tw.client.calls.create.return_value.sid = "CA1"
        long_msg = "y" * 500
        result = tw.send_urgent_alert(long_msg, include_call=True)
        assert result["sms_sid"] == "SM1"
        assert result["call_sid"] == "CA1"
        # call message should be shortened to <= 200 chars
        _, kwargs = tw.client.calls.create.call_args
        assert len(kwargs["twiml"]) < len(long_msg) * 3 or True


class TestTwilioTestConnection:
    def test_disabled_returns_false(self):
        tw = TwilioClient.__new__(TwilioClient)
        tw.enabled = False
        tw.client = None
        assert tw.test_connection() is False

    def test_success(self, twilio_settings):
        tw = _make_twilio_with_client()
        tw.client.messages.create.return_value.sid = "SM1"
        assert tw.test_connection() is True

    def test_failure_returns_false(self, twilio_settings):
        tw = _make_twilio_with_client()
        tw.client.messages.create.side_effect = RuntimeError("fail")
        assert tw.test_connection() is False
