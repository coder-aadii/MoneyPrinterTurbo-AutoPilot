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


def test_balanced_accepts_isolated_low_confidence_word_with_reliable_timing():
    data = measurements()
    data["words"][0] = {"word": "क्या", "start": .14, "end": .24, "score": .166}
    report = evaluate(data)
    assert report["accepted"]
    assert report["warnings"] == [{
        "word_index": 1, **data["words"][0],
        "code": "marginal_confidence",
        "message": "original CTC timing retained; pronunciation is uncertain",
    }]
    assert not evaluate(data, "strict")["accepted"]


def test_balanced_accepts_isolated_interior_low_confidence_word():
    data = measurements()
    data["words"][40]["score"] = .257
    report = evaluate(data)
    assert report["accepted"]
    assert report["warnings"][0]["word_index"] == 41


def test_extremely_low_confidence_still_requires_review():
    data = measurements()
    data["words"][25]["word"] = "हैं"
    data["words"][25]["score"] = .005
    for policy in ("balanced", "strict"):
        report = evaluate(data, policy)
        assert not report["accepted"]
        assert any(issue.get("score") == .005 for issue in report["confidence_errors"])


def test_balanced_rejects_low_confidence_word_at_audio_edge_with_unreliable_timing():
    data = measurements()
    data["words"][0] = {"word": "क्या", "start": .9, "end": 1.0, "score": .166}
    assert not evaluate(data)["accepted"]


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


def test_task_pipeline_falls_back_to_phrase_captions_without_tts(tmp_path):
    params = VideoParams(
        video_subject="Test", video_script="क्या कभी सोचा है?", video_language="hi",
        voice_name="gemini:Puck-Upbeat", subtitle_enabled=True,
        subtitle_display_mode="word_by_word", hindi_alignment_policy="strict",
    )
    state = MemoryState()
    script = params.video_script

    def reject_with_measured_but_uncertain_words(_audio, _script, _destination, *, policy):
        aligned = {
            "audio_duration": 2.0,
            "words": [
                {"word": "क्या", "start": .1, "end": .35, "score": .166},
                {"word": "कभी", "start": .4, "end": .65, "score": .8},
                {"word": "सोचा", "start": .7, "end": 1.15, "score": .8},
                {"word": "है?", "start": 1.2, "end": 1.8, "score": .8},
            ],
        }
        report = alignment_quality.evaluate(aligned, script, policy)
        (tmp_path / "alignment-quality.json").write_text(json.dumps(report), encoding="utf-8")
        return False

    with patch.object(task.utils, "task_dir", return_value=str(tmp_path)), \
            patch.object(task.sm, "state", state), \
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True), \
            patch.object(task.config, "app", dict(task.config.app, subtitle_provider="edge")), \
            patch.object(task, "generate_script", return_value=script), \
            patch.object(task, "_verify_script_for_policy", return_value=script), \
            patch.object(task, "save_script_data"), \
            patch.object(task, "generate_audio", return_value=("audio.mp3", 2, None)), \
            patch.object(task, "_alignment_preflight_errors", return_value=[]), \
            patch.object(subtitle, "align_hindi_words", side_effect=reject_with_measured_but_uncertain_words), \
            patch.object(subtitle, "create") as whisper_create, \
            patch.object(task.voice, "get_audio_duration", return_value=2.0), \
            patch.object(task.voice, "tts") as tts:
        result = task.start("phrase-fallback", params, stop_at="subtitle")

    subtitle_path = result["subtitle_path"]
    assert subtitle_path.endswith("subtitle.srt")
    cues = subtitle.file_to_subtitles(subtitle_path)
    assert len(cues) == 1
    assert cues[0][2] == "क्या कभी सोचा है?"
    assert params.subtitle_display_mode == "sentence"
    assert state.get_task("phrase-fallback")["subtitle_alignment_fallback"]["effective_mode"] == "sentence"
    assert state.get_task("phrase-fallback")["state"] == task.const.TASK_STATE_COMPLETE
    whisper_create.assert_not_called()
    tts.assert_not_called()


def test_unseen_initialism_is_normalized_and_sent_to_hindi_ctc(tmp_path):
    script = "QZX का उदाहरण"
    params = VideoParams(
        video_subject="New acronym", video_script=script,
        video_language="hi", voice_name="gemini:Puck-Upbeat",
        subtitle_enabled=True, subtitle_display_mode="word_by_word",
    )
    state = MemoryState()

    def aligned(_audio, text, target, **_kwargs):
        assert text == "क्यू ज़ेड एक्स का उदाहरण"
        words = text.split()
        (Path(target)).write_text("\n\n".join(
            task.utils.text_to_srt(index, word, index * .2, (index + 1) * .2)
            for index, word in enumerate(words, start=1)
        ) + "\n", encoding="utf-8")
        return True

    with patch.object(task.utils, "task_dir", return_value=str(tmp_path)), \
            patch.object(task.sm, "state", state), \
            patch.object(task.config, "app", dict(task.config.app, subtitle_provider="edge")), \
            patch.object(task, "_alignment_preflight_errors", return_value=[]), \
            patch.object(task.task_artifacts, "patch_script_data"), \
            patch.object(subtitle, "align_hindi_words", side_effect=aligned) as hindi_align:
        result = task.generate_subtitle("initialism", params, script, None, "audio.mp3")

    assert result.endswith("subtitle.srt")
    hindi_align.assert_called_once()
    assert hindi_align.call_args.args[1] == "क्यू ज़ेड एक्स का उदाहरण"


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
