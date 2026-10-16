from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from choicer_voicer_pack_creator import automation as automation_module
from choicer_voicer_pack_creator.analysis import AnalysisResult, AnalysisSuggestion, HardwareProfile
from choicer_voicer_pack_creator.automation import (
    HeadlessProjectAccess,
    MergeGroup,
    PackAutomation,
    heard_text,
)
from choicer_voicer_pack_creator.media import MediaInfo
from choicer_voicer_pack_creator.models import PackProject, Segment, SourceCaption
from choicer_voicer_pack_creator.segment_review import compare_transcript, review_project
from choicer_voicer_pack_creator.speaker_matching import (
    SpeakerClip,
    SpeakerEvidence,
    SpeakerEvidenceResult,
    SpeakerMatch,
    speaker_evidence,
)

HARDWARE = HardwareProfile(4, None, None, "base", "test")


class StubMedia:
    def probe(self, _source: Path) -> MediaInfo:
        return MediaInfo(10, 320, 180, 30, True, "mpeg4", "aac", "yuv420p", 48000, 1)


@pytest.fixture
def service(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source fixture")
    result = PackAutomation(HeadlessProjectAccess(), tmp_path / "analysis", StubMedia())
    result.new_project(str(source), "Pipeline", ["Tester"])
    return result


def add_segments(service, *rows):
    project = service.access.current.project
    for start, end, caption, characters in rows:
        project.segments.append(Segment(start, end, caption, list(characters)))
    project.sort_segments()
    return [segment.id for segment in project.segments]


def test_review_finds_split_sentences_and_timing_problems():
    project = PackProject(video_duration=60)
    project.segments = [
        Segment(0.0, 1.5, "When I was young", ["Ann"]),
        Segment(1.55, 3.0, "my father told me", ["Ann"]),
        Segment(3.05, 4.5, "to never give up.", []),
        Segment(5.0, 5.3, "This is a very long caption crammed into no time", ["Bob"]),
        Segment(6.0, 12.0, "Hi.", ["Bob"]),
        Segment(13.0, 14.0, "[Music]", ["Bob"]),
        Segment(15.0, 16.0, "Different speaker", ["Ann"]),
        Segment(16.1, 17.0, "continues here.", ["Bob"]),
    ]
    review = review_project(project)
    candidates = review["merge_candidates"]
    assert candidates[0]["segment_ids"] == [item.id for item in project.segments[:3]]
    assert candidates[0]["joined_caption"] == "When I was young my father told me to never give up."
    # Different named speakers are never proposed for merging.
    assert all(project.segments[6].id not in item["segment_ids"] for item in candidates)
    kinds = {item["index"]: item["kinds"] for item in review["issues"]}
    assert "range_may_cut_speech" in kinds[3]
    assert "too_short" in kinds[3]
    assert "range_may_include_silence" in kinds[4]
    assert "nonverbal_caption" in kinds[5]
    assert "missing_speaker" in kinds[2]


def test_review_does_not_merge_sound_captions_and_treats_ellipsis_as_continuation():
    project = PackProject(video_duration=20)
    project.segments = [
        Segment(5.2, 8.0, "they have really...", []),
        Segment(8.0, 12.6, "Really long trunks", []),
        Segment(12.6, 14.4, "and that's cool", []),
        Segment(14.4, 16.0, "(baaaaahhh!!)", []),
    ]
    review = review_project(project)
    assert [item["indexes"] for item in review["merge_candidates"]] == [[0, 1, 2]]
    kinds = {item["index"]: item["kinds"] for item in review["issues"]}
    assert "nonverbal_caption" in kinds[3]


def test_review_flags_rolling_duplicates_and_unpunctuated_captions():
    project = PackProject(video_duration=60)
    project.segments = [
        Segment(0, 2, "so we went down to the", []),
        Segment(2, 4, "down to the river and", []),
        Segment(4, 6, "down to the river and", []),
    ]
    review = review_project(project)
    kinds = {item["index"]: item["kinds"] for item in review["issues"]}
    assert "repeats_words_into_next" in kinds[0]
    assert "duplicate_of_next" in kinds[1]
    assert review["notes"]


def test_compare_transcript_reports_clipped_and_extra_speech():
    assert compare_transcript("Hello there my friend", "hello there my friend")["flags"] == []
    clipped = compare_transcript("Hello there my friend", "there my friend")
    assert "start_may_be_clipped" in clipped["flags"]
    end = compare_transcript("Hello there my friend", "hello there my")
    assert "end_may_be_clipped" in end["flags"]
    extra = compare_transcript("Hello there", "hello there what are you doing")
    assert "extra_speech_at_end" in extra["flags"]
    wrong = compare_transcript("I love pancakes", "the quick brown fox jumps")
    assert "caption_mismatch" in wrong["flags"]
    assert compare_transcript("Hi", "")["flags"] == ["no_speech_recognized"]


def test_heard_text_maps_tokens_back_to_concatenated_pieces(tmp_path):
    transcript = tmp_path / "t.json"
    transcript.write_text(json.dumps({"transcription": [
        {"offsets": {"from": 1500, "to": 4500}, "text": " Hello world. Bye", "tokens": [
            {"text": "[_BEG_]", "offsets": {"from": 1500, "to": 1500}},
            {"text": " Hello", "offsets": {"from": 1600, "to": 2000}},
            {"text": " world.", "offsets": {"from": 2000, "to": 2600}},
            {"text": " Bye", "offsets": {"from": 4100, "to": 4400}},
        ]},
        {"offsets": {"from": 4400, "to": 4900}, "text": " [BLANK_AUDIO]"},
    ]}), encoding="utf-8")
    assert heard_text(transcript, [(1.5, 3.0), (4.0, 5.0)]) == ["Hello world.", "Bye"]


def test_merge_and_split_segments(service):
    ids = add_segments(
        service, (0.0, 1.0, "When I was", ["Ann"]), (1.1, 2.0, "young.", ["Ann"]),
        (3.0, 5.0, "First part second part", ["Bob"]),
    )
    revision = service.get_project()["revision"]
    merged = service.merge_segments(
        [MergeGroup(segment_ids=ids[:2], caption="When I was young.")], revision,
    )
    assert merged["total_segments"] == 2
    combined = merged["segments"][0]
    assert (combined["start"], combined["end"], combined["caption"]) == (0.0, 2.0, "When I was young.")
    assert combined["characters"] == ["Ann"]
    with pytest.raises(ValueError, match="changed"):
        service.merge_segments([MergeGroup(segment_ids=ids[:2])], revision)
    split = service.split_segment(ids[2], 4.0, "First part", "second part", merged["revision"])
    first, second = (
        next(item for item in split["segments"] if item["id"] == segment_id)
        for segment_id in split["changed_ids"]
    )
    assert (first["start"], first["end"], first["caption"]) == (3.0, 4.0, "First part")
    assert (second["start"], second["end"], second["caption"]) == (4.0, 5.0, "second part")
    assert second["characters"] == ["Bob"]
    with pytest.raises(ValueError, match="inside"):
        service.split_segment(ids[2], 3.01, "a", "b", split["revision"])


def test_project_summary_omits_bulky_captions_and_pages_them(service):
    service.access.current.project.source_captions = [
        SourceCaption(index, index + 1, f"line {index}", "YouTube") for index in range(5)
    ]
    summary = service.get_project()
    assert "source_captions" not in summary["project"]
    assert summary["project"]["source_caption_count"] == 5
    page = service.get_source_captions(offset=3, limit=10)
    assert [item["text"] for item in page["captions"]] == ["line 3", "line 4"]
    assert page["next_offset"] is None
    assert "fragments" in service.get_source_captions(include_fragments=True)["captions"][0]


def test_draft_segments_from_captions_and_whisper(service, monkeypatch):
    captions = [SourceCaption(1, 2, "Hello there", "YouTube"), SourceCaption(2, 3, "[Music]", "YouTube")]
    service.access.current.project.source_captions = captions
    calls = []

    def fake_analyze(*_args, **kwargs):
        calls.append(kwargs)
        if kwargs["use_whisper"]:
            return AnalysisResult(
                [AnalysisSuggestion(4, 5, "Whisper line", "Whisper"), AnalysisSuggestion(6, 7, "", "Untranscribed activity")],
                1, 1, None, "base", "en", HARDWARE,
            )
        return AnalysisResult([], 1, 0, None, None, None, HARDWARE, refined_captions=captions)

    monkeypatch.setattr(automation_module, "analyze_video", fake_analyze)
    revision = service.get_project()["revision"]
    with pytest.raises(ValueError, match="allow_download"):
        service.draft_segments(revision, "whisper", False, False, "base", "auto", "balanced", True,
                               lambda *_: None, lambda: False)
    drafted = service.draft_segments(revision, "captions", False, False, "base", "auto", "balanced",
                                     True, lambda *_: None, lambda: False)
    assert [item["caption"] for item in drafted["segments"]] == ["Hello there"]
    assert drafted["skipped_rows"] == 1
    assert calls[0]["source_captions"] == captions and not calls[0]["use_whisper"]
    with pytest.raises(ValueError, match="replace_existing"):
        service.draft_segments(drafted["revision"], "captions", False, False, "base", "auto",
                               "balanced", True, lambda *_: None, lambda: False)
    replaced = service.draft_segments(drafted["revision"], "whisper", True, True, "base", "auto",
                                      "balanced", True, lambda *_: None, lambda: False)
    assert [item["caption"] for item in replaced["segments"]] == ["Whisper line"]


def test_import_youtube_creates_captioned_project(service, tmp_path, monkeypatch):
    from choicer_voicer_pack_creator import youtube

    video = tmp_path / "downloads" / "YouTube-abc" / "source.mp4"
    calls = []

    def fake_download(media, url, folder, language, *, progress, cancelled, existing=None, overwrite=False):
        calls.append((folder, existing, overwrite))
        if existing is None:
            return youtube.YouTubeImportConflict((youtube.ExistingYouTubeImport(
                folder / "YouTube-abc", None, video, 10, "",
            ),))
        return youtube.YouTubeDownload(
            video, "Video title", 10, "https://www.youtube.com/watch?v=abc", "en",
            [SourceCaption(1, 2, "Hi", "YouTube")], ["note"],
        )

    monkeypatch.setattr(youtube, "download_youtube", fake_download)
    arguments = (str(tmp_path / "downloads"), None, ["Me"], "en")
    with pytest.raises(ValueError, match="if_exists"):
        service.import_youtube("https://youtu.be/abc", *arguments, "error", lambda *_: None, lambda: False)
    result = service.import_youtube(
        "https://youtu.be/abc", *arguments, "reuse", lambda *_: None, lambda: False,
    )
    assert calls[-1][1] is not None and not calls[-1][2]
    assert result["project"]["title"] == "Video title"
    assert result["project"]["authors"] == ["Me"]
    assert result["project"]["source_caption_count"] == 1
    assert result["import_notes"] == ["note"]
    assert len(service.access.list_projects()["projects"]) == 3


def test_speaker_evidence_flags_leave_one_out_similarity():
    a, b, c = np.eye(256, dtype=np.float32)[:3]
    signatures = {"a1": a, "a2": a + 0.05 * b, "b1": b, "x": a + 0.01 * b, "c1": c}
    clips = tuple(SpeakerClip(name, "file", 0, 3, chars) for name, chars in (
        ("a1", ("Ann",)), ("a2", ("Ann",)), ("b1", ("Bob",)), ("x", ("Bob",)), ("c1", ("Cat",)),
    ))
    evidence = {item.segment_id: dict(item.scores) for item in speaker_evidence(clips, signatures)}
    # x is labeled Bob but its leave-one-out Bob prototype is b1, far from x.
    assert evidence["x"]["Ann"] > evidence["x"]["Bob"]
    # A character with a single reference cannot be scored against itself.
    assert "Cat" not in evidence["c1"]


def test_match_speakers_reports_and_applies(service, monkeypatch):
    from choicer_voicer_pack_creator import speaker_matching

    ids = add_segments(
        service, (0, 2.5, "Ann line", ["Ann"]), (3, 5.5, "Bob line", ["Bob"]),
        (6, 8.5, "Unknown", []), (9, 11.5, "Wrongly Bob", ["Bob"]),
    )
    seen = {}

    def fake_evidence(self, media, clips, *, allow_download, progress, cancelled):
        seen["clips"] = clips
        return SpeakerEvidenceResult(
            (SpeakerMatch(ids[2], "Ann", 0.7),),
            (
                SpeakerEvidence(ids[2], (("Ann", 0.7), ("Bob", 0.2))),
                SpeakerEvidence(ids[3], (("Ann", 0.8), ("Bob", 0.3))),
                SpeakerEvidence(ids[0], (("Bob", 0.2),)),
            ),
            None, 4, 0,
        )

    monkeypatch.setattr(speaker_matching.SpeakerMatchingManager, "evidence", fake_evidence)
    revision = service.get_project()["revision"]
    report = service.match_speakers(revision, False, False, False, lambda *_: None, lambda: False)
    flags = {row["segment_id"]: (row["flag"], row["suggested"]) for row in report["segments"]}
    assert flags == {
        ids[2]: ("suggested_match", "Ann"), ids[3]: ("possible_misattribution", "Ann"),
    }
    assert report["revision"] == revision
    applied = service.match_speakers(revision, False, True, False, lambda *_: None, lambda: False)
    assert applied["applied_ids"] == [ids[2]]
    segment = service.access.current.project.segment_by_id(ids[2])
    assert (segment.characters, segment.speaker_assignment) == (["Ann"], "automatic")


def test_match_speakers_requires_references(service):
    add_segments(service, (0, 2.5, "Line", []))
    with pytest.raises(ValueError, match="Name at least one"):
        service.match_speakers(service.get_project()["revision"], False, False, False,
                               lambda *_: None, lambda: False)


def test_generate_backing_track_selects_result(service, tmp_path, monkeypatch):
    from choicer_voicer_pack_creator import separation

    backing = tmp_path / "backing.wav"
    backing.write_bytes(b"wav")
    captured = {}

    def fake_generate(self, media, video, **kwargs):
        captured.update(kwargs, root=self.data_root, mode=self.mode)
        return backing

    monkeypatch.setattr(separation.SeparationManager, "generate", fake_generate)
    result = service.generate_backing_track(
        service.get_project()["revision"], "remove_all_vocals", True, False,
        lambda *_: None, lambda: False,
    )
    assert result["project"]["backing_track_path"] == str(backing)
    assert result["dirty"]
    assert captured["root"] == (tmp_path / "backing").resolve()
    assert captured["allow_download"] and not captured["offer_runtime_download"]


def test_transcribe_segments_compares_heard_text(service, monkeypatch):
    ids = add_segments(service, (1, 2, "Hello world", ["Ann"]), (3, 4, "Goodbye now", ["Ann"]))

    def fake_preview(source, start, duration, output):
        with wave.open(str(output), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(b"\0\0" * int(16000 * duration))
        return b""

    def fake_transcribe(self, wav, folder, model, language, hardware, progress, cancelled):
        with wave.open(str(wav), "rb") as reader:
            assert abs(reader.getnframes() - 16000 * (1.5 * 3 + 1.4 * 2)) <= 4
        (folder / "whisper-transcript.json").write_text(json.dumps({"transcription": [
            {"offsets": {"from": 1600, "to": 2800}, "text": " Hello world"},
            {"offsets": {"from": 4600, "to": 5000}, "text": " Goodbye"},
        ]}), encoding="utf-8")
        return [], "en"

    monkeypatch.setattr(service, "_wav_preview", fake_preview)
    monkeypatch.setattr(automation_module.WhisperManager, "transcribe", fake_transcribe)
    monkeypatch.setattr(automation_module, "detect_hardware", lambda: HARDWARE)
    with pytest.raises(ValueError, match="allow_download"):
        service.transcribe_segments(None, False, "base", "auto", lambda *_: None, lambda: False)
    result = service.transcribe_segments(None, True, "base", "auto", lambda *_: None, lambda: False)
    rows = {row["segment_id"]: row for row in result["segments"]}
    assert rows[ids[0]]["heard"] == "Hello world" and rows[ids[0]]["flags"] == []
    assert "end_may_be_clipped" in rows[ids[1]]["flags"]
    assert result["flagged"] == 1
