# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.

"""Tests for text measurement against the calibrated font metrics.

These assertions guard the single most load-bearing assumption in the codebase:
that a string's width can be computed offline and will match what the hardware
draws. If :func:`barometer.fonts.text_width` is wrong, every fitting decision
downstream is wrong too, and the failure shows up as clipped text on a panel
rather than as an exception anywhere.

The metrics themselves are validated against real hardware by
``scripts/verify_metrics.py``. These tests cover the *arithmetic* built on top
of them, which is what can regress without a device present.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from barometer import fonts


class TestSanitize:
    """Reducing arbitrary text to drawable printable ASCII."""

    def test_plain_ascii_is_unchanged(self) -> None:
        """Text already within the drawable range passes through intact."""
        assert fonts.sanitize("Ship it!") == "Ship it!"

    def test_emoji_are_removed(self) -> None:
        """Emoji are dropped rather than mangled.

        The upstream API really does return them; the firmware rejects the
        whole draw if any survive, so this is a correctness requirement.
        """
        assert fonts.sanitize("Go with the flow 🌊") == "Go with the flow"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("it isn’t", "it isn't"),
            ("“quoted”", '"quoted"'),
            ("dash – here", "dash - here"),
            ("em — dash", "em - dash"),
            ("more…", "more..."),
            ("non\u00a0breaking", "non breaking"),
        ],
    )
    def test_typography_is_transliterated(self, raw: str, expected: str) -> None:
        """Lookalike punctuation becomes its ASCII equivalent, not nothing.

        Dropping these outright would silently corrupt words, so they are
        mapped before the printable-range filter runs.
        """
        assert fonts.sanitize(raw) == expected

    def test_ellipsis_expands_before_filtering(self) -> None:
        """A mapping that expands to several characters survives the filter."""
        assert fonts.sanitize("wait…") == "wait..."

    def test_whitespace_is_collapsed_and_trimmed(self) -> None:
        """Runs of whitespace collapse, including gaps left by dropped glyphs."""
        assert fonts.sanitize("  too   many \n spaces  ") == "too many spaces"

    def test_text_of_only_undrawable_characters_becomes_empty(self) -> None:
        """Everything undrawable yields an empty string for the caller to handle."""
        assert fonts.sanitize("🌊🎉") == ""


class TestTextWidth:
    """Computing exact rendered width."""

    def test_empty_string_is_zero(self) -> None:
        """Empty input is special-cased; the formula would return ``-gap``."""
        assert fonts.text_width("", "tiny") == 0

    def test_width_is_sum_of_advances_minus_one_gap(self) -> None:
        """The documented model holds: the trailing gap is never drawn."""
        metrics = fonts._metrics()["small"]
        advances, gap = metrics["advances"], metrics["gap"]
        text = "Deploy"
        expected = sum(advances[c] for c in text) - gap
        assert fonts.text_width(text, "small") == expected

    def test_single_character_excludes_the_trailing_gap(self) -> None:
        """One glyph measures its advance less the gap it would be followed by."""
        metrics = fonts._metrics()["tiny"]
        assert fonts.text_width("M", "tiny") == metrics["advances"]["M"] - metrics["gap"]

    def test_ladder_is_ordered_by_descending_height(self) -> None:
        """The ladder descends in height, which is what makes it a ladder.

        Height, not width, is the ordering that holds. See
        :meth:`test_smallest_font_is_not_always_the_narrowest` for why.
        """
        heights = [fonts.line_height(f) for f in fonts.FONT_LADDER]
        assert heights == sorted(heights, reverse=True)

    def test_smallest_font_is_not_always_the_narrowest(self) -> None:
        """``tiny`` can render text *wider* than ``small`` does.

        Counter-intuitive but measured: ``tiny``'s space advance is 3px against
        ``small``'s 2px, so space-heavy text is wider in the shorter font.

        This is harmless because the ladder tries ``small`` first and returns
        the first font that fits, so the better option is always reached. It is
        pinned here because the obvious assumption -- that the last rung is the
        narrowest -- is false, and code written on it would be subtly wrong.
        """
        text = "Ship it today"
        assert fonts.text_width(text, "tiny") > fonts.text_width(text, "small")
        assert fonts.FONT_LADDER.index("small") < fonts.FONT_LADDER.index("tiny")

    def test_unknown_character_falls_back_to_question_mark(self) -> None:
        """An unsanitised character degrades gracefully instead of raising."""
        metrics = fonts._metrics()["tiny"]
        expected = metrics["advances"]["?"] - metrics["gap"]
        assert fonts.text_width("☃", "tiny") == expected

    def test_unknown_font_raises(self) -> None:
        """A misspelled font name fails loudly rather than guessing."""
        with pytest.raises(KeyError):
            fonts.text_width("x", "not_a_font")


class TestVerticalMetrics:
    """Line heights, spacing and placement."""

    def test_pitch_is_one_more_than_ink_height(self) -> None:
        """The extra pixel is what stops descenders touching the next line."""
        for font in fonts.FONT_LADDER:
            assert fonts.line_pitch(font) == fonts.line_height(font) + 1

    def test_only_tiny_stacks_on_a_16px_panel(self) -> None:
        """Exactly one font is short enough for multiple lines.

        The layout logic is built on this being true; if a firmware update
        changed font heights, this is the test that would notice.
        """
        assert fonts.max_lines("tiny") == 3
        for font in fonts.FONT_LADDER:
            if font != "tiny":
                assert fonts.max_lines(font) == 1, font

    def test_line_centres_match_the_verified_layout(self) -> None:
        """Three tiny lines land where the hardware was confirmed to look right."""
        assert fonts.line_centers(3, "tiny") == [2, 8, 14]

    def test_single_line_is_centred_on_the_panel(self) -> None:
        """One line sits at the panel's midpoint."""
        assert fonts.line_centers(1, "large") == [fonts.DISPLAY_HEIGHT // 2]

    def test_line_centres_are_evenly_spaced(self) -> None:
        """Consecutive centres differ by exactly the pitch."""
        centres = fonts.line_centers(3, "tiny")
        pitch = fonts.line_pitch("tiny")
        gaps = [b - a for a, b in pairwise(centres)]
        assert gaps == [pitch, pitch]

    def test_line_centres_are_vertically_symmetric(self) -> None:
        """The block is centred: space above the first equals space below the last."""
        for count in (1, 2, 3):
            centres = fonts.line_centers(count, "tiny")
            assert centres[0] == fonts.DISPLAY_HEIGHT - centres[-1]


class TestMetricsFile:
    """The calibrated data the module is built on."""

    def test_every_ladder_font_is_present(self) -> None:
        """Fitting would raise on any font missing from the table."""
        metrics = fonts._metrics()
        for font in fonts.FONT_LADDER:
            assert font in metrics, font

    def test_all_printable_ascii_is_covered(self) -> None:
        """Sanitised text can never contain a character the table lacks."""
        for font in fonts.FONT_LADDER:
            advances = fonts._metrics()[font]["advances"]
            missing = [chr(c) for c in range(0x20, 0x7F) if chr(c) not in advances]
            assert not missing, f"{font} missing {missing}"

    def test_metrics_are_cached(self) -> None:
        """The file is read once; every fitting decision consults it."""
        assert fonts._metrics() is fonts._metrics()

    def test_usable_width_leaves_padding_on_both_sides(self) -> None:
        """Glyphs never sit flush against the panel edge."""
        assert fonts.USABLE_WIDTH == fonts.DISPLAY_WIDTH - 2 * fonts.SIDE_PADDING
