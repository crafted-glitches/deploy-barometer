# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.

"""Tests for the BUSY Bar driver.

The device is replaced by :class:`tests.conftest.FakeDeviceClient`, which
records calls instead of performing them. That allows assertions on things a
real bar could never be asked about mid-flight -- the exact ordering of a clear
and a draw, or which elements a scroll frame omits.

Several behaviours here were discovered by testing against real hardware and
are easy to regress silently, because they fail as a wrong-looking panel rather
than an exception. Those get explicit tests: the protobuf zero-value omissions,
the scoped clear, and the dial gate.
"""

from __future__ import annotations

import asyncio

import pytest

from barometer import layout
from barometer.verdict import Verdict

YES = Verdict(True, "Go for it", "UTC", "")
NO = Verdict(False, "Tomorrow?", "UTC", "")
TOO_TALL = Verdict(False, "Trust me, they will be much happier if it wasn't broken for a night",
                   "UTC", "")


def press(action: str | None = None) -> dict:
    """Build a decoded START button event.

    Args:
        action: ``"RELEASE"``, or ``None`` for a press. A press is enum value
            ``0``, which protobuf omits entirely, so it is represented by the
            key being absent.

    Returns:
        A message shaped like the decoded status stream.
    """
    event: dict = {"button": "START"}
    if action is not None:
        event["action"] = action
    return {"updates": [{"input": {"button_event": event}}]}


def dial(position: str | None) -> dict:
    """Build a decoded dial-movement event.

    Args:
        position: Position name, or ``None`` for BUSY -- enum value ``0``,
            which protobuf omits, producing an empty event.

    Returns:
        A message shaped like the decoded status stream.
    """
    event = {} if position is None else {"position": position}
    return {"updates": [{"input": {"switch_event": event}}]}


class TestPressGate:
    """Deciding whether a button press should act."""

    @pytest.mark.parametrize(
        ("required", "unknown", "current", "expected"),
        [
            ("apps", "allow", None, True),
            ("apps", "block", None, False),
            ("apps", "allow", "APPS", True),
            ("apps", "block", "APPS", True),
            ("apps", "allow", "BUSY", False),
            ("apps", "allow", "CUSTOM", False),
            ("apps", "allow", "SETTINGS", False),
            ("apps", "allow", "OFF", False),
            ("any", "block", None, True),
            ("any", "block", "BUSY", True),
            ("custom", "allow", "CUSTOM", True),
            ("custom", "allow", "APPS", False),
        ],
    )
    def test_gate_decisions(
        self, display, settings_override, required, unknown, current, expected
    ) -> None:
        """Every combination of requirement, policy and dial position."""
        bar, _ = display
        settings_override(button_dial_position=required, button_when_dial_unknown=unknown)
        bar._dial = current
        assert bar._press_allowed() is expected


