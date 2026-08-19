# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.
"""Check the calibrated font metrics against what the bar actually renders.

Draws sample strings on the device and compares the measured pixel width with
the width `fonts.text_width` predicts. Run after re-calibrating, or after a
firmware update, to confirm the table is still correct.

    python scripts/verify_metrics.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _screen import capture_front, ink_box  # noqa: E402

from barometer import fonts  # noqa: E402
from barometer.config import settings  # noqa: E402

SAMPLES = [
    "No",
    "SHIP IT",
    "Go for it!",
    "Deploy!",
    "Yes, but be careful",
    "j0Q|@#$%^&*()",
    "The quick brown fox",
    "Tell your boss that you found a bug",
]


def main() -> int:
    """Check every sample string in every font against the device.

    For each combination, the predicted width from the calibrated metrics is
    compared with the width the hardware actually renders. Any disagreement is
    a real defect: it means text this app believes fits may not, and the
    fitting logic is quietly unsound.

    Samples that would exceed the panel are skipped rather than failed, since a
    clipped render cannot be measured and would report a false mismatch. The
    sample set deliberately includes punctuation and mixed-width characters,
    which are where a metrics table is most likely to be wrong.

    Returns:
        ``0`` if every prediction matched, ``1`` otherwise — so this can be
        used as a check in CI or after a firmware update. Mismatches are
        printed individually with predicted and actual widths.

    Raises:
        httpx.HTTPStatusError: A draw or framebuffer read failed.
    """
    headers = {"X-API-Token": settings.busybar_token} if settings.busybar_token else {}
    failures = 0

    with httpx.Client(base_url=settings.busybar_url, headers=headers, timeout=15) as client:
        for font in fonts.FONT_LADDER:
            for sample in SAMPLES:
                predicted = fonts.text_width(sample, font)
                if predicted > fonts.DISPLAY_WIDTH:
                    continue  # would clip; nothing to compare against

                payload = {
                    "application_name": settings.app_name,
                    "priority": settings.draw_priority,
                    "elements": [
                        {"id": "bg", "type": "rectangle", "x": 0, "y": 0,
                         "width": 72, "height": 16, "fill": "solid",
                         "fill_colors": ["#000000FF"], "border_width": 0,
                         "align": "top_left", "display": "front", "timeout": 30},
                        {"id": "t", "type": "text", "x": 0, "y": 0, "align": "top_left",
                         "text": sample, "font": font, "color": "#FFFFFFFF",
                         "display": "front", "timeout": 30},
                    ],
                }
                client.post("/api/display/draw", json=payload).raise_for_status()
                time.sleep(0.2)

                actual, _ = ink_box(capture_front(client))

                if actual != predicted:
                    failures += 1
                    print(f"MISMATCH {font:12} predicted={predicted:3} actual={actual:3} "
                          f"{sample!r}")

        client.request("DELETE", "/api/display/draw",
                       params={"application_name": settings.app_name})

    checked = len(SAMPLES) * len(fonts.FONT_LADDER)
    if failures:
        print(f"\n{failures} mismatch(es)")
        return 1
    print(f"all predictions matched the device (up to {checked} samples)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
