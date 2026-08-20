# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.

"""Tests for long-running behaviour: animation, reconnection and startup.

These cover the code that normally runs for hours, which makes it the easiest
to leave untested and the most expensive to get wrong -- a scroll that stops
looping or a watcher that gives up after one drop both fail silently, with a
bar that simply stops responding.

Timings are collapsed to near-zero so real behaviour is exercised without real
waiting, and every loop is bounded by a timeout so a regression fails the suite
instead of hanging it.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from barometer import busybar, main
from barometer.verdict import Verdict

TOO_TALL = Verdict(False, "Trust me, they will be much happier if it wasn't broken for a night",
                   "UTC", "")


@pytest.fixture
def fast_animation(monkeypatch: pytest.MonkeyPatch):
    """Collapse animation delays so a full cycle runs in milliseconds."""
    monkeypatch.setattr(busybar, "_VSCROLL_STEP_SECONDS", 0.001)
    monkeypatch.setattr(busybar, "_VSCROLL_BOTTOM_PAUSE", 0.001)


class TestScrollAnimation:
    """Driving a taller-than-panel message downwards."""

    async def test_frames_are_drawn_with_decreasing_offsets(
        self, display, settings_override, fast_animation
    ) -> None:
        """The text walks upwards a pixel at a time to reveal what is below."""
        bar, client = display
        settings_override(read_pause_seconds=0, display_seconds=60, sound_enabled=False)
        await bar.show(TOO_TALL)
        await asyncio.sleep(0.05)
        await bar._stop_animation()

        ys = [
            c[1]["elements"][1]["y"]
            for c in client.calls
            if c[0] == "display_draw" and len(c[1]["elements"]) > 1
        ]
        assert len(ys) > 2
        assert min(ys) < ys[0], "text never moved upwards"

    async def test_animation_loops_back_to_the_top(
        self, display, settings_override, fast_animation
    ) -> None:
        """Looping matters: a reader rarely arrives as the verdict does.

        Without it, anyone glancing over a few seconds late sees a message
        frozen mid-sentence.
        """
        bar, client = display
        settings_override(read_pause_seconds=0, display_seconds=60, sound_enabled=False)
        await bar.show(TOO_TALL)
        await asyncio.sleep(0.15)
        await bar._stop_animation()

        hinted = [
            c for c in client.calls
            if c[0] == "display_draw"
            and any(str(e.get("text", "")).endswith("...") for e in c[1]["elements"][1:])
        ]
        assert len(hinted) >= 2, "held frame was never redrawn, so it did not loop"

    async def test_animation_stops_at_the_display_deadline(
        self, display, settings_override, fast_animation
    ) -> None:
        """A verdict with an expiry stops animating when it expires."""
        bar, _ = display
        settings_override(read_pause_seconds=0, display_seconds=0.05, sound_enabled=False)
        await bar.show(TOO_TALL)
        await asyncio.wait_for(bar._animation, timeout=2.0)
        assert bar._animation.done()

    async def test_animation_survives_a_failed_frame(
        self, display, settings_override, fast_animation
    ) -> None:
        """A dropped frame ends the animation quietly, not with a crash."""
        bar, client = display
        settings_override(read_pause_seconds=0, display_seconds=60, sound_enabled=False)
        await bar.show(TOO_TALL)
        client.draw_error = RuntimeError("device busy")
        await asyncio.sleep(0.05)
        await bar._stop_animation()

    async def test_cancelling_stops_further_frames(
        self, display, settings_override, fast_animation
    ) -> None:
        """After teardown returns, no more frames may be drawn.

        Awaiting the cancelled task is what guarantees this; merely requesting
        cancellation would let a frame land on top of the next verdict.
        """
        bar, client = display
        settings_override(read_pause_seconds=0, display_seconds=60, sound_enabled=False)
        await bar.show(TOO_TALL)
        await asyncio.sleep(0.02)
        await bar._stop_animation()
        before = len(client.calls)
        await asyncio.sleep(0.03)
        assert len(client.calls) == before


class TestWatcherReconnection:
    """Recovering when the status stream drops."""

    async def test_reconnects_after_a_dropped_stream(
        self, display, settings_override, monkeypatch
    ) -> None:
        """A dropped connection is retried rather than ending the watcher."""
        bar, client = display
        settings_override(button_dial_position="any")
        attempts = 0
        original = client.stream_status_ws

        def failing(**kwargs):
            """Open a failing stream, ending the loop after a few attempts."""
            nonlocal attempts
            attempts += 1
            if attempts >= 3:
                raise asyncio.CancelledError()
            return original(**kwargs)

        client.stream_status_ws = failing
        client.stream_error = RuntimeError("connection closed")
        # Capture the real sleep first: patching with a lambda that calls
        # asyncio.sleep would call the patched version and recurse forever.
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda *_: real_sleep(0))

        with pytest.raises(asyncio.CancelledError):
            await bar.watch_start_button(lambda: asyncio.sleep(0))
        assert attempts >= 3

    async def test_backoff_grows_between_failures(
        self, display, settings_override, monkeypatch
    ) -> None:
        """Delays lengthen instead of hammering a struggling device.

        The bar leaks a websocket slot on every dropped stream, so an eager
        retry loop will wedge it entirely until the hardware is restarted.
        """
        bar, client = display
        settings_override(button_dial_position="any")
        delays: list[float] = []
        attempts = 0

        async def record(delay: float = 0) -> None:
            """Capture each backoff delay instead of waiting it out."""
            delays.append(delay)
            if len(delays) >= 4:
                raise asyncio.CancelledError()

        def failing(**kwargs):
            """Fail immediately, as a wedged device does."""
            nonlocal attempts
            attempts += 1
            raise RuntimeError("connection closed")

        client.stream_status_ws = failing
        monkeypatch.setattr(asyncio, "sleep", record)

        with pytest.raises(asyncio.CancelledError):
            await bar.watch_start_button(lambda: asyncio.sleep(0))
        assert delays == sorted(delays)
        assert delays[-1] > delays[0]

    async def test_backoff_is_capped(self, display, settings_override, monkeypatch) -> None:
        """Retries never stop entirely, however long the outage."""
        bar, client = display
        settings_override(button_dial_position="any")
        delays: list[float] = []

        async def record(delay: float = 0) -> None:
            """Capture delays over a long outage to observe the cap."""
            delays.append(delay)
            if len(delays) >= 20:
                raise asyncio.CancelledError()

        client.stream_status_ws = lambda **k: (_ for _ in ()).throw(RuntimeError("closed"))
        monkeypatch.setattr(asyncio, "sleep", record)

        with pytest.raises(asyncio.CancelledError):
            await bar.watch_start_button(lambda: asyncio.sleep(0))
        assert max(delays) <= busybar._RECONNECT_MAX


class TestApplicationLifespan:
    """Startup and shutdown wiring."""

    @pytest.fixture
    def wired(self, monkeypatch: pytest.MonkeyPatch):
        """Replace the coordinator and announcer with recording doubles."""
        events: list[str] = []

        class FakeBar:
            """Device double recording lifecycle milestones."""

            async def ensure_sounds(self) -> list[str]:
                """Record that assets were synchronised."""
                events.append("sounds")
                return []

            async def watch_start_button(self, on_press) -> None:
                """Record that watching began, then block as the real one does."""
                events.append("watching")
                await asyncio.Event().wait()

        class FakeBarometer:
            """Coordinator double wrapping the recording device."""

            def __init__(self) -> None:
                """Attach a recording device double."""
                self.bar = FakeBar()

            async def refresh(self, source: str) -> dict:
                """Return an empty reading; refresh is covered elsewhere."""
                return {}

            async def aclose(self) -> None:
                """Record that resources were released."""
                events.append("closed")

        class FakeAnnouncer:
            """mDNS double recording publish and withdraw."""

            def __init__(self, *args, **kwargs) -> None:
                """Accept whatever the app passes and expose a URL."""
                self.url = "http://bar.local:2323"

            async def start(self) -> bool:
                """Record a successful announcement."""
                events.append("announced")
                return True

            async def stop(self) -> None:
                """Record withdrawal of the name."""
                events.append("unannounced")

        monkeypatch.setattr(main, "Barometer", FakeBarometer)
        monkeypatch.setattr(main, "Announcer", FakeAnnouncer)
        return events

    async def test_startup_uploads_sounds_and_starts_watching(
        self, wired, settings_override
    ) -> None:
        """Assets and the button watcher are both live before serving."""
        settings_override(mdns_enabled=False)
        async with main.lifespan(main.app):
            await asyncio.sleep(0)
        assert "sounds" in wired
        assert "watching" in wired

    async def test_shutdown_closes_resources(self, wired, settings_override) -> None:
        """Nothing is left running after the app stops."""
        settings_override(mdns_enabled=False)
        async with main.lifespan(main.app):
            pass
        assert "closed" in wired

    async def test_mdns_is_announced_and_withdrawn(self, wired, settings_override) -> None:
        """The name is published for exactly the app's lifetime."""
        settings_override(mdns_enabled=True)
        async with main.lifespan(main.app):
            assert "announced" in wired
        assert "unannounced" in wired

    async def test_mdns_can_be_disabled(self, wired, settings_override) -> None:
        """Disabled in containers, where it cannot work anyway."""
        settings_override(mdns_enabled=False)
        async with main.lifespan(main.app):
            pass
        assert "announced" not in wired

    async def test_failed_asset_upload_is_not_fatal(
        self, monkeypatch, settings_override
    ) -> None:
        """A sleeping bar must not stop the app from serving.

        The watcher reconnects when the device returns; refusing to start would
        require a human to notice and restart it.
        """
        settings_override(mdns_enabled=False)

        class FailingBar:
            """Device double whose asset upload fails, as a sleeping bar does."""

            async def ensure_sounds(self) -> list[str]:
                """Fail as an unreachable device would."""
                raise RuntimeError("device asleep")

            async def watch_start_button(self, on_press) -> None:
                """Block, as the real watcher does while retrying."""
                await asyncio.Event().wait()

        class FakeBarometer:
            """Coordinator double wrapping the failing device."""

            def __init__(self) -> None:
                """Attach the failing device double."""
                self.bar = FailingBar()

            async def refresh(self, source: str) -> dict:
                """Return an empty reading."""
                return {}

            async def aclose(self) -> None:
                """Release nothing; there is nothing to release."""

        monkeypatch.setattr(main, "Barometer", FakeBarometer)
        async with main.lifespan(main.app):
            pass

    async def test_failed_announcement_is_not_fatal(
        self, monkeypatch, wired, settings_override
    ) -> None:
        """A blocked network costs a hostname, not the service."""
        settings_override(mdns_enabled=True)

        class FailingAnnouncer:
            """mDNS double that cannot register, and must never be stopped."""

            def __init__(self, *args, **kwargs) -> None:
                """Expose no URL, since nothing is published."""
                self.url = ""

            async def start(self) -> bool:
                """Report failure, as a blocked network would."""
                return False

            async def stop(self) -> None:
                """Fail the test if teardown touches a failed announcer."""
                raise AssertionError("must not be stopped when it never started")

        monkeypatch.setattr(main, "Announcer", FailingAnnouncer)
        async with main.lifespan(main.app):
            pass