class TestButtonWatcher:
    """Reacting to the device's status stream."""

    async def _run(self, bar, client, messages) -> list[str]:
        """Feed messages through the watcher and collect callback fires."""
        fired: list[str] = []

        async def on_press() -> None:
            """Record that the watcher decided to act on a press."""
            fired.append("fired")

        client.stream_messages = messages
        client.stream_error = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await bar.watch_start_button(on_press)
        return fired

    async def test_press_triggers_a_refresh(self, display, settings_override) -> None:
        """The ordinary path: a press on the right dial position acts."""
        bar, client = display
        settings_override(button_dial_position="any")
        assert await self._run(bar, client, [press()]) == ["fired"]

    async def test_release_is_ignored(self, display, settings_override) -> None:
        """One physical press must fire once, not twice.

        The device reports both a press and a release; acting on both would
        double every reading.
        """
        bar, client = display
        settings_override(button_dial_position="any")
        assert await self._run(bar, client, [press("RELEASE")]) == []

    async def test_press_and_release_together_fire_once(self, display, settings_override) -> None:
        """A real press arrives as this exact pair."""
        bar, client = display
        settings_override(button_dial_position="any")
        assert await self._run(bar, client, [press(), press("RELEASE")]) == ["fired"]

    async def test_other_buttons_are_ignored(self, display, settings_override) -> None:
        """Only start/pause drives the app; OK and BACK belong to the device."""
        bar, client = display
        settings_override(button_dial_position="any")
        messages = [{"updates": [{"input": {"button_event": {"button": b}}}]}
                    for b in ("OK", "BACK")]
        assert await self._run(bar, client, messages) == []

    async def test_ok_button_is_ignored_despite_being_omitted(
        self, display, settings_override
    ) -> None:
        """``OK`` is enum 0, so it arrives as an absent key -- and must not fire.

        This is the trap: an event with no ``button`` key is the OK button, not
        a missing field to be defaulted to START.
        """
        bar, client = display
        settings_override(button_dial_position="any")
        assert await self._run(bar, client, [{"updates": [{"input": {"button_event": {}}}]}]) == []

    async def test_non_input_updates_are_ignored(self, display, settings_override) -> None:
        """The stream carries power and brightness updates too."""
        bar, client = display
        settings_override(button_dial_position="any")
        assert await self._run(bar, client, [{"updates": [{"power": {"battery": 50}}]}]) == []

    async def test_dial_movement_is_recorded(self, display) -> None:
        """Position is tracked from the same stream that carries presses."""
        bar, client = display
        await self._run(bar, client, [dial("APPS")])
        assert bar.dial_position == "APPS"

    async def test_empty_switch_event_means_busy(self, display) -> None:
        """BUSY is enum 0, so an empty event means the dial is on BUSY.

        Read naively this looks like "no information", which would leave the
        gate open in exactly the position the gate exists to block.
        """
        bar, client = display
        await self._run(bar, client, [dial(None)])
        assert bar.dial_position == "BUSY"

    async def test_gate_blocks_a_press_after_the_dial_moves_away(
        self, display, settings_override
    ) -> None:
        """Moving off Apps disables the button, in one stream."""
        bar, client = display
        settings_override(button_dial_position="apps", button_when_dial_unknown="allow")
        assert await self._run(bar, client, [dial("BUSY"), press()]) == []

    async def test_gate_allows_a_press_after_returning_to_apps(
        self, display, settings_override
    ) -> None:
        """Returning to Apps re-enables it."""
        bar, client = display
        settings_override(button_dial_position="apps", button_when_dial_unknown="allow")
        assert await self._run(bar, client, [dial("BUSY"), dial("APPS"), press()]) == ["fired"]

    async def test_callback_errors_do_not_kill_the_watcher(
        self, display, settings_override
    ) -> None:
        """One failed refresh must not cost every future press."""
        bar, client = display
        settings_override(button_dial_position="any")
        calls: list[int] = []

        async def on_press() -> None:
            """Record the call, then fail as a flaky upstream would."""
            calls.append(1)
            raise RuntimeError("upstream blip")

        client.stream_messages = [press(), press()]
        client.stream_error = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await bar.watch_start_button(on_press)
        assert len(calls) == 2

    async def test_non_dict_messages_are_skipped(self, display, settings_override) -> None:
        """The stream can yield raw text; only decoded updates are inspected."""
        bar, client = display
        settings_override(button_dial_position="any")
        assert await self._run(bar, client, ["some text", press()]) == ["fired"]


