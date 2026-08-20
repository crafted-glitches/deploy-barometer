# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.

"""Tests for the fit/scroll presentation decision.

:mod:`barometer.layout` performs no I/O, so its behaviour is fully determined
by the message and the mode. That makes it the one place where the app's most
visible behaviour -- what you actually see on the panel -- can be pinned
exactly, without a device.

Real quips from the upstream API are used as fixtures throughout, chosen to sit
on either side of the fit/wrap/scroll boundaries.
"""

from __future__ import annotations

import pytest

from barometer import fonts, layout

#: Real API quips, shortest to longest.
SHORT = "No"
MEDIUM = "Call your partner!"
WRAPS = "How much do you trust your logging tools?"
TOO_TALL = "Trust me, they will be much happier if it wasn't broken for a night"


class TestWrap:
    """Breaking text into lines that fit a pixel budget."""

    def test_short_text_stays_on_one_line(self) -> None:
        """Text within budget is not split."""
        assert layout.wrap("No", "tiny") == ["No"]

    def test_every_line_fits_the_budget(self) -> None:
        """No produced line exceeds the usable width."""
        for line in layout.wrap(TOO_TALL, "tiny"):
            assert fonts.text_width(line, "tiny") <= fonts.USABLE_WIDTH

    def test_words_are_preserved_in_order(self) -> None:
        """Wrapping rearranges nothing; joining the lines restores the text."""
        assert " ".join(layout.wrap(TOO_TALL, "tiny")).split() == TOO_TALL.split()

    def test_lines_have_no_stray_whitespace(self) -> None:
        """Lines are trimmed, so centring is not thrown off by invisible padding."""
        for line in layout.wrap(TOO_TALL, "tiny"):
            assert line == line.strip()

    def test_empty_input_yields_one_empty_line(self) -> None:
        """Callers can always index the first line."""
        assert layout.wrap("", "tiny") == [""]
        assert layout.wrap("   ", "tiny") == [""]

    def test_unbreakable_word_is_hard_split(self) -> None:
        """A word too wide for a line is split rather than allowed to overflow.

        English quips never trigger this, but without it a single long token
        would silently render past the panel edge.
        """
        lines = layout.wrap("W" * 200, "tiny")
        assert len(lines) > 1
        for line in lines:
            assert fonts.text_width(line, "tiny") <= fonts.USABLE_WIDTH
        assert "".join(lines) == "W" * 200

    def test_narrower_budget_produces_more_lines(self) -> None:
        """The budget genuinely drives the split."""
        assert len(layout.wrap(WRAPS, "tiny", usable=30)) > len(layout.wrap(WRAPS, "tiny"))


class TestPlanFitMode:
    """The default mode: get the whole message on screen."""

    def test_short_message_uses_the_largest_font(self) -> None:
        """A two-character message fills the panel rather than sitting tiny."""
        plan = layout.plan(SHORT)
        assert plan.kind == "static"
        assert plan.font == fonts.FONT_LADDER[0]
        assert plan.lines == [SHORT]

    def test_medium_message_stays_on_one_line_in_a_smaller_font(self) -> None:
        """Fitting one line is preferred over wrapping."""
        plan = layout.plan(MEDIUM)
        assert plan.kind == "static"
        assert len(plan.lines) == 1

    def test_long_message_wraps_and_still_fits(self) -> None:
        """A message too wide for one line wraps but does not scroll."""
        plan = layout.plan(WRAPS)
        assert plan.kind == "static"
        assert plan.font == fonts.WRAP_FONT
        assert 1 < len(plan.lines) <= fonts.max_lines(fonts.WRAP_FONT)

    def test_message_too_tall_scrolls_downwards(self) -> None:
        """Only when wrapping overflows the panel does anything move."""
        plan = layout.plan(TOO_TALL)
        assert plan.kind == "vscroll"
        assert len(plan.lines) > fonts.max_lines(plan.font)

    def test_chosen_font_actually_fits(self) -> None:
        """Whatever is chosen, every static line is within the usable width."""
        for message in (SHORT, MEDIUM, WRAPS):
            plan = layout.plan(message)
            for line in plan.lines:
                assert fonts.text_width(line, plan.font) <= fonts.USABLE_WIDTH


