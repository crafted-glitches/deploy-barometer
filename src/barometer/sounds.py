# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 crafted-glitches
#
# Dual-licensed: AGPL-3.0 , or a commercial licence. See LICENSING.md.
"""Synthesised 8-bit verdict sounds.

The bar plays **headerless raw PCM**: signed 16-bit little-endian, mono, at
44.1 kHz. This was confirmed against the firmware's own stock assets, whose
sizes divide exactly as that format predicts (a 132,300 byte ``.snd`` is
precisely 1.5 seconds of it). There is no RIFF/WAVE header — the bytes are
sample data and nothing else.

Generating the waveforms in code rather than committing audio files keeps the
repository free of binary blobs, makes the sounds diffable and tweakable, and
avoids requiring ffmpeg at build or run time.

Both cues are built from square waves, which is what gives them their chiptune
character: a square wave's odd-harmonic spectrum is the sound of early
programmable sound generators. Lowering the duty cycle below 0.5 thins the
tone into the buzzier, more nasal timbre those chips used for negative
feedback, which is why the rejection sound uses it.

Sizes are modest — roughly 34 KB and 61 KB — so uploading both on startup costs
a fraction of a second over USB.
"""

from __future__ import annotations

import struct

#: Sample rate in Hz. Fixed by the firmware's audio pipeline, not a preference.
SAMPLE_RATE = 44100

#: Peak amplitude as a fraction of full scale. Kept well below 1.0 because a
#: full-scale square wave on a small passive speaker sounds harsh and clips.
_AMPLITUDE = 0.3

# Note frequencies in Hz, equal temperament at A4 = 440.
C5, E5, G5, C6 = 523.25, 659.25, 783.99, 1046.50
G4, E4, D4, A3 = 392.00, 329.63, 293.66, 220.00


def _square(freq: float, ms: int, *, duty: float = 0.5, volume: float = _AMPLITUDE) -> bytes:
    """Render one square-wave tone as raw PCM.

    The waveform is computed per sample rather than by tiling a period, so any
    frequency works regardless of whether its period divides evenly into the
    sample rate.

    A trapezoidal envelope is applied: amplitude ramps up over the first 220
    samples (~5 ms) and down over the final 500 samples (~11 ms). Without it,
    starting or ending mid-cycle leaves a step discontinuity that the speaker
    reproduces as an audible click between notes. The ramp is expressed in
    samples rather than milliseconds because it exists to smooth waveform
    edges, which is a sample-domain concern. Note that for a tone shorter than
    about 16 ms the two ramps overlap and the note never reaches full volume —
    all tones here are far longer than that.

    Args:
        freq: Frequency in Hz.
        ms: Duration in milliseconds. Converted to a whole number of samples,
            truncating any fractional remainder.
        duty: Fraction of each cycle spent at positive amplitude, in ``(0, 1)``.
            ``0.5`` is a pure square; lower values thin the tone and raise its
            perceived buzziness.
        volume: Peak amplitude as a fraction of full scale, in ``[0, 1]``.

    Returns:
        Raw PCM bytes: signed 16-bit little-endian mono samples, exactly
        ``2 * int(SAMPLE_RATE * ms / 1000)`` bytes long.
    """
    total = int(SAMPLE_RATE * ms / 1000)
    attack, release = 220, 500
    out = bytearray()
    for index in range(total):
        phase = (index * freq / SAMPLE_RATE) % 1.0
        level = volume if phase < duty else -volume
        envelope = min(1.0, index / attack, (total - index) / release)
        out += struct.pack("<h", int(32767 * level * envelope))
    return bytes(out)


def _silence(ms: int) -> bytes:
    """Render a gap of pure silence as raw PCM.

    Used to separate notes so a sequence reads as distinct beats rather than
    one continuous sliding tone. Zero-valued samples need no envelope, since
    silence introduces no discontinuity.

    Args:
        ms: Duration in milliseconds, truncated to whole samples.

    Returns:
        Zeroed PCM bytes in the same format as :func:`_square`.
    """
    return b"\x00\x00" * int(SAMPLE_RATE * ms / 1000)


def go_ahead() -> bytes:
    """Build the positive cue: a bright ascending arpeggio.

    A C major arpeggio (C5–E5–G5) climbing to C6, held four times longer than
    the notes leading to it. Rising major intervals resolving upward onto a
    sustained octave is the classic "power-up" gesture, which is why it reads
    as approval without needing any words.

    Total duration is 390 ms (three 70 ms notes and one 180 ms note).

    Returns:
        Raw PCM bytes ready to upload as ``go.snd``.
    """
    return b"".join([
        _square(C5, 70),
        _square(E5, 70),
        _square(G5, 70),
        _square(C6, 180),
    ])


def stop_right_there() -> bytes:
    """Build the negative cue: a descending buzz.

    Falls G4–E4–D4 and lands on a low A3 held for 300 ms. Every tone runs at a
    narrow duty cycle (0.25, dropping to 0.2 on the final note) to make it buzz
    rather than sing. Descending pitch, narrowing duty and a long low tail is
    the "you died" gesture, the inverse of :func:`go_ahead` in every dimension.

    Short silences separate the first three notes so they land as distinct
    beats. Total duration is 690 ms — deliberately longer than the positive
    cue, so a refusal is harder to miss.

    Returns:
        Raw PCM bytes ready to upload as ``stop.snd``.
    """
    return b"".join([
        _square(G4, 110, duty=0.25),
        _silence(25),
        _square(E4, 110, duty=0.25),
        _silence(25),
        _square(D4, 120, duty=0.25),
        _square(A3, 300, duty=0.2),
    ])


#: Asset filename on the device mapped to the function that builds its audio.
#:
#: The values are *callables, not bytes*: nothing is synthesised at import
#: time. :meth:`barometer.busybar.BusyBarDisplay.ensure_sounds` calls each one
#: only when it needs to compare or upload, so importing this module stays
#: instant. Because generation is deterministic, the byte length of a rebuilt
#: waveform is a reliable way to tell whether the device already holds it.
SOUNDS = {
    "go.snd": go_ahead,
    "stop.snd": stop_right_there,
}