class TestEntryPoint:
    """The console-script entry point."""

    def test_serves_on_the_configured_address(
        self, monkeypatch: pytest.MonkeyPatch, settings_override
    ) -> None:
        """Host and port come from configuration, not hardcoded values."""
        settings_override(host="1.2.3.4", port=9999)
        captured: dict = {}

        import uvicorn

        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
        main.main()
        assert captured["host"] == "1.2.3.4"
        assert captured["port"] == 9999


class TestCoordinatorTeardown:
    """Closing the coordinator's resources."""

    async def test_closes_http_client_and_device(self, monkeypatch) -> None:
        """Both long-lived clients are released on shutdown."""
        closed: list[str] = []

        barometer = main.Barometer.__new__(main.Barometer)

        class FakeHttp:
            """HTTP client double recording its closure."""

            async def aclose(self) -> None:
                """Record that the HTTP client was closed."""
                closed.append("http")

        class FakeBar:
            """Device double recording its closure."""

            async def aclose(self) -> None:
                """Record that the device client was closed."""
                closed.append("bar")

        barometer.http = FakeHttp()
        barometer.bar = FakeBar()
        await barometer.aclose()
        assert closed == ["http", "bar"]


class TestBackoffReset:
    """Recovering the retry delay once a stream proves itself."""

    async def test_healthy_stream_resets_the_backoff(
        self, display, settings_override, monkeypatch
    ) -> None:
        """A stream that stays up long enough earns its short delay back.

        This is the counterpart to the anti-hammering rule: without a reset, a
        device that recovered would keep being retried at the maximum delay
        forever. The reset is gated on *duration*, not on receiving a frame,
        because a stream that yields one frame and dies is not healthy.
        """
        bar, client = display
        settings_override(button_dial_position="any")
        monkeypatch.setattr(busybar, "_HEALTHY_AFTER_SECONDS", -1)  # any frame counts

        delays: list[float] = []

        async def record(delay: float = 0) -> None:
            """Capture delays to confirm they stay at the minimum."""
            delays.append(delay)
            if len(delays) >= 3:
                raise asyncio.CancelledError()

        client.stream_messages = [{"updates": []}]
        client.stream_error = RuntimeError("closed")
        monkeypatch.setattr(asyncio, "sleep", record)

        with pytest.raises(asyncio.CancelledError):
            await bar.watch_start_button(lambda: asyncio.sleep(0))
        assert delays == [busybar._RECONNECT_MIN] * len(delays)


