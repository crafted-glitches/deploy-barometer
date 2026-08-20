# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.
"""Measure the real pixel width of every glyph in every BUSY Bar font.

The firmware exposes fonts by name only -- there is no metrics endpoint. So we
measure them empirically: draw a glyph repeated N times on the front display,
read the framebuffer back via /api/screen, and look at the bounding box.

Rendered width follows `width(s) = sum(advance(c) for c in s) - GAP`, where GAP
is the trailing inter-glyph gap that is not drawn after the last glyph. Drawing
a single glyph N times therefore gives `advance = (measured + GAP) / N`.

Run this once per firmware version; the result is committed as font_metrics.json.

    python scripts/calibrate_fonts.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _screen import capture_front, ink_box

from barometer.config import settings

FONTS = ["tiny", "small", "normal", "condensed", "bold", "large", "extra_large"]
GAP = 1
REPEATS = 4
SETTLE_SECONDS = 0.2
WIDTH, HEIGHT = 72, 16
OUTPUT = Path(__file__).resolve().parent.parent / "src" / "barometer" / "font_metrics.json"


def _headers() -> dict[str, str]:
    """Build the device authentication header.

    Returns:
        A dict carrying the API token, or an empty dict when no PIN is
        configured — bars with authentication disabled reject an empty token
        header, so it must be omitted entirely rather than sent blank.
    """
    return {"X-API-Token": settings.busybar_token} if settings.busybar_token else {}


def _draw(client: httpx.Client, text: str, font: str) -> None:
    """Draw a measurement probe: white text on a black background.

    The colours are the measurement instrument. A black flood gives every
    non-background pixel a single meaning — "ink" — so the rendered bounding
    box can be found by looking for anything that is not black, with no
    thresholding or glyph recognition involved.

    Text is anchored ``top_left`` at the origin so the ink starts at a known
    edge, and the draw is followed by a short settle so the framebuffer read
    that follows cannot catch a half-updated panel.

    Args:
        client: HTTP client with the device as its base URL.
        text: Probe string, typically one glyph repeated.
        font: Font to render in.

    Raises:
        httpx.HTTPStatusError: The device rejected the draw — most likely a
            ``409`` because something of higher priority owns the screen.
    """
    payload = {
        "application_name": settings.app_name,
        "priority": settings.draw_priority,
        "elements": [
            {
                "id": "bg", "type": "rectangle", "x": 0, "y": 0,
                "width": WIDTH, "height": HEIGHT, "fill": "solid",
                "fill_colors": ["#000000FF"], "border_width": 0,
                "align": "top_left", "display": "front", "timeout": 30,
            },
            {
                "id": "probe", "type": "text", "x": 0, "y": 0, "align": "top_left",
                "text": text, "font": font, "color": "#FFFFFFFF",
                "display": "front", "timeout": 30,
            },
        ],
    }
    client.post("/api/display/draw", json=payload).raise_for_status()
    time.sleep(SETTLE_SECONDS)


def _measure(client: httpx.Client) -> tuple[int, int]:
    """Measure the ink currently on the panel.

    Reads the framebuffer back and returns the bounding box of everything that
    is not black — which, given :func:`_draw`'s black background, is exactly
    the rendered glyphs.

    Args:
        client: HTTP client with the device as its base URL.

    Returns:
        ``(width, height)`` of the ink in pixels, or ``(0, 0)`` if the panel is
        blank.
    """
    return ink_box(capture_front(client))


def calibrate_font(client: httpx.Client, font: str) -> dict[str, object]:
    """Measure every printable ASCII glyph in one font.

    Each glyph is drawn ``REPEATS`` times in a row and the total ink width
    measured. Repetition is what makes this precise: rendered width is
    ``n * advance - gap``, so dividing by the repeat count averages away the
    single trailing gap and any rounding, recovering the true advance from one
    observation. Measuring a lone glyph would instead conflate its ink with its
    spacing.

    The space character needs different treatment, since it draws no pixels at
    all and has no bounding box. Its advance is recovered by difference:
    rendering ``"M M"`` and subtracting the width of ``"MM"`` leaves exactly
    one space's advance.

    Args:
        client: HTTP client with the device as its base URL.
        font: Font name to calibrate.

    Returns:
        The font's measurements: ``gap``, ``cap_height`` (the tallest ink seen
        across every glyph, so it includes descenders) and ``advances``,
        mapping each character to its advance width in pixels.

    Raises:
        RuntimeError: A probe filled the full panel width, meaning the
            measurement was clipped and the derived advance would be wrong.
            Raised rather than recorded, because a silently truncated metric
            would corrupt every fitting decision that later relies on it.
        httpx.HTTPStatusError: A draw or framebuffer read failed.
    """
    advances: dict[str, int] = {}
    cap_height = 0

    # Space draws no pixels, so derive it by difference: "M M" minus "MM".
    _draw(client, "MM", font)
    double_m, _ = _measure(client)
    _draw(client, "M M", font)
    spaced_m, _ = _measure(client)
    advances[" "] = spaced_m - double_m

    for code in range(33, 127):  # printable ASCII, excluding space
        char = chr(code)
        _draw(client, char * REPEATS, font)
        width, height = _measure(client)
        if width >= WIDTH:
            raise RuntimeError(f"glyph {char!r} in {font} clipped the display")
        advances[char] = round((width + GAP) / REPEATS)
        cap_height = max(cap_height, height)

    return {"gap": GAP, "cap_height": cap_height, "advances": advances}


def main() -> None:
    """Calibrate every font and write the metrics file.

    Takes roughly five minutes: seven fonts times ninety-five glyphs, each a
    draw, a settle and a framebuffer read. Progress is printed per font so a
    long run is visibly alive.

    The panel is cleared afterwards so the bar is not left displaying probe
    text. Output is written sorted and indented, so re-running produces a
    diffable file and a firmware change shows up as a readable diff rather than
    a wholesale rewrite.

    Raises:
        RuntimeError: Propagated from :func:`calibrate_font` if a glyph
            clipped. Nothing is written in that case, leaving any existing
            metrics file intact rather than replacing it with partial data.
    """
    metrics: dict[str, object] = {}
    with httpx.Client(base_url=settings.busybar_url, headers=_headers(), timeout=15) as client:
        for font in FONTS:
            print(f"calibrating {font} ...", flush=True)
            metrics[font] = calibrate_font(client, font)
            sample = metrics[font]["advances"]  # type: ignore[index]
            print(f"  cap_height={metrics[font]['cap_height']}px "  # type: ignore[index]
                  f"M={sample['M']}px i={sample['i']}px space={sample[' ']}px", flush=True)
        client.request("DELETE", "/api/display/draw",
                       params={"application_name": settings.app_name})

    OUTPUT.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    print(f"\nwrote {OUTPUT}")


if __name__ == "__main__":
    main()
