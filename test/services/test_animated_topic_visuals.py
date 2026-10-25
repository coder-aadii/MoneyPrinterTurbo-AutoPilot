from unittest.mock import patch

from app.services import animated_explainer


class RecordingDraw:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record


def test_village_city_comparison_uses_distinct_house_tree_and_skyline_art():
    draw = RecordingDraw()
    with patch.object(animated_explainer, "_font", return_value=object()):
        animated_explainer._draw_topic_scene(
            draw, "unused-font", {"text": "गाँव और शहर का अंतर", "top_visual": "topic_diagram"},
            phase=0.25, topic="Village vs City",
        )

    names = [call[0] for call in draw.calls]
    texts = [call[1][1] for call in draw.calls if call[0] == "text"]
    assert "polygon" in names
    assert names.count("rectangle") >= 10
    assert "गाँव" in texts and "शहर" in texts


def test_client_server_beats_render_distinct_contextual_scenes():
    cases = (
        ("terminology_roast", "Client"),
        ("restaurant_setup", "रेस्टोरेंट"),
        ("restaurant_order", "ग्राहक → ऑर्डर"),
        ("kitchen_server", "किचन तैयार करता है"),
        ("request_response_flow", "Request"),
        ("browser_youtube_exchange", "YouTube\nServer"),
        ("client_server_summary", "Server"),
    )
    with patch.object(animated_explainer, "_font", return_value=object()):
        for visual, expected_text in cases:
            draw = RecordingDraw()
            animated_explainer._draw_topic_scene(
                draw, "unused-font", {"text": "Client और Server", "top_visual": visual},
                phase=0.5, topic="Client vs Server",
            )
            texts = [call[1][1] for call in draw.calls if call[0] == "text"]
            assert expected_text in texts


def test_short_headings_fit_measured_width_for_english_hindi_and_mixed_text():
    canvas = animated_explainer.Image.new("RGB", (1080, 1920))
    draw = animated_explainer.ImageDraw.Draw(canvas)
    samples = (
        "CLOUD vs PLANE",
        "पाँच सौ टन?!",
        "Browser ↔ YouTube सर्वर",
        "500 टन?!",
    )
    for title in samples:
        lines, font, _gap = animated_explainer._fit_heading(draw, title, animated_explainer._font_path())
        assert 1 <= len(lines) <= 2
        assert all(draw.textbbox((0, 0), line, font=font)[2] <= 812 for line in lines)
        assert all((box := draw.textbbox((0, 0), line, font=font))[3] - box[1] <= 112 for line in lines)

    lines, _font, _gap = animated_explainer._fit_heading(
        draw, "pneumonoultramicroscopicsilicovolcanoconiosis" * 3,
        animated_explainer._font_path(), max_width=100,
    )
    assert len(lines) <= 2
    assert "pneumonoultramicroscopicsilicovolcanoconiosis" not in " ".join(lines)


def test_unapproved_stock_uses_fallback_unless_preview_is_explicit(tmp_path, monkeypatch):
    asset = {
        "provider": "pixabay", "asset_id": "sample-1", "source_page": "https://pixabay.com/videos/id-1/",
        "license": "Pixabay Content License", "license_url": "https://pixabay.com/service/license-summary/",
        "url": "https://cdn.pixabay.com/sample.mp4", "path": str(tmp_path / "sample.mp4"),
        "sha256": "abc123", "duration": 10, "width": 1920, "height": 1080,
        "codec": "h264", "tags": ["clouds", "aerial"], "relevance_score": 2,
    }
    monkeypatch.setattr(animated_explainer, "_cached_stock_candidates", lambda *_args: [asset])
    monkeypatch.setattr(animated_explainer.visual_review, "human_approval", lambda *_args: None)
    monkeypatch.setattr(animated_explainer.visual_review, "sample_clip", lambda *_a, **_k: {})
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(tmp_path))
    beats = [{
        "beat_id": "beat_01", "start": 1.0, "end": 4.0,
        "visual_mode": "stock_video", "stock_query": "aerial clouds",
        "top_visual": "stock_clouds", "visual_rationale": "Real clouds fit this beat.",
    }]

    fallback_beats, segments = animated_explainer.resolve_storyboard_media(
        "test-task", [dict(beats[0])], provider="pixabay"
    )
    assert not segments
    assert fallback_beats[0]["effective_media_type"] == "illustration"
    assert fallback_beats[0]["top_visual"] == "cloud_illustration"

    _preview_beats, segments = animated_explainer.resolve_storyboard_media(
        "test-task", [dict(beats[0])], provider="pixabay", allow_unapproved_preview=True
    )
    assert len(segments) == 1
    assert segments[0]["approved"] is False
