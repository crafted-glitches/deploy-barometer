# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.

"""Tests for the shouldideploy.today client.

The upstream API is replaced by :class:`httpx.MockTransport`, so the suite
never touches the network. That keeps it fast and deterministic, and means a
test failure always indicates a change here rather than upstream weather.

Responses are modelled on real captures, including the emoji the API genuinely
returns.
"""

from __future__ import annotations

import httpx
import pytest

from barometer.verdict import Verdict, fetch_verdict

PAYLOAD = {
    "timezone": "Europe/Dublin",
    "date": "2026-08-19T18:34:51.000Z",
    "shouldideploy": True,
    "message": "Go for it",
}


def client_returning(payload: dict, status: int = 200) -> httpx.AsyncClient:
    """Build a client whose every request yields a fixed response.

    Args:
        payload: JSON body to return.
        status: HTTP status to return.

    Returns:
        An async client backed by a mock transport, requiring no network.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        """Record the request for assertions, then return the fixed response."""
        handler.last_request = request  # type: ignore[attr-defined]
        return httpx.Response(status, json=payload)

    handler.last_request = None  # type: ignore[attr-defined]
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._handler = handler  # type: ignore[attr-defined]
    return client


class TestVerdictObject:
    """The value object representing one answer."""

    def test_positive_verdict_maps_to_the_approval_sound(self) -> None:
        """A yes plays the power-up."""
        assert Verdict(True, "x", "", "").sound == "go.snd"

    def test_negative_verdict_maps_to_the_rejection_sound(self) -> None:
        """A no plays the buzz."""
        assert Verdict(False, "x", "", "").sound == "stop.snd"

    def test_colours_follow_the_verdict(self, settings_override) -> None:
        """Green for yes, red for no, taken from configuration."""
        settings_override(color_go="#00FF00FF", color_stop="#FF0000FF")
        assert Verdict(True, "x", "", "").background == "#00FF00FF"
        assert Verdict(False, "x", "", "").background == "#FF0000FF"

    def test_colour_is_resolved_at_access_time(self, settings_override) -> None:
        """Recolouring takes effect without rebuilding verdicts.

        The colour is deliberately not captured at construction, which keeps
        the dataclass about the API response and leaves presentation to config.
        """
        verdict = Verdict(True, "x", "", "")
        settings_override(color_go="#123456FF")
        assert verdict.background == "#123456FF"

    def test_verdict_is_immutable(self) -> None:
        """An in-flight verdict cannot be mutated by the code rendering it."""
        with pytest.raises(Exception):
            Verdict(True, "x", "", "").message = "y"  # type: ignore[misc]


class TestFetchVerdict:
    """Retrieving and parsing an answer."""

    async def test_parses_a_successful_response(self) -> None:
        """Every field is carried across from the payload."""
        async with client_returning(PAYLOAD) as client:
            verdict = await fetch_verdict(client)
        assert verdict.should_deploy is True
        assert verdict.message == "Go for it"
        assert verdict.timezone == "Europe/Dublin"
        assert verdict.date == "2026-08-19T18:34:51.000Z"

    async def test_negative_verdict_is_parsed(self) -> None:
        """A false answer is not confused with a missing one."""
        async with client_returning({**PAYLOAD, "shouldideploy": False}) as client:
            assert (await fetch_verdict(client)).should_deploy is False

    async def test_timezone_is_sent_when_configured(self, settings_override) -> None:
        """A configured timezone reaches the API as the ``tz`` parameter."""
        settings_override(timezone="Europe/Dublin")
        async with client_returning(PAYLOAD) as client:
            await fetch_verdict(client)
            request = client._handler.last_request
        assert request.url.params["tz"] == "Europe/Dublin"

    async def test_timezone_is_omitted_when_not_configured(self, settings_override) -> None:
        """No timezone means no parameter, so the API applies its own default.

        Sending an empty value would be a different request with a different
        meaning, so the parameter has to be absent rather than blank.
        """
        settings_override(timezone="")
        async with client_returning(PAYLOAD) as client:
            await fetch_verdict(client)
            request = client._handler.last_request
        assert "tz" not in request.url.params

    async def test_message_is_passed_through_unsanitised(self) -> None:
        """Sanitising belongs to the display layer, not the client.

        The raw message is what an API caller sees in the response; only the
        panel needs it reduced to drawable ASCII.
        """
        async with client_returning({**PAYLOAD, "message": "Go with the flow 🌊"}) as client:
            assert (await fetch_verdict(client)).message == "Go with the flow 🌊"

    async def test_missing_optional_fields_default_to_empty(self) -> None:
        """Timezone and date are cosmetic, so their absence is tolerated."""
        async with client_returning({"shouldideploy": True, "message": "yes"}) as client:
            verdict = await fetch_verdict(client)
        assert verdict.timezone == ""
        assert verdict.date == ""

    @pytest.mark.parametrize("status", [400, 404, 500, 503])
    async def test_error_status_raises(self, status: int) -> None:
        """A failed request is surfaced, not turned into a fabricated verdict."""
        async with client_returning(PAYLOAD, status=status) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_verdict(client)

    @pytest.mark.parametrize("missing", ["shouldideploy", "message"])
    async def test_missing_required_field_raises(self, missing: str) -> None:
        """A response without a verdict or a message cannot be displayed."""
        payload = {k: v for k, v in PAYLOAD.items() if k != missing}
        async with client_returning(payload) as client:
            with pytest.raises(KeyError):
                await fetch_verdict(client)

    async def test_network_failure_propagates(self) -> None:
        """Connection errors reach the caller, which reports them as a 502."""
        def handler(request: httpx.Request) -> httpx.Response:
            """Simulate a connection that never reaches the API."""
            raise httpx.ConnectError("unreachable")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.HTTPError):
                await fetch_verdict(client)
