# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.
"""Driver for the BUSY Bar: assets, drawing, animation and button input.

This is the only module that talks to the device. It wraps the official
``busylib`` async client, which supplies the authentication header and — more
importantly — the protobuf schema needed to decode the device's status stream,
where button presses arrive.

Responsibilities:

* **Assets.** Synthesised sounds are uploaded once on startup, then reused.
* **Drawing.** A :class:`barometer.layout.Layout` is turned into the firmware's
  JSON element format and drawn.
* **Animation.** Downward scrolling is driven from here, frame by frame,
  because the firmware only scrolls sideways.
* **Input.** A long-lived websocket delivers start/pause presses.

Two firmware behaviours shape the code, both established by testing rather than
documentation:

1. **Element ids are the unit of replacement.** Redrawing with the same ids
   replaces a drawing outright, so consecutive animation frames need no
   clearing and do not flicker. Line *counts* vary between messages though, so
   a scoped clear runs before each new verdict to remove stale line elements.
2. **The status websocket is fragile.** The device never sends close frames and
   appears to leak a client slot on every dropped stream; enough reconnect
   churn and it stops serving the stream entirely until the bar is restarted,
   while the REST API carries on working. The retry policy here is therefore
   deliberately unhurried — see :func:`BusyBarDisplay.watch_start_button`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from busylib import AsyncBusyBar

from . import fonts, layout, sounds
from .config import settings
from .verdict import Verdict

logger = logging.getLogger(__name__)


class _OffPanelFilter(logging.Filter):
    """Suppress busylib's warning about elements positioned past the panel edge.

    Downward scrolling works precisely by parking lines below the panel and
    sliding them into view, so busylib's complaint that an element sits beyond
    the display bounds is expected rather than actionable. Left unfiltered it
    fires several times per second during an animation and buries real warnings.

    The filter matches on message text so that only this one complaint is
    dropped; every other warning from the same logger still gets through.

    Note:
        Installed on the ``busylib.client.display`` logger at import time, as a
        module-level side effect. Importing this module therefore mutates
        third-party logging configuration — a deliberate trade for keeping the
        suppression next to the code whose behaviour justifies it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Decide whether a log record should be emitted.

        Args:
            record: The record busylib is about to emit.

        Returns:
            ``False`` for the off-panel geometry warning, which drops it;
            ``True`` for everything else.
        """
        return "exceeds front" not in record.getMessage()


logging.getLogger("busylib.client.display").addFilter(_OffPanelFilter())

#: Uploaded app assets live here on the device's filesystem.
ASSET_DIR = "/ext/user_assets"

#: Reconnect backoff for the status websocket, in seconds.
_RECONNECT_MIN, _RECONNECT_MAX = 2.0, 60.0

#: A stream has to stay up this long before it counts as healthy and earns a
#: backoff reset. Without this, a connection that yields one frame and dies
#: resets the delay every time, turning the retry loop into a hammer on a
#: device that is already struggling.
_HEALTHY_AFTER_SECONDS = 30.0

#: Vertical scrolling: one pixel every this many seconds, then a rest at the
#: bottom before returning to the top.
_VSCROLL_STEP_SECONDS = 0.09
_VSCROLL_BOTTOM_PAUSE = 2.5


class BusyBarDisplay:
    """Everything this application does to a BUSY Bar.

    One instance owns one device connection and, at most, one running scroll
    animation. It is not safe to call :meth:`show` concurrently from several
    tasks: the caller is expected to serialise refreshes, which
    :class:`barometer.main.Barometer` does with a lock. Sequential calls are
    fine and cheap — each one supersedes whatever was on screen.

    All configuration is read from the module-level settings object rather than
    passed in, so a single instance always targets the configured bar.

    Attributes:
        _client: The underlying async device client.
        _animation: The running downward-scroll task, or ``None`` when the
            current layout needs no animation.
    """

    def __init__(self) -> None:
        """Create a client for the configured bar.

        No I/O happens here and no connection is opened; the first request
        establishes one. An empty token is normalised to ``None`` so that bars
        with authentication disabled are not sent an empty header.

        Version compatibility checking is switched off deliberately: the client
        otherwise warns whenever the device's API version differs from the one
        it was built against, which is noise for an app that uses a small,
        stable subset of endpoints.
        """
        self._client = AsyncBusyBar(
            settings.busybar_url,
            token=settings.busybar_token or None,
            compatibility_mode="none",
        )
        self._animation: asyncio.Task[None] | None = None

    async def aclose(self) -> None:
        """Stop any animation and close the device connection.

        Cancels the scroll task before closing the transport, so a frame in
        flight cannot outlive the client it would be written to. Safe to call
        when nothing is running.
        """
        await self._stop_animation()
        await self._client.aclose()

    # --- assets ---------------------------------------------------------------

    async def ensure_sounds(self) -> list[str]:
        """Upload the synthesised sounds, skipping any already on the device.

        Called once at startup. Waveform generation is deterministic — the same
        code always produces the same bytes — so a matching file size is
        sufficient evidence that the device already holds the right audio, and
        avoids re-uploading ~95 KB on every restart.

        A failure to list the asset directory is treated as "nothing is there
        yet" rather than an error, because that is exactly what a first run on
        a fresh device looks like: the directory is created by the first upload.

        Returns:
            Filenames actually uploaded, in the order they were sent. Empty
            when the device was already up to date, which is the normal case
            after the first run.

        Raises:
            Exception: Propagated from the upload itself if the device rejects
                a write or is unreachable. Only *listing* failures are
                tolerated; a failed upload is fatal, since the verdict sound
                would otherwise be silently missing.
        """
        try:
            listing = await self._client.storage_list(f"{ASSET_DIR}/{settings.app_name}")
            on_device = {entry.name: getattr(entry, "size", -1) for entry in listing.list}
        except Exception:  # directory does not exist yet on first run
            on_device = {}

        uploaded = []
        for filename, build in sounds.SOUNDS.items():
            data = build()
            if on_device.get(filename) == len(data):
                continue
            await self._client.assets_upload(settings.app_name, filename, data)
            uploaded.append(filename)
        if uploaded:
            logger.info("uploaded sounds: %s", ", ".join(uploaded))
        return uploaded

    # --- drawing --------------------------------------------------------------

    def _frame(
        self,
        plan: layout.Layout,
        background: str,
        *,
        lines: list[str] | None = None,
        offset: int | None = None,
    ) -> dict[str, Any]:
        """Build one complete draw payload for the front panel.

        Every frame is self-contained: a background rectangle flooding all
        72x16 pixels, then one text element per visible line on top. Elements
        are drawn in array order, so the background must come first. Ids are
        stable (``background``, ``line0``, ``line1``...) so that redrawing
        replaces the previous frame in place rather than stacking on it, which
        is what makes animation flicker-free.

        Vertical placement has two modes. Without an ``offset`` the block is
        centred on the panel, which is right for anything static. With one, the
        block is anchored so that a zero offset puts the first screenful
        exactly where a static block of the same font would sit, and scrolling
        by the full travel distance leaves the *last* screenful in that same
        position — so the text arrives and departs at identical alignment
        rather than drifting.

        Lines scrolled completely off the panel are omitted. They would draw
        nothing anyway, and skipping them keeps the payload small. Lines that
        are only *partly* off-panel are kept, since clipping them is what makes
        text slide smoothly into view instead of appearing all at once.

        Args:
            plan: The resolved layout being drawn. Its ``kind`` decides whether
                text elements get the firmware's sideways-scroll properties.
            background: Panel colour as ``#RRGGBBAA``. Also used for the status
                LED, so the bar's glow matches the verdict.
            lines: Text to draw instead of ``plan.lines`` — used to render the
                ``...`` hint variant. Defaults to the layout's own lines.
            offset: Vertical shift in pixels, negative to move text up. ``None``
                centres the block and is the normal static case.

        Returns:
            A payload for the firmware's draw endpoint, containing the
            application name, draw priority, LED colour and element list.
        """
        drawn = lines if lines is not None else plan.lines
        pitch = fonts.line_pitch(plan.font)
        ink = fonts.line_height(plan.font)

        if offset is None:
            centers = [float(y) for y in fonts.line_centers(len(drawn), plan.font)]
        else:
            # Anchored so that at rest the first screenful sits exactly where a
            # static block of the same font would, and the last screenful does
            # too once fully scrolled.
            start = fonts.line_centers(fonts.max_lines(plan.font), plan.font)[0]
            centers = [start + index * pitch + offset for index in range(len(drawn))]

        elements: list[dict[str, Any]] = [
            {
                "id": "background", "type": "rectangle", "x": 0, "y": 0,
                "width": fonts.DISPLAY_WIDTH, "height": fonts.DISPLAY_HEIGHT,
                "align": "top_left", "fill": "solid", "fill_colors": [background],
                "border_width": 0, "display": "front",
                "timeout": settings.display_seconds,
            }
        ]
        for index, (line, y) in enumerate(zip(drawn, centers)):
            # Skip lines scrolled entirely off the panel: they would draw
            # nothing and the device client rightly complains about them.
            if y + ink / 2 < 0 or y - ink / 2 > fonts.DISPLAY_HEIGHT:
                continue
            element: dict[str, Any] = {
                "id": f"line{index}", "type": "text", "x": fonts.DISPLAY_WIDTH // 2,
                "y": round(y), "align": "center", "text": line, "font": plan.font,
                "color": settings.color_text, "display": "front",
                "timeout": settings.display_seconds,
            }
            if plan.kind == "marquee":
                # Hand the overflow to the firmware's sideways scroller.
                element |= {
                    "x": 0, "align": "mid_left", "width": fonts.DISPLAY_WIDTH,
                    "scroll_rate": fonts.SCROLL_RATE,
                    "scroll_start_delay": fonts.SCROLL_START_DELAY,
                    "scroll_repeat_delay": fonts.SCROLL_REPEAT_DELAY,
                }
            elements.append(element)

        return {
            "application_name": settings.app_name,
            "priority": settings.draw_priority,
            "led_notification_color": background,
            "elements": elements,
        }

    async def show(self, verdict: Verdict) -> dict[str, Any]:
        """Render a verdict on the bar and play its sound.

        The single entry point for putting something on screen. It supersedes
        whatever was there before, including a scroll animation still running
        from a previous verdict.

        Sequence:

        1. Cancel any running animation, so two verdicts cannot animate at once.
        2. Resolve the layout for the configured display mode.
        3. Clear this application's elements. Necessary because line counts
           differ between messages — going from a four-line message to a
           one-line message would otherwise leave the old ``line1``–``line3``
           on screen, since only ids that are redrawn get replaced. The clear
           is scoped to this application, so other apps' drawings survive.
        4. Draw the first frame. For a scrolling layout this is the held view
           with the ``...`` hint.
        5. Start the scroll animation, if the layout needs one.
        6. Play the verdict's sound.

        Sound failure is caught and logged rather than raised: a bar with the
        volume down, or an asset that failed to upload, should still show the
        verdict. Display failure is *not* caught, since a request that could
        not draw anything has genuinely failed and the caller reports it.

        Args:
            verdict: The verdict to present. Supplies the message, the
                background colour and the sound.

        Returns:
            A description of what was rendered, suitable for returning to an
            API caller:

            * ``text`` -- the sanitised message as one string.
            * ``font`` -- the font chosen by the layout.
            * ``mode`` -- the configured display mode.
            * ``presentation`` -- ``static``, ``marquee`` or ``vscroll``.
            * ``lines`` -- the message as drawn, split into lines.
            * ``background`` -- the panel colour used.
            * ``sound`` -- the asset played, or ``None`` when sound is disabled.

        Raises:
            Exception: Propagated from the device if the draw fails — most
                usefully a ``409`` when something of higher priority owns the
                screen, such as an active BUSY session.
        """
        await self._stop_animation()
        plan = layout.plan(verdict.message, settings.display_mode)
        background = verdict.background

        # Line counts differ between messages, so stale `lineN` elements from a
        # previous verdict have to go. The clear is scoped to this application,
        # leaving other apps' drawings untouched.
        await self._client.display_clear(application_name=settings.app_name)

        first = plan.with_more_hint() if plan.kind == "vscroll" else None
        await self._client.display_draw(self._frame(
            plan, background, lines=first, offset=0 if plan.kind == "vscroll" else None,
        ))

        if plan.kind == "vscroll":
            self._animation = asyncio.create_task(
                self._scroll_down(plan, background), name="vscroll"
            )

        if settings.sound_enabled:
            try:
                await self._client.audio_play(
                    path=verdict.sound, application_name=settings.app_name
                )
            except Exception as error:  # a mute bar should not fail the request
                logger.warning("could not play %s: %s", verdict.sound, error)

        return {
            "text": " ".join(plan.lines),
            "font": plan.font,
            "mode": settings.display_mode,
            "presentation": plan.kind,
            "lines": plan.lines,
            "background": background,
            "sound": verdict.sound if settings.sound_enabled else None,
        }

    async def _scroll_down(self, plan: layout.Layout, background: str) -> None:
        """Animate a taller-than-panel message, looping until it expires.

        Runs as a background task, because the firmware can only scroll
        sideways; vertical movement has to be driven frame by frame from here.
        Each frame is a full redraw, which the device handles comfortably —
        measured round-trip is about 21 ms, and travel is only a few pixels, so
        a complete pass costs well under a second of device time.

        One cycle:

        1. Hold still, so the visible lines can be read. The frame itself was
           already drawn by :meth:`show`, so this begins with the pause.
        2. Walk the text up one pixel at a time until the final lines are in
           view.
        3. Rest at the bottom, so the ending can be read too.
        4. Redraw the held view with its ``...`` hint and repeat.

        Looping matters because a reader rarely arrives at the moment the
        verdict does; without it, anyone glancing over a few seconds late would
        see a message frozen mid-sentence.

        The task ends by being cancelled — by the next :meth:`show`, or by
        shutdown — or by reaching the display timeout. When
        ``display_seconds`` is ``0`` the verdict has no expiry and this loops
        indefinitely, which is intended for an always-on display.

        Args:
            plan: The layout to animate. Must be taller than the panel;
                otherwise travel is zero and this degenerates to redrawing the
                same frame forever.
            background: Panel colour as ``#RRGGBBAA``.

        Raises:
            asyncio.CancelledError: Re-raised untouched so cancellation is not
                mistaken for an error and the task terminates promptly.
        """
        pitch = fonts.line_pitch(plan.font)
        hidden = max(0, len(plan.lines) - fonts.max_lines(plan.font))
        travel = hidden * pitch
        deadline = (time.monotonic() + settings.display_seconds
                    if settings.display_seconds else None)

        try:
            while deadline is None or time.monotonic() < deadline:
                await asyncio.sleep(settings.read_pause_seconds)

                for step in range(1, travel + 1):
                    await self._client.display_draw(
                        self._frame(plan, background, offset=-step)
                    )
                    await asyncio.sleep(_VSCROLL_STEP_SECONDS)

                await asyncio.sleep(_VSCROLL_BOTTOM_PAUSE)
                await self._client.display_draw(self._frame(
                    plan, background, lines=plan.with_more_hint(), offset=0,
                ))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("vertical scroll stopped: %s", error)

    async def _stop_animation(self) -> None:
        """Cancel the running scroll animation and wait for it to finish.

        Awaits the cancelled task rather than merely requesting cancellation,
        so that by the time this returns no further frames can be drawn. That
        ordering is what stops a stale frame from a previous verdict landing on
        top of a new one.

        Both :class:`asyncio.CancelledError` and any error the task failed with
        are swallowed: this is teardown, and the animation's outcome cannot
        usefully change what the caller does next. Safe to call when no
        animation is running.
        """
        if self._animation is None:
            return
        self._animation.cancel()
        try:
            await self._animation
        except (asyncio.CancelledError, Exception):
            pass
        self._animation = None

    async def clear(self) -> None:
        """Remove this application's drawings from the panel.

        Stops any animation first, so nothing redraws immediately afterwards.
        The clear is scoped to this application's name, so drawings owned by
        other apps are left alone; the bar returns to whatever it was showing
        underneath, usually the clock.

        Provided for completeness and manual control — the normal display
        lifecycle relies on each element's own timeout instead.
        """
        await self._stop_animation()
        await self._client.display_clear(application_name=settings.app_name)

    async def ping(self) -> dict[str, Any]:
        """Query the device for a liveness summary.

        Backs the health endpoint. Nested fields are read defensively with
        :func:`getattr`, so a firmware revision that omits part of the status
        payload degrades to ``None`` values rather than failing the health
        check outright — the useful signal is that the device answered at all.

        Returns:
            A summary containing ``reachable`` (always ``True`` — failure is
            signalled by raising), ``host``, ``firmware`` version and
            ``battery_charge`` as a percentage.

        Raises:
            Exception: Propagated if the device cannot be reached or errors.
                The caller catches this and reports the bar as unreachable.
        """
        status = await self._client.status()
        firmware = getattr(status, "firmware", None)
        power = getattr(status, "power", None)
        return {
            "reachable": True,
            "host": settings.busybar_host,
            "firmware": getattr(firmware, "version", None),
            "battery_charge": getattr(power, "battery_charge", None),
        }

    # --- input ----------------------------------------------------------------

    async def watch_start_button(self, on_press: Callable[[], Awaitable[None]]) -> None:
        """Watch the device's status stream and react to start/pause presses.

        Runs until cancelled, holding a single long-lived websocket and
        reconnecting when it drops. Intended to be run as a background task for
        the lifetime of the application.

        Three details, each established by testing against the hardware:

        **The handshake is mandatory.** The device sends nothing at all until
        the client enables the stream. A connection without it opens, stays up
        and reports healthy while delivering no events whatsoever — verified by
        watching such a stream stay silent through three physical presses. The
        handshake is issued by the underlying client by default.

        **A press arrives as an absent field.** Protobuf omits zero-valued
        fields, and ``PRESS`` is enum value ``0``, so a press is signalled by
        the ``action`` key being *missing* rather than present. Reading it with
        a ``PRESS`` default is what makes one physical press fire once; a naive
        equality check against the decoded value would miss presses entirely,
        and treating any button event as a press would fire twice — once on
        press and once on release.

        **Reconnecting is expensive for the device.** It never sends close
        frames and appears to leak a client slot on each dropped stream; a
        burst of reconnects will wedge its websocket server until the bar is
        restarted, all while the REST API keeps working normally. So the
        backoff resets only after a stream has proven itself by staying up for
        a sustained period, not on the first frame received. Resetting eagerly
        turns a stream that yields one frame and dies into a tight retry loop
        against an already-struggling device — which is exactly how this was
        discovered.

        Callback errors are logged and swallowed, so one failed refresh — an
        upstream API blip, say — does not tear down the watcher and cost every
        future press.

        Args:
            on_press: Awaited once per press. Should be idempotent-ish and
                reasonably quick; while it runs, no further events are read
                from the stream.

        Raises:
            asyncio.CancelledError: Re-raised untouched so shutdown is prompt
                and cancellation is never mistaken for a dropped connection.
        """
        delay = _RECONNECT_MIN
        while True:
            started = time.monotonic()
            try:
                # The device sends nothing until this handshake, so it is
                # required to hear button presses -- verified by watching a
                # stream without it stay silent through three presses.
                async for message in self._client.stream_status_ws():
                    if not isinstance(message, dict):
                        continue
                    if time.monotonic() - started > _HEALTHY_AFTER_SECONDS:
                        delay = _RECONNECT_MIN
                    for update in message.get("updates", []):
                        button = update.get("input", {}).get("button_event")
                        if button is None:
                            continue
                        if button.get("button") != "START":
                            continue
                        if button.get("action", "PRESS") != "PRESS":
                            continue
                        logger.info("start button pressed")
                        try:
                            await on_press()
                        except Exception:
                            logger.exception("failed to advance to the next verdict")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("status stream dropped (%s); retrying in %.0fs", error, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RECONNECT_MAX)
