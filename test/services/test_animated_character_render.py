import shutil

import pytest

from app.services import animated_explainer


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
