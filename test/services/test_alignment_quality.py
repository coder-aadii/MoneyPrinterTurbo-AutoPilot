import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import alignment_quality, animated_explainer, generation_settings, hindi_script, subtitle, task, task_artifacts
from app.services.state import MemoryState


def measurements(count=100):
    words = [{"word": "शब्द", "start": round(.4 + i * .32, 3),
              "end": round(.6 + i * .32, 3), "score": .8} for i in range(count)]
    return {"audio_duration": words[-1]["end"] + .2, "words": words}


def evaluate(data, policy="balanced"):
    return alignment_quality.evaluate(data, " ".join(w["word"] for w in data["words"]), policy)


def test_real_marginal_be_timing_is_accepted_unchanged_only_in_balanced():
    data = measurements()
    data["words"][14] = {"word": "बे", "start": 4.882, "end": 4.982, "score": .338}
    original = copy.deepcopy(data)
    report = evaluate(data)
    assert report["accepted"]
    assert report["warnings"] == [{"word_index": 15, **data["words"][14],
                                   "code": "marginal_confidence",
                                   "message": "original CTC timing retained; pronunciation is uncertain"}]
    assert data == original
    assert not evaluate(data, "strict")["accepted"]


@pytest.mark.parametrize("word,start,end,score,index", [
    ("सर्वर,", 13.187, 13.447, .257, 40),
    ("हैं", 8.426, 8.487, .005, 25),
])
def test_lower_confidence_requires_review_in_both_policies(word, start, end, score, index):
    data = measurements()
    data["words"][index] = dict(word=word, start=start, end=end, score=score)
    for policy in ("balanced", "strict"):
        report = evaluate(data, policy)
        assert not report["accepted"]
        assert any(issue.get("score") == score for issue in report["confidence_errors"])


@pytest.mark.parametrize("change", [
    {"start": float("nan")}, {"end": float("inf")}, {"start": -1},
    {"end": 1.0}, {"end": 99}, {"start": .599},
])
def test_balanced_never_accepts_broken_timestamps(change):
    data = measurements()
    data["words"][2].update(change)
    report = evaluate(data)
    assert not report["accepted"]
    assert report["structural_errors"]


def test_missing_or_changed_words_fail_even_with_high_scores():
    data = measurements()
    script = " ".join(w["word"] for w in data["words"])
    data["words"].pop()
    assert not alignment_quality.evaluate(data, script)["accepted"]
    data["words"].append({"word": "अलग", "start": 32.08, "end": 32.28, "score": .99})
    assert not alignment_quality.evaluate(data, script)["accepted"]


def test_missing_raw_character_boundary_fails_structure_even_if_word_score_is_high():
    data = measurements()
    data["character_word_coverage"] = [
        {"characters": word["word"], "character_count": 2,
         "measured_character_count": 1 if i == 5 else 2}
        for i, word in enumerate(data["words"])
    ]
    report = evaluate(data)
    assert not report["accepted"]
    assert any(issue["code"] == "missing_character_alignment"
               for issue in report["structural_errors"])


def test_policy_defaults_to_balanced_for_tasks_and_profiles():
    from app.models.channel_profile import ChannelProfile

    assert VideoParams(video_subject="test").hindi_alignment_policy == "balanced"
    assert ChannelProfile(channel_id="test", display_name="test").hindi_alignment_policy == "balanced"


@pytest.mark.parametrize("count,indices", [(100, [10, 20, 30, 40]), (10, [3]), (100, [10, 11])])
def test_aggregate_limits_reject_count_proportion_and_consecutive_words(count, indices):
    data = measurements(count)
    for index in indices:
        data["words"][index]["score"] = .338
    report = evaluate(data)
    assert not report["accepted"]
    assert any(issue["code"] == "aggregate_confidence" for issue in report["confidence_errors"])
    assert len(report["low_confidence_words"]) == len(indices)


@pytest.mark.parametrize("kind", ["gap", "tail", "sparse"])
def test_temporal_coverage_fails_for_substantially_uncovered_audio(kind):
    data = measurements()
    if kind == "gap":
        for word in data["words"][50:]:
            word["start"] += 10
            word["end"] += 10
        data["audio_duration"] += 10
    elif kind == "tail":
        data["audio_duration"] += 20
    else:
        for word in data["words"]:
            word["end"] = word["start"] + .001
    report = evaluate(data)
    assert not report["accepted"]
    assert any(issue["code"] == "temporal_coverage" for issue in report["structural_errors"])