class TestFrameBuilding:
    """Turning a layout into a draw payload."""

    def test_background_is_drawn_first(self, display) -> None:
        """Elements paint in order, so the flood must precede the text."""
        bar, _ = display
        frame = bar._frame(layout.plan("No"), "#00B000FF")
        assert frame["elements"][0]["id"] == "background"
        assert frame["elements"][0]["type"] == "rectangle"

    def test_background_covers_the_whole_panel(self, display) -> None:
        """A partial flood would leave the previous app visible at the edges."""
        bar, _ = display
        background = bar._frame(layout.plan("No"), "#00B000FF")["elements"][0]
        assert (background["width"], background["height"]) == (72, 16)
        assert background["fill_colors"] == ["#00B000FF"]

    def test_led_matches_the_background(self, display) -> None:
        """The status LED glows the same colour as the verdict."""
        bar, _ = display
        assert bar._frame(layout.plan("No"), "#C00000FF")["led_notification_color"] == "#C00000FF"

    def test_each_line_becomes_an_element_with_a_stable_id(self, display) -> None:
        """Stable ids are what make redrawing replace rather than accumulate."""
        bar, _ = display
        plan = layout.plan("How much do you trust your logging tools?")
        ids = [e["id"] for e in bar._frame(plan, "#000000FF")["elements"][1:]]
        assert ids == [f"line{i}" for i in range(len(plan.lines))]

    def test_static_text_is_centred(self, display) -> None:
        """A static layout is anchored at the middle of the panel."""
        bar, _ = display
        element = bar._frame(layout.plan("No"), "#000000FF")["elements"][1]
        assert element["align"] == "center"
        assert element["x"] == 36

    def test_marquee_gets_the_firmware_scroll_properties(self, display) -> None:
        """Sideways scrolling is delegated to the firmware, not animated here."""
        bar, _ = display
        plan = layout.plan("How much do you trust your logging tools?", "scroll")
        element = bar._frame(plan, "#000000FF")["elements"][1]
        assert element["scroll_rate"] > 0
        assert element["width"] == 72
        assert element["align"] == "mid_left"

    def test_static_text_has_no_scroll_properties(self, display) -> None:
        """Text that fits must not be handed to the scroller."""
        bar, _ = display
        element = bar._frame(layout.plan("No"), "#000000FF")["elements"][1]
        assert "scroll_rate" not in element

    def test_offset_shifts_the_text_block(self, display) -> None:
        """Offsetting is how the downward scroll is animated."""
        bar, _ = display
        plan = layout.plan(TOO_TALL.message)
        first = bar._frame(plan, "#000000FF", offset=0)["elements"][1]["y"]
        moved = bar._frame(plan, "#000000FF", offset=-3)["elements"][1]["y"]
        assert moved == first - 3

    def test_lines_scrolled_off_panel_are_omitted(self, display) -> None:
        """Fully hidden lines would draw nothing; sending them is waste."""
        bar, _ = display
        plan = layout.plan(TOO_TALL.message)
        visible = len(bar._frame(plan, "#000000FF", offset=-100)["elements"]) - 1
        assert visible == 0

    def test_partly_visible_lines_are_kept(self, display) -> None:
        """Clipping is what makes text slide in rather than pop into place."""
        bar, _ = display
        plan = layout.plan(TOO_TALL.message)
        assert len(bar._frame(plan, "#000000FF", offset=-1)["elements"]) > 1

    def test_explicit_lines_override_the_layout(self, display) -> None:
        """Used to render the ``...`` hint variant of the held frame."""
        bar, _ = display
        frame = bar._frame(layout.plan("No"), "#000000FF", lines=["ONE", "TWO"])
        assert [e["text"] for e in frame["elements"][1:]] == ["ONE", "TWO"]

    def test_frame_carries_app_identity_and_priority(self, display, settings_override) -> None:
        """Priority arbitrates the screen; the name scopes our elements."""
        bar, _ = display
        settings_override(app_name="test_app", draw_priority=42)
        frame = bar._frame(layout.plan("No"), "#000000FF")
        assert frame["application_name"] == "test_app"
        assert frame["priority"] == 42


class TestShow:
    """Rendering a verdict end to end."""

    async def test_clears_before_drawing(self, display) -> None:
        """Line counts vary, so stale elements must go first.

        Without this, a one-line message after a four-line one leaves the
        earlier lines stranded on the panel.
        """
        bar, client = display
        await bar.show(YES)
        names = [c[0] for c in client.calls]
        assert names.index("display_clear") < names.index("display_draw")

    async def test_clear_is_scoped_to_this_application(self, display, settings_override) -> None:
        """Other apps' drawings must survive."""
        bar, client = display
        settings_override(app_name="test_app")
        await bar.show(YES)
        assert ("display_clear", "test_app") in client.calls

    async def test_plays_the_matching_sound(self, display, settings_override) -> None:
        """The cue follows the verdict."""
        bar, client = display
        settings_override(sound_enabled=True)
        await bar.show(YES)
        assert ("audio_play", "go.snd") in client.calls
        await bar.show(NO)
        assert ("audio_play", "stop.snd") in client.calls

    async def test_sound_can_be_disabled(self, display, settings_override) -> None:
        """A silent bar still shows verdicts."""
        bar, client = display
        settings_override(sound_enabled=False)
        result = await bar.show(YES)
        assert not any(c[0] == "audio_play" for c in client.calls)
        assert result["sound"] is None

    async def test_audio_failure_does_not_fail_the_render(
        self, display, settings_override
    ) -> None:
        """A missing asset or muted bar must not lose the verdict."""
        bar, client = display
        settings_override(sound_enabled=True)
        client.audio_error = RuntimeError("no such file")
        result = await bar.show(YES)
        assert result["text"] == "Go for it"

    async def test_draw_failure_propagates(self, display) -> None:
        """A request that drew nothing has genuinely failed."""
        bar, client = display
        client.draw_error = RuntimeError("409 priority")
        with pytest.raises(RuntimeError):
            await bar.show(YES)

    async def test_result_describes_what_was_rendered(self, display) -> None:
        """The API response mirrors the panel."""
        bar, _ = display
        result = await bar.show(YES)
        assert result["text"] == "Go for it"
        assert result["presentation"] == "static"
        assert result["background"] == YES.background
        assert result["lines"] == ["Go for it"]

    async def test_tall_message_starts_an_animation(self, display) -> None:
        """Scrolling is driven by a background task."""
        bar, _ = display
        result = await bar.show(TOO_TALL)
        assert result["presentation"] == "vscroll"
        assert bar._animation is not None
        await bar._stop_animation()

    async def test_short_message_starts_no_animation(self, display) -> None:
        """Nothing that fits should move."""
        bar, _ = display
        await bar.show(YES)
        assert bar._animation is None

    async def test_new_verdict_cancels_the_previous_animation(self, display) -> None:
        """Two verdicts must not animate over each other."""
        bar, _ = display
        await bar.show(TOO_TALL)
        first = bar._animation
        await bar.show(YES)
        assert first.cancelled() or first.done()
        assert bar._animation is None

    async def test_held_frame_shows_the_more_hint(self, display) -> None:
        """The first frame of a scroll marks that there is more below."""
        bar, client = display
        await bar.show(TOO_TALL)
        draw = next(c[1] for c in client.calls if c[0] == "display_draw")
        assert draw["elements"][-1]["text"].endswith("...")
        await bar._stop_animation()


