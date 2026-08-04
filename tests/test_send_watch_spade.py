"""
Tests for the Spade-based watch payload delivery.

Twitch stopped counting watch minutes sent through the `sendSpadeEvents` GQL
mutation, so watch events have to be POSTed directly to the Spade tracking URL.
These tests pin that behaviour down so the miner cannot silently regress to
"watching" a channel without ever advancing a drop.
"""

import base64
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.exceptions import MinerException, RequestException
from src.models.channel import Channel, Stream


class _FakeResponse:
    """Minimal stand-in for an aiohttp response used as an async context manager."""

    def __init__(self, status: int = 204, text: str = ""):
        self.status = status
        self._text = text

    async def text(self, encoding: str = "utf8") -> str:
        return self._text

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


def _make_channel(twitch: MagicMock, login: str = "somestreamer") -> Channel:
    return Channel(twitch, id=123, login=login)


def _make_twitch() -> MagicMock:
    twitch = MagicMock()
    twitch.gui.channels = MagicMock()
    twitch._auth_state.user_id = 491426818
    twitch._client_type.CLIENT_URL = "https://www.twitch.tv"
    return twitch


class TestSpadePayload(unittest.TestCase):
    def setUp(self):
        self.twitch = _make_twitch()
        self.channel = _make_channel(self.twitch)
        self.stream = Stream(
            self.channel,
            id=987654,
            game={"id": "515025", "name": "Overwatch"},
            viewers=100,
            title="stream title",
        )

    def test_spade_payload_is_plain_base64_json(self):
        """Spade expects base64(JSON) under a "data" key - NOT gzipped like the GQL route."""
        payload = self.stream.spade_payload

        self.assertIn("data", payload)
        decoded = json.loads(base64.b64decode(payload["data"]))
        self.assertIsInstance(decoded, list)
        self.assertEqual(decoded[0]["event"], "minute-watched")

    def test_spade_payload_carries_channel_and_broadcast_identity(self):
        decoded = json.loads(base64.b64decode(self.stream.spade_payload["data"]))
        props = decoded[0]["properties"]

        self.assertEqual(props["broadcast_id"], "987654")
        self.assertEqual(props["channel_id"], "123")
        self.assertEqual(props["channel"], "somestreamer")
        self.assertEqual(props["game"], "Overwatch")
        self.assertEqual(props["user_id"], 491426818)


class TestGetSpadeUrl(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.twitch = _make_twitch()
        self.channel = _make_channel(self.twitch)

    async def test_extracts_spade_url_directly_from_page(self):
        page = 'blah "spade_url": "https://spade.twitch.tv/track" blah'
        self.twitch.request = MagicMock(return_value=_FakeResponse(200, page))

        url = await self.channel.get_spade_url()

        self.assertEqual(url, "https://spade.twitch.tv/track")

    async def test_falls_back_to_settings_js(self):
        settings_url = "https://assets.twitch.tv/config/settings." + "a" * 32 + ".js"
        page = f'<script src="{settings_url}"></script>'
        settings_js = '"spade_url":"https://spade.twitch.tv/track"'
        self.twitch.request = MagicMock(
            side_effect=[_FakeResponse(200, page), _FakeResponse(200, settings_js)]
        )

        url = await self.channel.get_spade_url()

        self.assertEqual(url, "https://spade.twitch.tv/track")

    async def test_raises_when_spade_url_cannot_be_found(self):
        self.twitch.request = MagicMock(return_value=_FakeResponse(200, "nothing here"))

        with self.assertRaises(MinerException):
            await self.channel.get_spade_url()


class TestSendWatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.twitch = _make_twitch()
        self.channel = _make_channel(self.twitch)
        self.channel._stream = Stream(
            self.channel,
            id=987654,
            game={"id": "515025", "name": "Overwatch"},
            viewers=100,
            title="stream title",
        )

    async def test_posts_watch_payload_to_spade_url(self):
        """The whole point: a watch event must go to Spade, not through GQL."""
        self.twitch.request = MagicMock(return_value=_FakeResponse(204))
        self.twitch.gql_request = AsyncMock()

        with patch.object(
            Channel, "get_spade_url", AsyncMock(return_value="https://spade.twitch.tv/track")
        ):
            result = await self.channel.send_watch()

        self.assertTrue(result)
        self.twitch.gql_request.assert_not_called()
        method, url = self.twitch.request.call_args[0][:2]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://spade.twitch.tv/track")
        self.assertEqual(
            self.twitch.request.call_args[1]["data"], self.channel._stream.spade_payload
        )

    async def test_non_204_response_is_a_failure(self):
        self.twitch.request = MagicMock(return_value=_FakeResponse(200))

        with patch.object(
            Channel, "get_spade_url", AsyncMock(return_value="https://spade.twitch.tv/track")
        ):
            self.assertFalse(await self.channel.send_watch())

    async def test_spade_url_is_fetched_once_and_cached(self):
        self.twitch.request = MagicMock(return_value=_FakeResponse(204))
        spade_mock = AsyncMock(return_value="https://spade.twitch.tv/track")

        with patch.object(Channel, "get_spade_url", spade_mock):
            await self.channel.send_watch()
            await self.channel.send_watch()

        spade_mock.assert_awaited_once()

    async def test_request_failure_returns_false(self):
        self.twitch.request = MagicMock(side_effect=RequestException("boom"))

        with patch.object(
            Channel, "get_spade_url", AsyncMock(return_value="https://spade.twitch.tv/track")
        ):
            self.assertFalse(await self.channel.send_watch())

    async def test_no_stream_returns_false(self):
        self.channel._stream = None
        self.assertFalse(await self.channel.send_watch())


if __name__ == "__main__":
    unittest.main()
