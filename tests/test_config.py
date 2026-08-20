# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.

"""Tests for environment-driven configuration.

Configuration is resolved once at import and read directly by the rest of the
app, so a wrong value does not fail where it is set -- it fails much later,
against the device. These tests pin the defaults, the URL normalisation, and
the validation that turns a bad value into an immediate, obvious error.

Each test constructs its own ``Settings`` rather than touching the global
singleton, so nothing leaks between tests.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from barometer.config import Settings


def build(**overrides) -> Settings:
    """Construct settings without reading the developer's real ``.env``.

    Args:
        **overrides: Field values to set explicitly.

    Returns:
        A settings instance isolated from the ambient environment.
    """
    return Settings(_env_file=None, **overrides)


class TestDefaults:
    """Values that apply with no configuration at all."""

    def test_device_defaults_to_the_usb_address(self) -> None:
        """A USB-connected bar is always at this address, so it needs no setup."""
        assert build().busybar_host == "10.0.4.20"

    def test_server_is_exposed_on_all_interfaces(self) -> None:
        """Binding beyond localhost is what makes the LAN and .local names work."""
        assert build().host == "0.0.0.0"
        assert build().port == 2323

    def test_display_defaults_to_fitting_the_whole_message(self) -> None:
        """Showing everything at once is the default presentation."""
        assert build().display_mode == "fit"

    def test_button_is_gated_to_the_apps_dial_position(self) -> None:
        """The button only drives this app when the dial says so."""
        assert build().button_dial_position == "apps"

    def test_unknown_dial_allows_presses(self) -> None:
        """A bar parked on Apps works straight after a restart.

        The device never reports its dial position on connect, so this window
        is unavoidable; allowing is the choice that behaves correctly for the
        intended setup and self-corrects on the first dial movement.
        """
        assert build().button_when_dial_unknown == "allow"

    def test_no_token_by_default(self) -> None:
        """The PIN is a secret with no sensible default."""
        assert build().busybar_token == ""

    def test_draw_priority_beats_apps_but_loses_to_a_busy_session(self) -> None:
        """Built-in apps draw at 10 and an active BUSY session at 90."""
        assert 10 < build().draw_priority < 90


class TestBusybarUrl:
    """Normalising however the host was written."""

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("10.0.4.20", "http://10.0.4.20"),
            ("busybar.local", "http://busybar.local"),
            ("http://10.0.4.20", "http://10.0.4.20"),
            ("https://bar.example", "https://bar.example"),
            ("http://10.0.4.20/", "http://10.0.4.20"),
            ("http://10.0.4.20///", "http://10.0.4.20"),
            ("10.0.4.20:8080", "http://10.0.4.20:8080"),
        ],
    )
    def test_host_forms_are_normalised(self, host: str, expected: str) -> None:
        """Both the obvious value and an explicit URL are accepted."""
        assert build(busybar_host=host).busybar_url == expected

    def test_result_never_ends_in_a_slash(self) -> None:
        """Endpoint paths are appended directly, so a trailing slash would double up."""
        assert not build(busybar_host="http://x/").busybar_url.endswith("/")


class TestValidation:
    """Rejecting values that would misbehave later."""

    @pytest.mark.parametrize("priority", [0, -1, 101, 1000])
    def test_out_of_range_draw_priority_is_rejected(self, priority: int) -> None:
        """The firmware only accepts 1-100; anything else fails at startup."""
        with pytest.raises(ValidationError):
            build(draw_priority=priority)

    @pytest.mark.parametrize("priority", [1, 60, 100])
    def test_valid_draw_priority_is_accepted(self, priority: int) -> None:
        """The documented range is usable in full."""
        assert build(draw_priority=priority).draw_priority == priority

    def test_unknown_display_mode_is_rejected(self) -> None:
        """A typo fails loudly instead of silently falling back."""
        with pytest.raises(ValidationError):
            build(display_mode="sideways")

    def test_unknown_dial_position_is_rejected(self) -> None:
        """Only positions the bar actually reports are accepted."""
        with pytest.raises(ValidationError):
            build(button_dial_position="upside_down")

    @pytest.mark.parametrize("position", ["apps", "busy", "custom", "settings", "off", "any"])
    def test_every_dial_position_is_accepted(self, position: str) -> None:
        """All five positions, plus the escape hatch that disables the gate."""
        assert build(button_dial_position=position).button_dial_position == position

    def test_negative_display_seconds_is_rejected(self) -> None:
        """Zero means "until replaced"; below zero is meaningless."""
        with pytest.raises(ValidationError):
            build(display_seconds=-1)

    def test_zero_display_seconds_is_allowed(self) -> None:
        """Zero pins the verdict indefinitely, which is a supported setup."""
        assert build(display_seconds=0).display_seconds == 0

    def test_negative_read_pause_is_rejected(self) -> None:
        """A negative pause has no meaning for the scroll animation."""
        with pytest.raises(ValidationError):
            build(read_pause_seconds=-0.5)


class TestEnvironment:
    """Reading values from the environment."""

    def test_variables_use_the_barometer_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The prefix keeps these from colliding with unrelated variables."""
        monkeypatch.setenv("BAROMETER_BUSYBAR_HOST", "bar.example")
        monkeypatch.setenv("BAROMETER_PORT", "9999")
        settings = build()
        assert settings.busybar_host == "bar.example"
        assert settings.port == 9999

    def test_unprefixed_variables_are_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A generic name in the environment must not be picked up."""
        monkeypatch.setenv("PORT", "1234")
        assert build().port == 2323

    def test_unrelated_prefixed_variables_are_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown settings do not prevent startup."""
        monkeypatch.setenv("BAROMETER_SOMETHING_ELSE", "x")
        assert build().port == 2323

    def test_booleans_are_parsed_from_strings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment values are strings; booleans must still work."""
        monkeypatch.setenv("BAROMETER_SOUND_ENABLED", "false")
        assert build().sound_enabled is False