class TestAssets:
    """Uploading the generated sounds."""

    async def test_uploads_when_the_device_is_empty(self, display) -> None:
        """A fresh device receives both cues."""
        bar, _ = display
        assert sorted(await bar.ensure_sounds()) == ["go.snd", "stop.snd"]

    async def test_skips_files_already_present(self, display) -> None:
        """Matching sizes mean the device is already up to date."""
        from barometer import sounds

        bar, client = display
        client.listing = [
            type("E", (), {"name": name, "size": len(build())})()
            for name, build in sounds.SOUNDS.items()
        ]
        assert await bar.ensure_sounds() == []

    async def test_reuploads_when_size_differs(self, display) -> None:
        """A truncated or stale file is replaced."""
        bar, client = display
        client.listing = [type("E", (), {"name": "go.snd", "size": 1})()]
        assert "go.snd" in await bar.ensure_sounds()

    async def test_missing_directory_is_treated_as_empty(self, display) -> None:
        """A first run has no asset directory yet; that is not an error."""
        bar, client = display
        client.storage_list_error = RuntimeError("no such directory")
        assert len(await bar.ensure_sounds()) == 2


class TestStatus:
    """Reporting device health."""

    async def test_reports_firmware_and_battery(self, display) -> None:
        """Health surfaces enough to identify the device."""
        bar, _ = display
        status = await bar.ping()
        assert status["reachable"] is True
        assert status["firmware"] == "1.1.1"
        assert status["battery_charge"] == 99

    async def test_reports_dial_and_gate_state(self, display, settings_override) -> None:
        """The gate is observable, so a dead-looking button can be diagnosed."""
        bar, _ = display
        settings_override(button_dial_position="apps", button_when_dial_unknown="allow")
        bar._dial = "BUSY"
        status = await bar.ping()
        assert status["dial"] == "BUSY"
        assert status["button_active"] is False

    async def test_unreachable_device_raises(self, display) -> None:
        """Failure is signalled by raising, not by a falsy field."""
        bar, client = display
        client.status_error = RuntimeError("unreachable")
        with pytest.raises(RuntimeError):
            await bar.ping()


class TestLifecycle:
    """Teardown behaviour."""

    async def test_close_stops_animation_and_client(self, display) -> None:
        """No frame may outlive the transport it would be written to."""
        bar, client = display
        await bar.show(TOO_TALL)
        await bar.aclose()
        assert bar._animation is None
        assert client.closed is True

    async def test_stopping_without_animation_is_safe(self, display) -> None:
        """Teardown must not depend on something having been started."""
        bar, _ = display
        await bar._stop_animation()

    async def test_clear_removes_our_elements(self, display, settings_override) -> None:
        """Manual clear is scoped like every other one."""
        bar, client = display
        settings_override(app_name="test_app")
        await bar.clear()
        assert ("display_clear", "test_app") in client.calls
