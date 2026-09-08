from __future__ import annotations

import shutil
import subprocess
import sys
import wave
from array import array
from pathlib import Path

import pytest

from choicer_voicer_pack_creator.media import MediaError, MediaTools


def _little_endian(samples: array) -> bytes:
    if sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


@pytest.fixture
def media():
    tools = MediaTools.__new__(MediaTools)
    tools.ffmpeg = "ffmpeg"
    return tools


def _decoded_samples(monkeypatch, media, samples: array) -> None:
    payload = _little_endian(samples)
    monkeypatch.setattr(
        media, "_capture",
        lambda command: subprocess.CompletedProcess(command, 0, payload, b""),
    )


def test_default_waveform_retains_separate_short_transients(monkeypatch, media):
    samples = array("f", [0.0]) * 120_000
    samples[60_000] = 0.9
    samples[60_020] = -0.8
    _decoded_samples(monkeypatch, media, samples)

    peaks = media.waveform_peaks(Path("source.mp4"), 60)

    assert len(peaks) == 120_000
    assert peaks[60_000] == pytest.approx(0.9)
    assert peaks[60_001:60_020] == [0.0] * 19
    assert peaks[60_020] == pytest.approx(0.8)
    assert len(peaks) / 80 >= 1_000


def test_default_waveform_bounds_retained_data_and_preserves_last_sample(monkeypatch, media):
    samples = array("f", [0.0]) * 400_003
    samples[-1] = -0.75
    _decoded_samples(monkeypatch, media, samples)

    peaks = media.waveform_peaks(Path("source.mp4"), len(samples) / 2000)

    assert len(peaks) == 384_000
    assert peaks[-1] == 0.75
    assert not any(peaks[:-1])


def test_waveform_buckets_span_equal_time_intervals(monkeypatch, media):
    _decoded_samples(
        monkeypatch, media, array("f", [0, -0.1, 0, -0.2, 0.3, 0, -0.4, 0, 0.5, -1.2]),
    )

    assert media.waveform_peaks(Path("source.mp4"), 1, target_peaks=4) == pytest.approx(
        [0.1, 0.3, 0.4, 1.0],
    )


def test_short_and_empty_waveforms_do_not_invent_samples(monkeypatch, media):
    _decoded_samples(monkeypatch, media, array("f", [0.25, -0.5]))
    assert media.waveform_peaks(Path("source.mp4"), 1) == [0.25, 0.5]
    _decoded_samples(monkeypatch, media, array("f"))
    assert media.waveform_peaks(Path("source.mp4"), 1) == []


@pytest.mark.parametrize("options", [{"target_peaks": 0}, {"sample_rate": 0}])
def test_waveform_rejects_invalid_resolution(media, options):
    with pytest.raises(ValueError, match="must be positive"):
        media.waveform_peaks(Path("source.mp4"), 1, **options)


def test_waveform_decode_failure_is_reported(monkeypatch, media):
    monkeypatch.setattr(
        media, "_capture",
        lambda command: subprocess.CompletedProcess(command, 1, b"", b"Cannot decode audio"),
    )
    with pytest.raises(MediaError, match="Cannot decode audio"):
        media.waveform_peaks(Path("source.mp4"), 1)


@pytest.mark.integration
def test_decoded_waveform_preserves_transient_timing(tmp_path, media):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg is not installed")
    media.ffmpeg = ffmpeg
    source = tmp_path / "impulses.wav"
    samples = array("h", [0]) * 4_000
    samples[2_000] = 24_576
    samples[2_020] = -16_384
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(2_000)
        output.writeframes(_little_endian(samples))

    peaks = media.waveform_peaks(source, 2)

    assert len(peaks) == 4_000
    assert peaks[2_000] == pytest.approx(0.75)
    assert peaks[2_001:2_020] == [0.0] * 19
    assert peaks[2_020] == pytest.approx(0.5)