def test_cached_alignment_revalidates_policy_and_preserves_display_map(tmp_path):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"local fixture audio")
    data = measurements()
    data["words"][14] = {"word": "बे", "start": 4.882, "end": 4.982, "score": .338}
    data["words"][40]["word"] = "सर्वर"
    script = " ".join(w["word"] for w in data["words"])

    def run_worker(args, **kwargs):
        Path(args[-1]).write_text(json.dumps(data), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    with patch.object(subtitle, "_hindi_alignment_environment", return_value={"MPT_HINDI_ALIGNMENT_PYTHON": sys.executable}), \
            patch.object(subtitle.subprocess, "run", side_effect=run_worker) as worker:
        captions = tmp_path / "subtitle.srt"
        assert subtitle.align_hindi_words(str(audio), script, str(captions))
        assert not subtitle.align_hindi_words(str(audio), script, str(tmp_path / "strict.srt"), policy="strict")
        worker.assert_called_once()
        report = json.loads((tmp_path / "alignment-quality.json").read_text())
        assert report["cache_hit"] and not report["accepted"]
        assert report["words"] == data["words"]
        cues = animated_explainer._read_word_cues(str(captions))
        mapping = [{"text": "Server" if i == 40 else w["word"], "spoken_start": i, "spoken_end": i + 1}
                   for i, w in enumerate(data["words"])]
        displayed = animated_explainer._display_cues(cues, mapping)
        assert len(displayed) == len(data["words"])
        assert displayed[14] == (4.882, 4.982, "बे")
        assert displayed[40] == (cues[40][0], cues[40][1], "Server")
        audio.write_bytes(b"different narration")
        assert subtitle.align_hindi_words(str(audio), script, str(captions))
        assert worker.call_count == 2


def test_worker_errors_are_reported_and_never_emit_captions(tmp_path):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    with patch.object(subtitle, "_hindi_alignment_environment", return_value={"MPT_HINDI_ALIGNMENT_PYTHON": sys.executable}), \
            patch.object(subtitle.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stderr="Hindi alignment model cannot represent script characters: ['x']")):
        assert not subtitle.align_hindi_words(str(audio), "x", str(tmp_path / "subtitle.srt"))
    report = json.loads((tmp_path / "alignment-quality.json").read_text())
    assert "cannot represent" in report["structural_errors"][0]["message"]
    assert not (tmp_path / "subtitle.srt").exists()


def test_render_retry_reuses_gemini_audio_but_changed_synthesis_inputs_do_not(tmp_path):
    params = VideoParams(video_subject="Test", video_script="तू है क्लाइंट।", video_language="hi",
                         voice_name="gemini:Puck-Upbeat", subtitle_display_mode="word_by_word")

    def synthesize(**kwargs):
        Path(kwargs["voice_file"]).write_bytes(b"successful narration")
        return SimpleNamespace()

    with patch.object(task.utils, "task_dir", return_value=str(tmp_path)), \
            patch.object(task.sm, "state", MemoryState()), \
            patch.object(task.voice, "get_audio_duration", return_value=40.04), \
            patch.object(task.voice, "tts", side_effect=synthesize) as tts:
        first = task.generate_audio("same-task", params, params.video_script)
        retry = task.generate_audio("same-task", params, params.video_script)
        assert retry[0:2] == first[0:2]
        tts.assert_called_once()
        changed_params = VideoParams(
            video_subject="Test", video_script="तू है क्लाइंट।", video_language="hi",
            voice_name="gemini:Puck-Upbeat", voice_rate=.9,
            subtitle_display_mode="word_by_word",
        )
        task.generate_audio("same-task", changed_params, changed_params.video_script)
        assert tts.call_count == 2
        settings = generation_settings.resolve_task_settings(changed_params)
        key = task._narration_cache_key(
            hindi_script.build_representations(changed_params.video_script, "hi").spoken_script,
            settings, (None, None, b""), params=changed_params,
        )
        (tmp_path / "audio.mp3").write_bytes(b"corrupted")
        assert task_artifacts.load_cached_artifact("same-task", "audio", key) is None


def test_artifact_cache_rejects_changed_bytes_and_inputs(tmp_path):
    artifact = tmp_path / "background.mp4"
    artifact.write_bytes(b"validated output")
    with patch.object(task_artifacts.utils, "task_dir", return_value=str(tmp_path)):
        task_artifacts.cache_artifact("task", "explainer", "inputs", str(artifact))
        assert task_artifacts.load_cached_artifact("task", "explainer", "inputs")
        assert task_artifacts.load_cached_artifact("task", "explainer", "other-inputs") is None
        artifact.write_bytes(b"partial render")
        assert task_artifacts.load_cached_artifact("task", "explainer", "inputs") is None
