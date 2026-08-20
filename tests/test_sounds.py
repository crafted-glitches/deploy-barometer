# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.

"""Tests for the synthesised 8-bit verdict sounds.

Audio cannot be assessed by listening in CI, so these tests verify the
properties that make the bytes *playable* rather than pleasant: the exact PCM
format the firmware requires, the envelope that prevents clicks, and the
determinism that the upload-skipping logic depends on.

The format is not a preference. The bar plays headerless signed 16-bit
little-endian mono at 44.1 kHz; anything else is noise or silence.
"""

from __future__ import annotations

import struct
from itertools import pairwise

import pytest

from barometer import sounds


def samples(pcm: bytes) -> list[int]:
    """Decode raw PCM bytes into signed 16-bit sample values.

    Args:
        pcm: Raw little-endian 16-bit mono audio.

    Returns:
        One integer per sample, in order.
    """
    return list(struct.unpack(f"<{len(pcm) // 2}h", pcm))


class TestSquare:
    """The square-wave tone generator."""

    def test_length_matches_requested_duration(self) -> None:
        """Byte count follows exactly from duration, rate and sample width."""
        pcm = sounds._square(440, 100)
        assert len(pcm) == 2 * int(sounds.SAMPLE_RATE * 100 / 1000)

    def test_samples_are_16_bit(self) -> None:
        """Every sample is two bytes, so the buffer length is always even."""
        assert len(sounds._square(440, 50)) % 2 == 0

    def test_envelope_starts_and_ends_near_silence(self) -> None:
        """Both ends ramp, which is what stops audible clicks between notes."""
        values = samples(sounds._square(440, 100))
        assert abs(values[0]) < 1000
        assert abs(values[-1]) < 1000

    def test_tone_reaches_full_amplitude_in_the_middle(self) -> None:
        """The ramps are short; the body of the note plays at volume."""
        values = samples(sounds._square(440, 100))
        peak = max(abs(v) for v in values)
        assert peak > 0.9 * 32767 * sounds._AMPLITUDE

    def test_samples_stay_within_16_bit_range(self) -> None:
        """No value can wrap around into a loud glitch."""
        for value in samples(sounds._square(440, 60, volume=1.0)):
            assert -32768 <= value <= 32767

    def test_wave_is_bipolar(self) -> None:
        """A square wave swings both ways; a one-sided wave is a DC offset."""
        values = samples(sounds._square(440, 60))
        assert max(values) > 0
        assert min(values) < 0

    def test_volume_scales_amplitude(self) -> None:
        """Quieter requests produce proportionally smaller samples."""
        loud = max(abs(v) for v in samples(sounds._square(440, 60, volume=0.4)))
        quiet = max(abs(v) for v in samples(sounds._square(440, 60, volume=0.2)))
        assert quiet < loud

    def test_duty_cycle_shifts_the_positive_fraction(self) -> None:
        """A narrow duty spends less of each cycle positive, which is the buzz."""
        even = samples(sounds._square(440, 100, duty=0.5))
        narrow = samples(sounds._square(440, 100, duty=0.2))
        assert sum(v > 0 for v in narrow) < sum(v > 0 for v in even)

    def test_higher_frequency_crosses_zero_more_often(self) -> None:
        """Pitch is real, not decorative."""
        def crossings(pcm: bytes) -> int:
            """Count sign changes, which scale with frequency."""
            values = samples(pcm)
            return sum((a >= 0) != (b >= 0) for a, b in pairwise(values))

        assert crossings(sounds._square(880, 100)) > crossings(sounds._square(220, 100))


class TestSilence:
    """The inter-note gap generator."""

    def test_is_entirely_silent(self) -> None:
        """Every sample is zero, so no envelope is needed."""
        assert set(samples(sounds._silence(50))) == {0}

    def test_length_matches_requested_duration(self) -> None:
        """Gaps use the same timing arithmetic as tones."""
        assert len(sounds._silence(100)) == 2 * int(sounds.SAMPLE_RATE * 100 / 1000)


class TestCues:
    """The two finished verdict sounds."""

    @pytest.mark.parametrize(
        ("build", "expected_ms"),
        [(sounds.go_ahead, 390), (sounds.stop_right_there, 690)],
    )
    def test_durations_are_as_documented(self, build, expected_ms: int) -> None:
        """Total length matches the documented note sequence."""
        duration = len(build()) / 2 / sounds.SAMPLE_RATE * 1000
        assert round(duration) == expected_ms

    def test_rejection_is_longer_than_approval(self) -> None:
        """A refusal is deliberately harder to miss than an approval."""
        assert len(sounds.stop_right_there()) > len(sounds.go_ahead())

    def test_generation_is_deterministic(self) -> None:
        """Identical bytes every time.

        The upload logic compares file *sizes* to decide whether the device is
        already up to date. That shortcut is only valid because generation is
        reproducible.
        """
        assert sounds.go_ahead() == sounds.go_ahead()
        assert sounds.stop_right_there() == sounds.stop_right_there()

    def test_cues_are_distinguishable(self) -> None:
        """The two answers do not sound the same."""
        assert sounds.go_ahead() != sounds.stop_right_there()

    def test_rejection_contains_true_silence(self) -> None:
        """The gaps separating its notes are really silent."""
        assert b"\x00\x00" * 100 in sounds.stop_right_there()


class TestRegistry:
    """The filename-to-builder mapping used at upload time."""

    def test_expected_assets_are_registered(self) -> None:
        """These names are what the verdict asks the device to play."""
        assert set(sounds.SOUNDS) == {"go.snd", "stop.snd"}

    def test_values_are_builders_not_bytes(self) -> None:
        """Nothing is synthesised at import time, so importing stays instant."""
        for build in sounds.SOUNDS.values():
            assert callable(build)

    def test_each_builder_produces_audio(self) -> None:
        """Every registered entry yields non-empty, well-formed PCM."""
        for name, build in sounds.SOUNDS.items():
            data = build()
            assert len(data) > 0, name
            assert len(data) % 2 == 0, name
