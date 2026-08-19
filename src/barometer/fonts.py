# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.
"""Text measurement against real, on-device BUSY Bar font metrics.

The firmware exposes fonts by name only. There is no endpoint that reports a
glyph's width, and the fonts are proportional bitmaps — ``M`` is three times
the width of ``i`` in some of them — so a character count tells you nothing
about whether a string will fit a 72-pixel panel.

Rather than estimate, the widths were **measured on the hardware**.
``scripts/calibrate_fonts.py`` draws every printable ASCII glyph, reads the
framebuffer back, and records each glyph's true advance width into
``font_metrics.json``, which this module reads. The resulting model reproduces
the device's own rendering exactly: checked against 56 real renders spanning
all seven fonts, every prediction matched to the pixel.

The layout model, in full:

* Each glyph occupies an *advance* — its ink plus one trailing gap pixel.
* The trailing gap after the final glyph is not drawn, so a string's width is
  the sum of its advances minus one gap. See :func:`text_width`.
* Vertically, a font's *ink height* is its tallest glyph including descenders.
  Stacked lines are spaced one pixel further apart than that. See
  :func:`line_pitch`.

On a 16-pixel-tall panel this means only ``tiny`` can hold more than one line.
Every other font is single-line by definition, which is what keeps
:mod:`barometer.layout` simple.

The metrics file is specific to a firmware version. Re-run the calibration
after a firmware update and confirm with ``scripts/verify_metrics.py``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

#: Width of the bar's front panel in pixels. It is a 72x16 RGB LED matrix.
DISPLAY_WIDTH = 72

#: Height of the bar's front panel in pixels.
DISPLAY_HEIGHT = 16

#: Blank pixels reserved at each end of a line, so glyphs never sit flush
#: against the panel edge where they are hardest to read at an angle.
SIDE_PADDING = 2

#: Widest a single line of text may be drawn, in pixels.
USABLE_WIDTH = DISPLAY_WIDTH - 2 * SIDE_PADDING

#: Font names ordered from visually largest to smallest, used as a fitting
#: ladder: the first font a string fits in wins.
#:
#: ``condensed`` deliberately sits *after* ``normal``. Both are 11px tall, but
#: measurement showed condensed is the narrower of the two, so trying it after
#: normal buys one extra step of fitting at the same height before the layout
#: has to drop to a genuinely shorter font.
FONT_LADDER = ["extra_large", "large", "bold", "normal", "condensed", "small", "tiny"]

#: The only font short enough to stack more than one line on a 16px panel.
WRAP_FONT = "tiny"

#: Font used for the sideways marquee. Once text scrolls its width no longer
#: constrains anything, so this is chosen purely for legibility in motion.
SCROLL_FONT = "large"

#: Marquee speed, in the pixels-per-minute unit the firmware expects.
#: 1200 px/min is 20 px/s, which reads comfortably at arm's length.
SCROLL_RATE = 1200

#: Milliseconds the marquee holds still before scrolling, so the opening words
#: can be read before anything moves.
SCROLL_START_DELAY = 800

#: Milliseconds the marquee pauses between repeat cycles.
SCROLL_REPEAT_DELAY = 1500

_METRICS_PATH = Path(__file__).with_name("font_metrics.json")

#: Non-ASCII characters the upstream API actually emits, mapped to ASCII the
#: bitmap fonts can draw. Typographic quotes and dashes appear in the quips,
#: and a non-breaking space would otherwise survive whitespace collapsing.
_TRANSLITERATIONS = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
}


@lru_cache(maxsize=1)
def _metrics() -> dict[str, dict]:
    """Load and cache the measured font metrics.

    Cached because every fitting decision consults it and the file never
    changes at runtime. The cache holds a single entry, since the function
    takes no arguments.

    Returns:
        Font name mapped to its measurements, each containing:

        * ``advances``: single-character string mapped to its advance width in
          pixels, covering all printable ASCII (``\\x20``–``\\x7E``).
        * ``gap``: pixels of spacing included in every advance, and therefore
          the amount to subtract once for a string's trailing edge.
        * ``cap_height``: the font's ink height in pixels — the tallest glyph
          including descenders.

    Raises:
        FileNotFoundError: ``font_metrics.json`` is missing. It ships with the
            package; if absent, run ``scripts/calibrate_fonts.py``.
        json.JSONDecodeError: The metrics file is corrupt.
    """
    return json.loads(_METRICS_PATH.read_text())


def sanitize(text: str) -> str:
    """Reduce text to the printable ASCII the firmware's bitmap fonts accept.

    The draw endpoint validates text against ``^[\\x20-\\x7E]+$`` and rejects
    the entire request if anything falls outside it, so an unsanitised emoji
    does not render badly — it fails the draw and blanks the panel. The API's
    quips genuinely contain such characters (an observed response was
    ``"Go with the flow 🌊"``), which makes this mandatory rather than defensive.

    Applied in order:

    1. Known typographic characters are transliterated to ASCII equivalents,
       so a curly apostrophe becomes ``'`` rather than vanishing mid-word.
    2. Anything still outside ``\\x20``–``\\x7E`` is dropped. This is what
       removes emoji, and it runs *after* transliteration so that mappings
       expanding to several characters (``…`` to ``...``) survive.
    3. Whitespace is collapsed to single spaces and trimmed, tidying both the
       gaps left by dropped characters and any original double spacing.

    Args:
        text: Arbitrary text, typically a quip straight from the API.

    Returns:
        Printable-ASCII text safe to send to the draw endpoint. May be empty if
        every character was dropped; callers substitute a placeholder.

    Example:
        >>> sanitize("Go with the flow 🌊")
        'Go with the flow'
        >>> sanitize("it isn\\u2019t broken\\u2026")
        "it isn't broken..."
    """
    for source, replacement in _TRANSLITERATIONS.items():
        text = text.replace(source, replacement)
    cleaned = "".join(char for char in text if "\x20" <= char <= "\x7e")
    return " ".join(cleaned.split()).strip()


def text_width(text: str, font: str) -> int:
    """Compute the exact rendered width of a string, in pixels.

    Sums each glyph's measured advance and subtracts one gap, because the
    trailing gap after the last glyph is never drawn. Verified against the
    hardware across all seven fonts with no discrepancies, so the result can be
    compared directly against :data:`USABLE_WIDTH` to decide whether text fits.

    Characters absent from the metrics table fall back to the width of ``?``.
    In practice this cannot trigger for text that has been through
    :func:`sanitize`, since the table covers all printable ASCII; it exists so
    an unsanitised string degrades to a slightly wrong width rather than a
    :class:`KeyError`.

    Args:
        text: The string to measure. Should already be sanitised.
        font: Font name, one of :data:`FONT_LADDER`.

    Returns:
        Width in pixels. Zero for empty input — which is special-cased, since
        the general formula would otherwise return ``-gap``.

    Raises:
        KeyError: ``font`` is not a font the device reports metrics for.
    """
    font_metrics = _metrics()[font]
    advances = font_metrics["advances"]
    gap = font_metrics["gap"]
    fallback = advances["?"]
    if not text:
        return 0
    return sum(advances.get(char, fallback) for char in text) - gap


def line_height(font: str) -> int:
    """Return a font's ink height in pixels.

    This is the vertical extent of the font's tallest glyph *including*
    descenders, measured across the whole printable ASCII range. It is
    therefore a worst case: a line of text with neither descenders nor tall
    punctuation occupies less.

    Args:
        font: Font name, one of :data:`FONT_LADDER`.

    Returns:
        Ink height in pixels, from 5 (``tiny``) to 14 (``extra_large``).

    Raises:
        KeyError: ``font`` is not a known font.
    """
    return int(_metrics()[font]["cap_height"])


def line_pitch(font: str) -> int:
    """Return the vertical distance between stacked lines of a font.

    One pixel more than the ink height. That single pixel is not cosmetic:
    spacing lines at exactly the ink height leaves zero blank rows between
    them, so a descender on one line touches an ascender on the next. On paper
    that is tight but readable; on a 16-pixel LED matrix the glyphs visibly
    merge. This was confirmed by rendering the same three lines at both
    spacings and comparing the captured panel.

    Args:
        font: Font name, one of :data:`FONT_LADDER`.

    Returns:
        Line pitch in pixels.

    Raises:
        KeyError: ``font`` is not a known font.
    """
    return line_height(font) + 1


def max_lines(font: str) -> int:
    """Return how many lines of a font fit on the panel at once.

    Solves for the largest ``n`` satisfying
    ``n * line_pitch + line_height <= DISPLAY_HEIGHT + 1``.

    The ``+ 1`` grants one pixel of tolerated overhang. Because
    :func:`line_height` is a worst case that only the tallest glyphs reach, the
    outermost lines almost never occupy their full nominal extent, and demanding
    a strict fit would cost a whole line of capacity for overflow that is
    typically invisible. Confirmed on hardware: three lines of ``tiny`` render
    cleanly under this rule with no visible clipping.

    Args:
        font: Font name, one of :data:`FONT_LADDER`.

    Returns:
        Line capacity — 3 for ``tiny``, and 1 for every other font on a
        16-pixel panel.

    Raises:
        KeyError: ``font`` is not a known font.
    """
    count = 1
    while (count * line_pitch(font) + line_height(font)) <= DISPLAY_HEIGHT + 1:
        count += 1
    return count


def line_centers(count: int, font: str) -> list[int]:
    """Return Y coordinates for a vertically centred block of lines.

    Coordinates are the *centre* of each line, matching the ``center`` anchor
    used when drawing text elements, not their top edge. The block is centred
    by distributing the span between the first and last line's centres —
    ``(count - 1) * pitch`` — within the panel height.

    Args:
        count: Number of lines to place. Usually at most :func:`max_lines`,
            though larger values are meaningful for scrolling layouts, where
            the caller anchors and offsets the block itself.
        font: Font name, one of :data:`FONT_LADDER`.

    Returns:
        One Y coordinate per line, top to bottom, rounded to whole pixels.
        Values may fall outside the panel if ``count`` exceeds what fits.

    Raises:
        KeyError: ``font`` is not a known font.

    Example:
        >>> line_centers(3, "tiny")
        [2, 8, 14]
        >>> line_centers(1, "large")
        [8]
    """
    pitch = line_pitch(font)
    start = (DISPLAY_HEIGHT - (count - 1) * pitch) / 2
    return [round(start + pitch * index) for index in range(count)]
