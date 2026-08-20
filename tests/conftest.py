# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.

"""Shared pytest fixtures and test doubles.

Every test in this suite is **hermetic**: no BUSY Bar, no network, no clock
dependence. That is a deliberate constraint rather than a convenience. The
whole point of a pre-commit hook is that it runs anywhere, in seconds, without
the hardware plugged in — a suite that needed a device would simply be skipped,
and skipped tests protect nothing.

The device is therefore replaced by :class:`FakeDeviceClient`, which records
what it was asked to do so tests can assert on the *intent* of a call rather
than its effect on a panel nobody can see.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


class FakeDeviceClient:
    """Stand-in for ``busylib.AsyncBusyBar`` that records calls.

    Implements only the surface :class:`barometer.busybar.BusyBarDisplay`
    actually uses. Each method appends to :attr:`calls`, so a test can assert
    both *what* happened and *in what order* — ordering matters for at least
    one real invariant, namely that stale elements are cleared before a new
    verdict is drawn.

    Failures are injected by assigning to the ``*_error`` attributes, which is
    how the resilience paths (a mute bar, an unreachable device) are exercised
    without needing a broken one.

    Attributes:
        calls: ``(method_name, payload)`` pairs in invocation order.
        listing: Files :meth:`storage_list` reports as already on the device.
        storage_list_error: If set, raised by :meth:`storage_list`.
        audio_error: If set, raised by :meth:`audio_play`.
        draw_error: If set, raised by :meth:`display_draw`.
        status_error: If set, raised by :meth:`status`.
        stream_messages: Messages :meth:`stream_status_ws` yields.
        stream_error: If set, raised after the messages are exhausted.
    """

    def __init__(self) -> None:
        """Start with an empty call log and no injected failures."""
        self.calls: list[tuple[str, Any]] = []
        self.listing: list[Any] = []
        self.storage_list_error: Exception | None = None
        self.audio_error: Exception | None = None
        self.draw_error: Exception | None = None
        self.status_error: Exception | None = None
        self.stream_messages: list[Any] = []
        self.stream_error: Exception | None = None
        self.closed = False

    # --- assets ---

    async def storage_list(self, path: str) -> Any:
        """Report which assets the fake device already holds."""
        self.calls.append(("storage_list", path))
        if self.storage_list_error is not None:
            raise self.storage_list_error
        return type("Listing", (), {"list": self.listing})()

    async def assets_upload(self, app_name: str, filename: str, data: bytes) -> None:
        """Record an asset upload, keeping only its size."""
        self.calls.append(("assets_upload", (app_name, filename, len(data))))

    # --- output ---

    async def display_draw(self, payload: dict[str, Any]) -> None:
        """Record a draw, or fail if an error was injected."""
        self.calls.append(("display_draw", payload))
        if self.draw_error is not None:
            raise self.draw_error

    async def display_clear(self, application_name: str | None = None) -> None:
        """Record a scoped clear."""
        self.calls.append(("display_clear", application_name))

    async def audio_play(self, path: str, application_name: str) -> None:
        """Record playback, or fail if an error was injected."""
        self.calls.append(("audio_play", path))
        if self.audio_error is not None:
            raise self.audio_error

    # --- status ---

    async def status(self) -> Any:
        """Return a status object shaped like the real client's."""
        self.calls.append(("status", None))
        if self.status_error is not None:
            raise self.status_error
        firmware = type("Firmware", (), {"version": "1.1.1"})()
        power = type("Power", (), {"battery_charge": 99})()
        return type("Status", (), {"firmware": firmware, "power": power})()

    async def stream_status_ws(self, **kwargs: Any):
        """Yield queued status messages, then optionally raise."""
        self.calls.append(("stream_status_ws", kwargs))
        for message in self.stream_messages:
            yield message
        if self.stream_error is not None:
            raise self.stream_error

    async def aclose(self) -> None:
        """Mark the fake client closed."""
        self.closed = True


@pytest.fixture
def fake_client() -> FakeDeviceClient:
    """Provide a fresh recording device client."""
    return FakeDeviceClient()


@pytest.fixture
def display(monkeypatch: pytest.MonkeyPatch, fake_client: FakeDeviceClient):
    """Provide a ``BusyBarDisplay`` wired to the fake device.

    Patches the client class in the driver's namespace before construction, so
    the display builds its usual object graph and no production code needs a
    seam it would not otherwise have.

    Returns:
        A tuple of the display and the fake client backing it.
    """
    from barometer import busybar

    monkeypatch.setattr(busybar, "AsyncBusyBar", lambda *a, **k: fake_client)
    return busybar.BusyBarDisplay(), fake_client


@pytest.fixture
def settings_override(monkeypatch: pytest.MonkeyPatch):
    """Temporarily override fields on the global settings singleton.

    Configuration is a module-level singleton read directly by the code under
    test, so overrides must be undone or they leak between tests.
    ``monkeypatch.setattr`` restores the previous value automatically.

    Returns:
        A callable taking keyword arguments to apply to the settings object.
    """
    from barometer.config import settings

    def apply(**overrides: Any) -> None:
        """Apply overrides, rejecting names that are not real settings.

        The assertion guards against a typo silently testing nothing.
        """
        for key, value in overrides.items():
            assert hasattr(settings, key), f"unknown setting: {key}"
            monkeypatch.setattr(settings, key, value)

    return apply