class TestPlanScrollMode:
    """The alternative mode: one line, marqueed sideways."""

    def test_overflowing_message_becomes_a_marquee(self) -> None:
        """Text that does not fit is left on one line to scroll."""
        plan = layout.plan(WRAPS, "scroll")
        assert plan.kind == "marquee"
        assert plan.font == fonts.SCROLL_FONT
        assert len(plan.lines) == 1

    def test_short_message_is_still_static_and_large(self) -> None:
        """Mode only governs overflow; a short message is big and still either way."""
        assert layout.plan(SHORT, "scroll") == layout.plan(SHORT, "fit")

    def test_scroll_mode_never_wraps(self) -> None:
        """Even the longest message stays on a single line."""
        assert len(layout.plan(TOO_TALL, "scroll").lines) == 1

    def test_unknown_mode_behaves_as_fit(self) -> None:
        """Configuration validates the value, so anything else defaults safely."""
        assert layout.plan(WRAPS, "nonsense").kind == layout.plan(WRAPS, "fit").kind


class TestPlanSanitisation:
    """Planning happens against exactly the characters the device will draw."""

    def test_message_is_sanitised(self) -> None:
        """Emoji are gone by the time a layout exists."""
        assert layout.plan("Go with the flow 🌊").lines == ["Go with the flow"]

    def test_message_of_only_emoji_becomes_a_placeholder(self) -> None:
        """Empty text is rejected by the firmware, so a visible marker is used."""
        plan = layout.plan("🌊🎉")
        assert plan.lines == ["?"]

    def test_measurement_uses_the_sanitised_text(self) -> None:
        """Font choice reflects the drawn text, not the original.

        Sanitising after measuring would pick a font for a longer string than
        is actually drawn, wasting panel space.
        """
        assert layout.plan("Ship it 🌊").font == layout.plan("Ship it").font


class TestLayoutObject:
    """The resolved plan itself."""

    def test_static_layout_is_not_scrolling(self) -> None:
        """``scrolling`` distinguishes still layouts from moving ones."""
        assert layout.plan(SHORT).scrolling is False

    @pytest.mark.parametrize("mode", ["fit", "scroll"])
    def test_overflowing_layouts_scroll(self, mode: str) -> None:
        """Both kinds of overflow report as scrolling."""
        assert layout.plan(TOO_TALL, mode).scrolling is True

    def test_visible_lines_is_capped_by_the_panel(self) -> None:
        """A too-tall layout reports only what is on screen at rest."""
        plan = layout.plan(TOO_TALL)
        assert plan.visible_lines == fonts.max_lines(plan.font)
        assert plan.visible_lines < len(plan.lines)

    def test_hint_marks_that_more_follows(self) -> None:
        """The held view ends in an ellipsis so the reader expects movement."""
        plan = layout.plan(TOO_TALL)
        hinted = plan.with_more_hint()
        assert len(hinted) == plan.visible_lines
        assert hinted[-1].endswith(layout.MORE_HINT)

    def test_hinted_lines_still_fit(self) -> None:
        """Adding the dots never pushes a line past the panel edge."""
        plan = layout.plan(TOO_TALL)
        for line in plan.with_more_hint():
            assert fonts.text_width(line, plan.font) <= fonts.USABLE_WIDTH

    def test_hint_trims_a_full_line_to_make_room(self) -> None:
        """When the dots do not fit, characters are dropped to make space."""
        wide = "X" * 40
        plan = layout.Layout("vscroll", "tiny", [wide, wide, wide, wide])
        hinted = plan.with_more_hint()
        assert hinted[-1].endswith(layout.MORE_HINT)
        assert len(hinted[-1]) < len(wide) + len(layout.MORE_HINT)

    def test_hint_leaves_earlier_lines_untouched(self) -> None:
        """Only the last visible line is modified."""
        plan = layout.plan(TOO_TALL)
        assert plan.with_more_hint()[:-1] == plan.lines[: plan.visible_lines - 1]

    def test_layout_is_immutable(self) -> None:
        """A layout cannot drift between the frames of an animation."""
        with pytest.raises(Exception):
            layout.plan(SHORT).font = "tiny"  # type: ignore[misc]
