"""Topic-independent storyboard planning and render safety."""

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.schema import VideoParams
from app.services import animated_explainer, emotion_storyboard, material, scene_spec, task


@pytest.mark.parametrize(("narration", "expected"), [
    ("जेलीफ़िश अपनी शुरुआती अवस्था में वापस बदल सकती है।", "transformation"),
    ("बादल का वजन विमान की तुलना में अधिक हो सकता है।", "comparison"),
    ("Client request भेजता है। Server response देता है।", "flow"),
    ("ज्वालामुखी फटता है और फिर लावा नीचे बहता है।", "flow"),
    ("मस्तिष्क में न्यूरॉन संदेश भेजते हैं।", "flow"),
    ("पुराने व्यापार मार्ग भारत से रोम तक जाते थे।", "timeline"),
])
def test_unrelated_subjects_produce_versioned_scene_data(narration, expected):
    beats = emotion_storyboard.build(narration)
    assert beats
    assert any(beat["scene_spec"]["primitive"] == expected for beat in beats)
    assert all(beat["top_visual"] == "scene_spec_v1" for beat in beats)
    assert all(scene_spec.validate_scene_spec(beat["scene_spec"]).version == 1 for beat in beats)
    assert beats[-1]["end_word"] == len(narration.split())
    assert json.loads(json.dumps(beats, ensure_ascii=False))[0]["scene_spec"]["version"] == 1


@pytest.mark.parametrize("payload", [
    {"version": 2, "primitive": "flow", "heading": "A", "labels": ["A", "B"]},
    {"version": 1, "primitive": "python", "heading": "A", "labels": ["A"]},
    {"version": 1, "primitive": "label", "heading": "<script>", "labels": ["A"]},
    {"version": 1, "primitive": "label", "heading": "A", "labels": ["<svg>"]},
    {"version": 1, "primitive": "label", "heading": "A", "labels": ["A"], "code": "print(1)"},
])
def test_malformed_or_executable_scene_data_is_rejected(payload):
    with pytest.raises(ValidationError):
        scene_spec.validate_scene_spec(payload)


def test_unknown_subject_has_extractive_illustrative_fallback():
    script = "अज्ञात यंत्र भीतर एक घटना दिखाता है।"
    scene = scene_spec.plan_scene(script)
    assert scene.primitive == "label"
    assert scene.illustrative
    assert scene.stock_query is None
    assert "अज्ञात" in scene.labels[0]


def test_phrase_boundaries_time_scenes_without_claiming_word_alignment():
    script = "पुराने व्यापार मार्ग थे। व्यापारी सामान लाते थे।"
    beats = emotion_storyboard.build(script)
    timed, words = animated_explainer._visual_timeline(
        beats, [(0.2, 1.1, "पुराने व्यापार मार्ग थे।"),
                (1.5, 2.9, "व्यापारी सामान लाते थे।")],
        script, 3.0, .5)
    assert words == []
    assert [(beat["start"], beat["end"]) for beat in timed] == [(.7, 1.6), (2.0, 3.4)]
    assert all(beat["timing_source"] == "measured_phrase_alignment" for beat in timed)


def test_saved_pexels_setting_reaches_storyboard_and_regular_stock_route(tmp_path, monkeypatch):
    script = "समुद्र में एक जीव रहता है।"
    params = VideoParams(video_subject="समुद्री जीव", video_script=script,
                         video_source="pexels", video_language="hi",
                         visual_style="savage_animated_explainer")
    monkeypatch.setattr(task.utils, "task_dir", lambda _task: str(tmp_path))
    task.save_script_data("provider-path", script, ["sea creature"], params)
    saved = json.loads((tmp_path / "script.json").read_text(encoding="utf-8"))
    assert saved["params"]["video_source"] == "pexels"
    beats = emotion_storyboard.schedule_visuals_from_duration(
        saved["emotion_timeline"], 3.0)
    searched = []
    monkeypatch.setattr(animated_explainer, "_cached_stock_candidates", lambda *_args: [])
    monkeypatch.setattr(animated_explainer, "_search_and_cache_stock_candidate",
                        lambda query, provider, _task, **_kw: searched.append(provider) or [])
    resolved, selected = animated_explainer.resolve_storyboard_media(
        "provider-path", beats, provider=saved["params"]["video_source"],
        stock_terms=saved["search_terms"])
    assert searched == ["pexels"]
    assert not selected and resolved[0]["scene_spec"]["primitive"] == "label"

    searched_regular = []
    monkeypatch.setattr(material, "_search_videos_with_cache",
                        lambda **kw: searched_regular.append(kw["provider"]) or [])
    assert material.download_videos(
        "provider-path", ["sea creature"], source=params.video_source,
        audio_duration=2, max_clip_duration=2, match_script_order=True) == []
    assert searched_regular == ["pexels"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required")
def test_unseen_subject_renders_phrase_captions_and_original_timeline(tmp_path, monkeypatch):
    from moviepy import VideoFileClip
    from PIL import Image, ImageChops

    script = "पुराने व्यापार मार्ग भारत से रोम तक जाते थे।"
    subtitle_file = tmp_path / "subtitle.srt"
    subtitle_file.write_text(
        "1\n00:00:00,100 --> 00:00:01,400\n" + script + "\n", encoding="utf-8")
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(tmp_path))
    monkeypatch.setattr(animated_explainer, "_cached_stock_candidates", lambda *_args: [])
    searched = []
    monkeypatch.setattr(animated_explainer, "_search_and_cache_stock_candidate",
                        lambda query, provider, _task, **_kwargs: searched.append(provider) or [])
    output = animated_explainer.render_background(
        "unseen-trade-route", "व्यापार मार्ग", 1.6,
        output_path=str(tmp_path / "trade-route-preview.mp4"),
        subtitle_path=str(subtitle_file), spoken_script=script,
        narration_duration_seconds=1.6,
        emotion_beats=emotion_storyboard.build(script),
        stock_provider="pexels", stock_terms=["ancient trade route"],
        caption_mode="sentence",
    )
    with VideoFileClip(output, audio=False) as clip:
        assert clip.size == [1080, 1920]
        assert abs(clip.duration - 1.6) < .05
        early = Image.fromarray(clip.get_frame(.03)).crop((60, 850, 1020, 1050))
        active = Image.fromarray(clip.get_frame(.5)).crop((60, 850, 1020, 1050))
        assert ImageChops.difference(early, active).getbbox()
    rendered = json.loads((tmp_path / "top-panel-rendered-media.json").read_text())
    assert rendered["caption_mode"] == "sentence"
    assert rendered["sources"][0]["visual"] == "scene_spec_v1"
    # Saved terms are offered to the selected provider even for a timeline;
    # with no eligible candidate the diagram remains the rendered fallback.
    assert searched == ["pexels"]
    assert Path(output).is_file()