class TestLogFilter:
    """Suppressing the expected off-panel geometry warning."""

    def test_off_panel_warning_is_dropped(self) -> None:
        """Scrolling parks lines off-panel deliberately, several times a second."""
        record = logging.LogRecord("busylib", logging.WARNING, "", 0,
                                   "Element line3 y=18 exceeds front height=16", None, None)
        assert busybar._OffPanelFilter().filter(record) is False

    def test_other_warnings_still_pass(self) -> None:
        """Only this one complaint is suppressed, not the logger."""
        record = logging.LogRecord("busylib", logging.WARNING, "", 0,
                                   "device is on fire", None, None)
        assert busybar._OffPanelFilter().filter(record) is True


class TestCoordinatorConstruction:
    """Building the coordinator."""

    def test_creates_clients_without_connecting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Construction performs no I/O; both clients connect lazily."""
        monkeypatch.setattr(main, "BusyBarDisplay", lambda: "device")
        barometer = main.Barometer()
        assert barometer.bar == "device"
        assert barometer.last_reading is None
        assert isinstance(barometer._lock, asyncio.Lock)


class TestAnnouncerTeardownFailure:
    """Withdrawing a name when the socket is already gone."""

    async def test_unregister_failure_still_closes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Shutdown must not leak a socket because the goodbye failed.

        During teardown the network may already be gone; there is nothing
        useful left to do, but the instance must still be released.
        """
        from barometer import discovery

        closed: list[bool] = []

        class Broken:
            """zeroconf double whose withdrawal fails but which must still close."""

            async def async_unregister_service(self, info: object) -> None:
                """Fail as a already-closed socket would."""
                raise RuntimeError("socket already closed")

            async def async_close(self) -> None:
                """Record that the instance was released regardless."""
                closed.append(True)

        announcer = discovery.Announcer("bar", 2323, "1.2.3.4")
        announcer._zeroconf = Broken()
        announcer._info = object()
        await announcer.stop()
        assert closed == [True]
        assert announcer._zeroconf is None
