# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.

"""Tests for the HTTP surface and the refresh coordinator.

The endpoints are exercised through a real ASGI transport, so routing, methods
and status codes are genuinely tested rather than simulated by calling the
handler functions directly. The application lifespan is *not* run: it would
open a device connection, start a websocket watcher and publish an mDNS name,
none of which belong in a unit test. Instead the coordinator on ``app.state``
is replaced with a double.

That mirrors production honestly, because request handlers only ever reach the
coordinator through ``app.state``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from barometer import main
from barometer.verdict import Verdict


class FakeBar:
    """Minimal stand-in for the device driver."""

    def __init__(self, ping_error: Exception | None = None) -> None:
        """Optionally arm the device to fail its health check."""
        self.ping_error = ping_error

    async def ping(self) -> dict[str, Any]:
        """Return a health summary, or fail if an error was injected."""
        if self.ping_error is not None:
            raise self.ping_error
        return {"reachable": True, "host": "10.0.4.20", "firmware": "1.1.1",
                "battery_charge": 99, "dial": "APPS", "button_active": True}


class FakeBarometer:
    """Stand-in coordinator that records refreshes."""

    def __init__(self, error: Exception | None = None,
                 ping_error: Exception | None = None) -> None:
        """Optionally arm refresh and/or the device to fail."""
        self.error = error
        self.bar = FakeBar(ping_error)
        self.last_reading: dict[str, Any] | None = None
        self.sources: list[str] = []

    async def refresh(self, source: str) -> dict[str, Any]:
        """Record the trigger and return a reading, or raise."""
        self.sources.append(source)
        if self.error is not None:
            raise self.error
        self.last_reading = {"should_deploy": True, "message": "Go for it", "source": source}
        return self.last_reading


async def call(method: str, path: str) -> httpx.Response:
    """Issue a request against the app without running its lifespan.

    Args:
        method: HTTP method.
        path: Request path.

    Returns:
        The response.
    """
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path)


@pytest.fixture
def barometer(monkeypatch: pytest.MonkeyPatch):
    """Install a fake coordinator on the application state.

    Returns:
        A factory taking the same arguments as :class:`FakeBarometer`.
    """
    def install(**kwargs: Any) -> FakeBarometer:
        """Build a coordinator double and attach it to the app state."""
        fake = FakeBarometer(**kwargs)
        monkeypatch.setattr(main.app.state, "barometer", fake, raising=False)
        return fake

    return install


class TestCheckEndpoint:
    """The trigger endpoint."""

    async def test_post_returns_the_reading(self, barometer) -> None:
        """The documented verb works and echoes what was displayed."""
        barometer()
        response = await call("POST", "/check")
        assert response.status_code == 200
        assert response.json()["message"] == "Go for it"

    async def test_get_also_works(self, barometer) -> None:
        """Accepting GET is deliberate: bookmarks, webhooks and a bare curl.

        This is the difference between ``curl <host>/check`` working and
        needing ``-X POST``, which is the whole reason it is allowed.
        """
        barometer()
        assert (await call("GET", "/check")).status_code == 200

    async def test_reading_is_attributed_to_the_api(self, barometer) -> None:
        """Readings record what triggered them, distinguishing API from button."""
        fake = barometer()
        await call("POST", "/check")
        assert fake.sources == ["api"]

    async def test_upstream_failure_is_reported_as_bad_gateway(self, barometer) -> None:
        """This service works; something it depends on did not."""
        barometer(error=httpx.ConnectError("unreachable"))
        response = await call("POST", "/check")
        assert response.status_code == 502
        assert "shouldideploy" in response.json()["error"]

    async def test_device_failure_is_distinguished_from_upstream(self, barometer) -> None:
        """Both are 502, but the body says which failed."""
        barometer(error=RuntimeError("409 priority"))
        response = await call("POST", "/check")
        assert response.status_code == 502
        assert "bar" in response.json()["error"]
        assert "409" in response.json()["detail"]

    async def test_unsupported_method_is_rejected(self, barometer) -> None:
        """Only the two intended verbs are routed."""
        barometer()
        assert (await call("DELETE", "/check")).status_code == 405


class TestHealthEndpoint:
    """The health endpoint."""

    async def test_healthy_when_the_device_answers(self, barometer) -> None:
        """A reachable bar means the whole system is up."""
        barometer()
        response = await call("GET", "/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_degraded_when_the_device_is_unreachable(self, barometer) -> None:
        """503 is what the container health check acts on."""
        barometer(ping_error=RuntimeError("no route to host"))
        response = await call("GET", "/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
        assert response.json()["device"]["reachable"] is False

    async def test_reports_the_dial_gate(self, barometer) -> None:
        """Makes a dead-looking button diagnosable without reading logs."""
        barometer()
        device = (await call("GET", "/health")).json()["device"]
        assert device["dial"] == "APPS"
        assert device["button_active"] is True

    async def test_includes_the_last_reading(self, barometer) -> None:
        """Health doubles as "what is on the panel right now?"."""
        barometer()
        await call("POST", "/check")
        assert (await call("GET", "/health")).json()["last_reading"]["message"] == "Go for it"

    async def test_last_reading_is_null_before_any_request(self, barometer) -> None:
        """Nothing has been displayed yet, and the response says so."""
        barometer()
        assert (await call("GET", "/health")).json()["last_reading"] is None


class TestBarometer:
    """The real coordinator, with its device and API replaced."""

    def build(self, monkeypatch: pytest.MonkeyPatch, verdict: Verdict, rendered: dict) -> Any:
        """Construct a coordinator whose fetch and render are stubbed."""
        barometer = main.Barometer.__new__(main.Barometer)
        import asyncio

        barometer._lock = asyncio.Lock()
        barometer.last_reading = None
        barometer.http = object()

        class Bar:
            """Device double returning a fixed rendering."""

            async def show(self, v: Verdict) -> dict:
                """Return the prepared render description."""
                return rendered

        barometer.bar = Bar()
        monkeypatch.setattr(main, "fetch_verdict", lambda _client: _async(verdict))
        return barometer

    async def test_refresh_builds_a_complete_reading(self, monkeypatch) -> None:
        """The reading combines the verdict, its rendering and its provenance."""
        verdict = Verdict(True, "Go for it", "UTC", "2026-01-01T00:00:00Z")
        rendered = {"text": "Go for it", "font": "large"}
        barometer = self.build(monkeypatch, verdict, rendered)

        reading = await barometer.refresh("button")
        assert reading["should_deploy"] is True
        assert reading["message"] == "Go for it"
        assert reading["source"] == "button"
        assert reading["display"] == rendered
        assert "shown_at" in reading

    async def test_refresh_records_the_last_reading(self, monkeypatch) -> None:
        """Health reports the most recent success."""
        verdict = Verdict(False, "Tomorrow?", "UTC", "")
        barometer = self.build(monkeypatch, verdict, {"text": "Tomorrow?"})
        assert barometer.last_reading is None
        await barometer.refresh("api")
        assert barometer.last_reading["message"] == "Tomorrow?"

    async def test_refreshes_are_serialised(self, monkeypatch) -> None:
        """A button press and an API call must not interleave draws.

        Concurrent draws would fight over the panel, and a scrolling verdict
        keeps drawing after its request returns.
        """
        import asyncio

        verdict = Verdict(True, "Go", "UTC", "")
        barometer = self.build(monkeypatch, verdict, {"text": "Go"})
        active = 0
        peak = 0

        class SlowBar:
            """Device double that tracks how many renders overlap."""

            async def show(self, v: Verdict) -> dict:
                """Render slowly, recording peak concurrency."""
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1
                return {"text": "Go"}

        barometer.bar = SlowBar()
        await asyncio.gather(*(barometer.refresh("api") for _ in range(5)))
        assert peak == 1


def _async(value):
    """Wrap a value in an awaitable, for stubbing coroutine functions."""
    async def coro(*args, **kwargs):
        """Return the wrapped value when awaited."""
        return value

    return coro()
