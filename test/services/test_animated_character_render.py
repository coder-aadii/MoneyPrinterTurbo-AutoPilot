import shutil
import json

import numpy as np
import pytest
from moviepy import VideoClip

from app.services import animated_explainer, emotion_storyboard


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required for animated video validation")
def test_canonical_avatar_pack_renders_in_animated_scene(tmp_path, monkeypatch):
    subtitle = tmp_path / "narration.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:00,200\nनमस्ते।\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task_id: str(tmp_path))
    monkeypatch.setattr(animated_explainer.task_artifacts, "load_cached_artifact", lambda *_: None)
    monkeypatch.setattr(animated_explainer.task_artifacts, "cache_artifact", lambda *args, **kwargs: None)

    output = tmp_path / "canonical-avatar-smoke.mp4"
    result = animated_explainer.render_background(
        "canonical-avatar-smoke",
        "एक छोटी हिंदी व्याख्या",
        0.2,
        output_path=str(output),
        subtitle_path=str(subtitle),
        emotion_beats=[{
            "beat_id": "hello", "start_word": 0, "end_word": 1,
            "pose": "neutral", "text": "नमस्ते।",
            "short_visual_title": "हिंदी में नमस्ते",
            "visual_mode": "illustration", "top_visual": "topic_diagram",
            "stock_query": None, "visual_rationale": "Original diagram for a local render check.",
        }],
        display_word_map=[{"spoken_start": 0, "spoken_end": 1, "text": "नमस्ते।"}],
    )

    assert result == str(output)
    assert output.is_file() and output.stat().st_size > 0
    with animated_explainer.VideoFileClip(str(output), audio=False) as clip:
        assert tuple(clip.size) == (animated_explainer.WIDTH, animated_explainer.HEIGHT)
        assert clip.duration == pytest.approx(0.2, abs=1 / animated_explainer.FPS + 0.01)
        assert clip.get_frame(0).shape == (animated_explainer.HEIGHT, animated_explainer.WIDTH, 3)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required for animated video validation")
def test_animated_scene_renders_from_audio_duration_without_subtitle_cues(tmp_path, monkeypatch):
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task_id: str(tmp_path))
    monkeypatch.setattr(animated_explainer.task_artifacts, "load_cached_artifact", lambda *_: None)
    monkeypatch.setattr(animated_explainer.task_artifacts, "cache_artifact", lambda *args, **kwargs: None)
    narration = "Greenland और Iceland के बीच पानी बहता है। नाम Denmark Strait है।"
    output = tmp_path / "mixed-script-no-captions.mp4"

    result = animated_explainer.render_background(
        "mixed-script-render", "समंदर के अंदर झरना", 0.2,
        output_path=str(output), subtitle_path=None, spoken_script=narration,
        narration_duration_seconds=0.2,
        emotion_beats=emotion_storyboard.build(narration),
    )

    assert result == str(output)
    with animated_explainer.VideoFileClip(result, audio=False) as clip:
        assert tuple(clip.size) == (animated_explainer.WIDTH, animated_explainer.HEIGHT)
        assert clip.duration == pytest.approx(.2, abs=1 / animated_explainer.FPS + .01)
        assert clip.get_frame(0).shape == (animated_explainer.HEIGHT, animated_explainer.WIDTH, 3)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required for animated video validation")
def test_saved_branding_video_replaces_full_frame_and_manifest_records_it(tmp_path, monkeypatch):
    branding_path = tmp_path / "saved-intro.mp4"

    def moving_frame(time_seconds):
        frame = np.empty((1920, 1080, 3), dtype=np.uint8)
        frame[:] = (15, 25, 225)
        x = 100 + int(time_seconds * 500)
        frame[400:700, x:x + 180] = (250, 250, 20)
        return frame

    source = VideoClip(moving_frame, duration=0.4)
    source.write_videofile(str(branding_path), fps=24, codec="libx264", audio=False,
                           preset="ultrafast", logger=None)
    source.close()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task_id: str(tmp_path))
    monkeypatch.setattr(animated_explainer.task_artifacts, "load_cached_artifact", lambda *_: None)
    monkeypatch.setattr(animated_explainer.task_artifacts, "cache_artifact", lambda *args, **kwargs: None)
    monkeypatch.setattr(animated_explainer, "resolve_storyboard_media",
                        lambda _task, beats, **_kwargs: (beats, []))

    output = animated_explainer.render_background(
        "branding-video-render", "स्थानीय परीक्षण", 0.3,
        output_path=str(tmp_path / "branding-render.mp4"),
        branding_segments=[{
            "path": str(branding_path), "start": 0.0, "end": 0.3,
            "asset_key": "intro/v1-" + "a" * 64 + ".mp4", "sha256": "a" * 64,
        }],
    )
    with animated_explainer.VideoFileClip(output, audio=False) as rendered:
        first = rendered.get_frame(0.05)
        later = rendered.get_frame(0.25)
        assert np.mean(np.abs(first[380:800, 120:960].astype(float)
                              - later[380:800, 120:960].astype(float))) > 2
        assert first[400:820, 110:970, 2].mean() > first[400:820, 110:970, 0].mean()
        assert first[1400, 120, 2] > first[1400, 120, 0]
    manifest = json.loads((tmp_path / "top-panel-rendered-media.json").read_text())
    assert manifest["branding_video_segments"][0]["asset_key"].startswith("intro/")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is required for animated video validation")
