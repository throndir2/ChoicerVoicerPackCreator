from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from choicer_voicer_pack_creator import bandit_runtime, separation_progress, separation_worker
from choicer_voicer_pack_creator.separation_progress import SeparationProgress


def test_estimates_use_completed_chunks_and_exclude_setup(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(separation_progress, "monotonic", lambda: clock[0])
    updates = []
    timing = SeparationProgress(4, "Separating", lambda *update: updates.append(update))
    clock[0] = 500
    timing.start_chunk(0)
    assert "estimating time after the first chunk" in updates[-1][0]
    assert updates[-1][1] == 0
    clock[0] += 20
    timing.start_chunk(1)
    assert "about 1m 0s remaining" in updates[-1][0]
    assert "then writing and verification" in updates[-1][0]
    assert updates[-1][1] == pytest.approx(0.225)
    clock[0] += 40
    timing.start_chunk(2)
    assert "about 1m 0s remaining" in updates[-1][0]
    clock[0] += 120
    timing.start_chunk(3)
    assert "about 1m 0s remaining" in updates[-1][0]
    assert updates[-1][1] == pytest.approx(0.675)


def test_new_run_does_not_reuse_old_timings(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(separation_progress, "monotonic", lambda: clock[0])
    updates = []
    for _attempt in range(2):
        timing = SeparationProgress(2, "Separating", lambda *update: updates.append(update))
        timing.start_chunk(0)
        assert "estimating" in updates[-1][0]
        clock[0] += 10
        timing.start_chunk(1)
        assert "about 10s remaining" in updates[-1][0]
        clock[0] += 100


def test_unmeasurably_fast_chunks_do_not_claim_zero_remaining(monkeypatch):
    monkeypatch.setattr(separation_progress, "monotonic", lambda: 5.0)
    updates = []
    timing = SeparationProgress(2, "Separating", lambda *update: updates.append(update))
    timing.start_chunk(0)
    timing.start_chunk(1)
    assert "remaining" not in updates[-1][0]


@pytest.mark.parametrize("total", [0, -1])
def test_empty_work_is_rejected(total):
    with pytest.raises(ValueError, match="at least one chunk"):
        SeparationProgress(total, "Separating", lambda *_: None)


@pytest.mark.parametrize("index", [-1, 1, 2])
def test_out_of_order_progress_is_rejected(index):
    with pytest.raises(ValueError, match="in order"):
        SeparationProgress(2, "Separating", lambda *_: None).start_chunk(index)


@pytest.mark.parametrize("mode", ["bandit", "htdemucs"])
@pytest.mark.parametrize("frames", [7, 49])
def test_streaming_backends_report_measured_eta_without_changing_audio(
    tmp_path, monkeypatch, mode, frames,
):
    clock = [100.0]
    monkeypatch.setattr(separation_progress, "monotonic", lambda: clock[0])
    samples = np.full((frames, 2), (0.2, -0.3), dtype=np.float32)
    rate = 48000 if mode == "bandit" else 44100
    source, output = tmp_path / "source.wav", tmp_path / "backing.wav"
    sf.write(source, samples, rate, subtype="FLOAT")
    updates = []

    def predict(block):
        clock[0] += 10
        return {"speech": block * 0.4, "music": block * 0.4, "sfx": block * 0.2}

    class Session:
        def run(self, _names, inputs):
            clock[0] += 10
            mix = inputs["mix"]
            return [np.stack([mix * 0.1, mix * 0.2, mix * 0.3, mix * 0.4], axis=1)]

    if mode == "bandit":
        bandit_runtime.separate_stream(
            source, output, predict, frames, lambda *update: updates.append(update),
            lambda: False, chunk_frames=32, hop_frames=24,
        )
    else:
        separation_worker.separate_stream(
            source, output, Session(), frames, lambda *update: updates.append(update),
            lambda: False, chunk_frames=32, overlap_frames=8,
        )
    assert "chunk 1 of" in updates[0][0] and "estimating" in updates[0][0]
    chunks = (frames + 23) // 24
    assert len(updates) == chunks + 1
    for index in range(1, chunks):
        assert f"chunk {index + 1} of {chunks}" in updates[index][0]
        assert f"about {(chunks - index) * 10}s remaining" in updates[index][0]
        assert updates[index][1] == pytest.approx(index / chunks * 0.9)
    assert "Writing full-length" in updates[-1][0]
    assert "remaining" not in updates[-1][0]
    assert updates[-1][1] == 0.9
    actual, actual_rate = sf.read(output, dtype="float32", always_2d=True)
    assert actual_rate == rate
    np.testing.assert_allclose(actual, samples * 0.6, atol=2e-7)
