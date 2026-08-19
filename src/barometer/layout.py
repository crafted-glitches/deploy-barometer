# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.
"""Deciding how a message is presented on the panel.

Given a message and a mode, this module resolves a :class:`Layout`: which font
to use, how the text breaks into lines, and whether anything has to move. It
performs no I/O and touches no device — everything here is arithmetic over the
measured metrics in :mod:`barometer.fonts`, which makes the decisions
deterministic and testable without hardware.

Two presentation modes:

``fit``
    Shrink and wrap so the entire message is on screen at once. Only when a
    message is too tall even for the smallest font does anything scroll, and
    then it scrolls *downwards* after a deliberate reading pause.

``scroll``
    Keep the message on a single line and let the firmware marquee it sideways.

The vertical constraint drives the whole design. On a 16-pixel panel only
``tiny`` (5px ink) is short enough to stack, and then only three lines. Every
larger font is single-line by construction, so there is no general
"font size × line count" search to perform — just a ladder, then a wrap, then
scrolling as the last resort.

In practice the scrolling branch is rare. Sampling the upstream API's quips,
all but the longest fitted on screen whole.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from . import fonts

#: Appended to the last visible line when more of the message follows below.
MORE_HINT = "..."

#: How a resolved layout is presented.
#:
#: * ``static``  -- everything fits; nothing moves.
#: * ``marquee`` -- one line, scrolled sideways by the firmware.
#: * ``vscroll`` -- taller than the panel; scrolled downwards by the app.
LayoutKind = Literal["static", "marquee", "vscroll"]


@dataclass(frozen=True)
class Layout:
    """A resolved plan for drawing one message.

    Produced by :func:`plan` and consumed by
    :class:`barometer.busybar.BusyBarDisplay`, which turns it into draw
    payloads. Frozen, so a layout cannot drift between the frames of an
    animation that is rendering it.

    Attributes:
        kind: How this layout is presented. See :data:`LayoutKind`.
        font: Name of the font every line is drawn in, from
            :data:`barometer.fonts.FONT_LADDER`.
        lines: The message split into lines, already sanitised. Always at least
            one element. For ``static`` and ``marquee`` this is everything on
            screen; for ``vscroll`` it exceeds what fits and the surplus is
            revealed by scrolling.
    """

    kind: LayoutKind
    font: str
    lines: list[str]

    @property
    def scrolling(self) -> bool:
        """Whether presenting this layout involves motion.

        Returns:
            ``True`` for ``marquee`` and ``vscroll``, ``False`` for ``static``.
        """
        return self.kind != "static"

    @property
    def visible_lines(self) -> int:
        """How many lines are on screen when the layout is at rest.

        For everything that fits this is simply the line count. For a
        ``vscroll`` layout it is the panel's capacity in this font, which is
        fewer than :attr:`lines` holds — the difference is what scrolling
        exists to reveal.

        Returns:
            Number of lines visible before any scrolling occurs, at least 1.
        """
        return min(len(self.lines), fonts.max_lines(self.font))

    def with_more_hint(self) -> list[str]:
        """Return the at-rest lines, with ``...`` marking that more follows.

        Used for the held frame of a ``vscroll`` layout. Without a marker, a
        message paused mid-sentence is indistinguishable from one that simply
        ends there, and a reader has no reason to expect it to move.

        The hint is appended to the last *visible* line rather than the last
        line overall, since that is the one at the bottom of the panel while
        held. If the three dots would push that line past the panel edge,
        characters are trimmed from its end until it fits — so the marker is
        never itself clipped, which would defeat the point.

        Returns:
            Exactly :attr:`visible_lines` lines, the last ending in ``...``.
            Lines beyond the fold are omitted, since they are off-panel while
            the layout is at rest.

        Note:
            Trimming is by character, not by word, so the hint can attach
            mid-word (``"it wasn't brok..."``). That is the conventional
            reading of an ellipsis as truncation and keeps as much text visible
            as possible.
        """
        lines = list(self.lines)
        index = self.visible_lines - 1
        line = lines[index]
        while line and fonts.text_width(line + MORE_HINT, self.font) > fonts.USABLE_WIDTH:
            line = line[:-1].rstrip()
        lines[index] = line + MORE_HINT
        return lines[: self.visible_lines]


def wrap(text: str, font: str, usable: int = fonts.USABLE_WIDTH) -> list[str]:
    """Greedily wrap text into lines no wider than a pixel budget.

    Words are accumulated onto the current line while they still fit, measured
    with :func:`barometer.fonts.text_width` rather than by counting characters
    — the fonts are proportional, so ``"WWW"`` and ``"iii"`` differ by more
    than a factor of two.

    A word too wide for an entire line is hard-split across lines rather than
    allowed to overflow. That case does not arise for the upstream API's
    English quips, but without it a single long token would silently render
    past the panel edge, so no input can break the layout.

    Args:
        text: Text to wrap. Should already be sanitised; unknown characters are
            measured as ``?``.
        font: Font name the wrapping is measured in.
        usable: Maximum line width in pixels. Defaults to the panel's usable
            width, and is parameterised mainly for testing.

    Returns:
        One or more lines, none wider than ``usable``, with the original word
        order preserved. Whitespace between words is normalised to single
        spaces, and a line never has leading or trailing spaces. Returns
        ``[""]`` for empty or whitespace-only input, so callers can always
        index the first line.

    Raises:
        KeyError: ``font`` is not a known font.

    Example:
        >>> wrap("How much do you trust your logging tools?", "tiny")
        ['How much do you', 'trust your logging', 'tools?']
    """
    lines: list[str] = []
    current = ""

    for word in text.split():
        candidate = f"{current} {word}".strip()
        if fonts.text_width(candidate, font) <= usable:
            current = candidate
            continue

        if current:
            lines.append(current)
            current = ""

        while fonts.text_width(word, font) > usable:
            cut = len(word) - 1
            while cut > 1 and fonts.text_width(word[:cut], font) > usable:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        current = word

    if current:
        lines.append(current)
    return lines or [""]


def plan(message: str, mode: str = "fit") -> Layout:
    """Resolve how a message should be presented on the panel.

    The message is sanitised first, so the decision is made against exactly the
    characters the device will draw. A message reduced to nothing by
    sanitising — an emoji-only quip, say — becomes ``"?"``, because the draw
    endpoint rejects empty text and a visible placeholder beats a blank panel.

    The decision proceeds:

    1. **One line, largest font.** Walk :data:`barometer.fonts.FONT_LADDER`
       from largest down and take the first font the message fits in whole.
       This runs in *both* modes: a short message should be big and still,
       whatever the mode says about long ones.
    2. **Mode split.** In ``scroll`` mode, anything that got this far becomes a
       sideways marquee in :data:`barometer.fonts.SCROLL_FONT`.
    3. **Wrap.** In ``fit`` mode, wrap into the smallest font. If the result is
       within the panel's line capacity, it is static and entirely visible.
    4. **Scroll downwards.** Otherwise the layout is taller than the panel and
       is presented as ``vscroll``, which the display driver animates.

    Args:
        message: Raw message, typically a quip straight from the API. Sanitised
            internally, so callers need not pre-clean it.
        mode: ``"fit"`` or ``"scroll"``. Any other value behaves as ``"fit"``,
            since configuration validates the value before it reaches here.

    Returns:
        The resolved layout.

    Example:
        >>> plan("Go for it!").kind, plan("Go for it!").font
        ('static', 'extra_large')
        >>> plan("How much do you trust your logging tools?").kind
        'static'
        >>> plan("How much do you trust your logging tools?", "scroll").kind
        'marquee'
    """
    text = fonts.sanitize(message) or "?"

    # Whatever the mode, a message that fits on one line gets the biggest font
    # it fits in.
    for font in fonts.FONT_LADDER:
        if fonts.text_width(text, font) <= fonts.USABLE_WIDTH:
            return Layout("static", font, [text])

    if mode == "scroll":
        return Layout("marquee", fonts.SCROLL_FONT, [text])

    font = fonts.WRAP_FONT
    lines = wrap(text, font)
    if len(lines) <= fonts.max_lines(font):
        return Layout("static", font, lines)
    return Layout("vscroll", font, lines)