def test_two_reviewed_cached_pexels_clips_are_composited_with_captions_and_character(
    tmp_path, monkeypatch,
):
    script = "पहला दृश्य सुंदर है। दूसरा दृश्य अलग है।"
    subtitle = tmp_path / "captions.srt"
    subtitle.write_text(
        "1\n00:00:00,100 --> 00:00:01,300\nपहला दृश्य सुंदर है।\n\n"
        "2\n00:00:01,400 --> 00:00:02,600\nदूसरा दृश्य अलग है।\n",
        encoding="utf-8",
    )
    source_clips = []
    candidates = {}
    for query, color, asset_id in (
        ("first landscape", (220, 30, 30), "cached-pexels-red"),
        ("second landscape", (25, 50, 220), "cached-pexels-blue"),
    ):
        path = tmp_path / f"{asset_id}.mp4"
        def moving_frame(t, background=color):
            frame = np.empty((1080, 1920, 3), dtype=np.uint8)
            frame[:] = background
            x = 80 + int((t * 280) % 1450)
            frame[320:760, x:x + 280] = (245, 245, 245)
            return frame

        source = VideoClip(moving_frame, duration=3.5)
        source.write_videofile(str(path), fps=24, codec="libx264", audio=False,
                               preset="ultrafast", logger=None)
        source.close()
        source_clips.append(path)
        candidates[query] = [{
            "provider": "pexels", "asset_id": asset_id,
            "source_page": f"https://www.pexels.com/video/{asset_id}/",
            "license": "Pexels License", "license_url": "https://www.pexels.com/license/",
            "url": f"https://videos.pexels.com/mock/{asset_id}.mp4",
            "path": str(path), "sha256": animated_explainer.task_artifacts.file_digest(path),
            "duration": 3.5, "width": 1920, "height": 1080, "codec": "h264",
            "relevance_score": 1,
        }]

    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(tmp_path))
    monkeypatch.setattr(animated_explainer.task_artifacts, "load_cached_artifact", lambda *_: None)
    monkeypatch.setattr(animated_explainer.task_artifacts, "cache_artifact", lambda *args, **kwargs: None)
    monkeypatch.setattr(animated_explainer, "_cached_stock_candidates",
                        lambda query, _provider: candidates[query])
    monkeypatch.setattr(animated_explainer, "_search_and_cache_stock_candidate",
                        lambda *_args, **_kwargs: pytest.fail("cached Pexels clips should avoid a search"))
    monkeypatch.setattr(animated_explainer.visual_review, "human_approval",
                        lambda asset_id, digest: {
                            "asset_id": asset_id, "sha256": digest, "status": "approved",
                            "reviewer": "offline test", "reason": "Verified relevant local mock clip.",
                        })

    output = animated_explainer.render_background(
        "offline-stock-render", "स्थानीय परीक्षण", 2.8,
        output_path=str(tmp_path / "stock-caption-preview.mp4"),
        subtitle_path=str(subtitle), spoken_script=script,
        narration_duration_seconds=2.8,
        emotion_beats=emotion_storyboard.build(script),
        stock_provider="pexels", stock_terms=["first landscape", "second landscape"],
        caption_mode="sentence", stock_media_policy="manual_review",
    )

    with animated_explainer.VideoFileClip(output, audio=False) as rendered:
        assert tuple(rendered.size) == (1080, 1920)
        first, second = (rendered.get_frame(.6), rendered.get_frame(2.0))
        first_early = rendered.get_frame(.25)
        first_late = rendered.get_frame(.95)
        assert np.mean(np.abs(first_early[430:780, 150:930].astype(float)
                              - first_late[430:780, 150:930].astype(float))) > 2
        first_panel = first[430:780, 150:930].mean(axis=(0, 1))
        second_panel = second[430:780, 150:930].mean(axis=(0, 1))
        assert np.linalg.norm(first_panel - second_panel) > 80
        assert np.count_nonzero((first[850:1030, :, 0] > 200)
                                & (first[850:1030, :, 1] > 170)
                                & (first[850:1030, :, 2] < 70)) > 10
        assert np.count_nonzero(first[1050:1850].max(axis=2) < 50) > 100
    rendered_report = json.loads((tmp_path / "top-panel-rendered-media.json").read_text())
    assert rendered_report["caption_mode"] == "sentence"
    assert [source["asset_id"] for source in rendered_report["sources"]] == [
        "cached-pexels-red", "cached-pexels-blue",
    ]
    assert all(source["provider"] == "pexels" for source in rendered_report["sources"])
