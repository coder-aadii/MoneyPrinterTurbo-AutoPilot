import json
from unittest.mock import patch

import pytest

from app.models.schema import MaterialInfo
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
    monkeypatch.setattr(animated_explainer, "_search_and_cache_stock_candidate", lambda *_args, **_kwargs: [])
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


def test_savage_stock_search_uses_editable_keyword(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    beat = {
        "beat_id": "beat_01", "start": 1.0, "end": 4.0,
        "visual_mode": "stock_video", "stock_query": "old internal query",
        "top_visual": "stock_clouds", "visual_rationale": "Relevant footage.",
    }
    monkeypatch.setattr(animated_explainer, "_cached_stock_candidates", lambda *_args: [])
    searched = []
    monkeypatch.setattr(
        animated_explainer, "_search_and_cache_stock_candidate",
        lambda query, provider, _task, **_kwargs: searched.append((query, provider)) or [],
    )
    resolved, segments = animated_explainer.resolve_storyboard_media(
        "test-task", [beat], provider="pexels", stock_terms=["Siberian tit"],
    )
    assert searched == [("Siberian tit", "pexels")]
    assert resolved[0]["stock_query"] == "Siberian tit"
    assert not segments


def test_savage_applies_keywords_to_multiple_story_beats(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    searched = []
    monkeypatch.setattr(animated_explainer, "_cached_stock_candidates", lambda *_args: [])
    monkeypatch.setattr(
        animated_explainer, "_search_and_cache_stock_candidate",
        lambda query, _provider, _task, **_kwargs: searched.append(query) or [],
    )
    beats = [
        {"beat_id": f"beat_{index}", "start": index, "end": index + 1,
         "visual_mode": "illustration", "stock_query": None,
         "top_visual": "topic_diagram"}
        for index in range(4)
    ]

    resolved, _segments = animated_explainer.resolve_storyboard_media(
        "test-task", beats, provider="pexels",
        stock_terms=["Siberian tit", "Siberian tit", "bird singing", "rain clouds"],
    )

    assert searched == ["Siberian tit", "bird singing", "rain clouds"]
    assert [item["stock_query"] for item in resolved if item["visual_mode"] == "stock_video"] == [
        "Siberian tit", "Siberian tit", "bird singing", "rain clouds",
    ]
    assert all(item["visual_mode"] == "stock_video" for item in resolved)


def test_stock_candidate_search_downloads_only_the_required_clip_count(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    candidates = [
        MaterialInfo(
            provider="pexels", url=f"https://cdn.pexels.com/{index}.mp4", duration=8 + index,
            source_info={
                "provider": "pexels", "asset_id": str(index), "license": "Pexels License",
                "tags": ["bird", "singing", f"tag-{index}"],
            },
        )
        for index in range(5)
    ]
    saved = []
    monkeypatch.setattr(animated_explainer.material, "get_api_key", lambda _name: "test")
    monkeypatch.setattr(animated_explainer.material, "_search_videos_with_cache", lambda **_kwargs: candidates)
    monkeypatch.setattr(
        animated_explainer.material, "_save_material_item",
        lambda item, _directory: saved.append(item) or str(tmp_path / f"{item.source_info['asset_id']}.mp4"),
    )
    monkeypatch.setattr(
        animated_explainer, "_probe_video",
        lambda _path: {"duration": 10, "width": 1920, "height": 1080, "codec": "h264"},
    )
    monkeypatch.setattr(animated_explainer.task_artifacts, "file_digest", lambda path: str(path).split("/")[-1])
    monkeypatch.setattr(
        animated_explainer.visual_review, "human_approval",
        lambda *_args: {"reviewer": "test", "reason": "Approved"},
    )
    results = animated_explainer._search_and_cache_stock_candidate(
        "bird singing", "pexels", "test-task", required_count=3,
    )
    assert len(saved) == 3
    assert len(results) == 3


def test_stock_download_failure_tries_a_replacement_for_the_same_keyword(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    candidates = [
        MaterialInfo(
            provider="pexels", url=f"https://cdn.pexels.com/{index}.mp4", duration=8,
            source_info={
                "provider": "pexels", "asset_id": str(index), "license": "Pexels License",
                "tags": ["bird", "singing"],
            },
        )
        for index in range(3)
    ]
    attempted = []
    monkeypatch.setattr(animated_explainer.material, "get_api_key", lambda _name: "test")
    monkeypatch.setattr(animated_explainer.material, "_search_videos_with_cache", lambda **_kwargs: candidates)
    monkeypatch.setattr(
        animated_explainer.material, "_save_material_item",
        lambda item, _directory: attempted.append(item.url) or (
            "" if len(attempted) == 1 else str(tmp_path / "replacement.mp4")
        ),
    )
    monkeypatch.setattr(
        animated_explainer, "_probe_video",
        lambda _path: {"duration": 8, "width": 1920, "height": 1080, "codec": "h264"},
    )
    monkeypatch.setattr(animated_explainer.task_artifacts, "file_digest", lambda path: str(path).split("/")[-1])
    monkeypatch.setattr(
        animated_explainer.visual_review, "human_approval",
        lambda *_args: {"reviewer": "test", "reason": "Approved"},
    )

    results = animated_explainer._search_and_cache_stock_candidate(
        "bird singing", "pexels", "test-task", required_count=1,
    )

    assert attempted == [candidates[0].url, candidates[1].url]
    assert len(results) == 1


def test_unapproved_candidate_is_replaced_by_same_keyword_candidate(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    candidates = [
        MaterialInfo(
            provider="pexels", url=f"https://cdn.pexels.com/{index}.mp4", duration=8,
            source_info={
                "provider": "pexels", "asset_id": str(index), "license": "Pexels License",
                "tags": ["bird", "singing"],
            },
        )
        for index in range(3)
    ]
    saved = []
    monkeypatch.setattr(animated_explainer.material, "get_api_key", lambda _name: "test")
    monkeypatch.setattr(animated_explainer.material, "_search_videos_with_cache", lambda **_kwargs: candidates)
    monkeypatch.setattr(
        animated_explainer.material, "_save_material_item",
        lambda item, _directory: saved.append(item) or str(tmp_path / f"{item.source_info['asset_id']}.mp4"),
    )
    monkeypatch.setattr(
        animated_explainer, "_probe_video",
        lambda _path: {"duration": 8, "width": 1920, "height": 1080, "codec": "h264"},
    )
    monkeypatch.setattr(animated_explainer.task_artifacts, "file_digest", lambda path: str(path).split("/")[-1])
    approvals = iter([None, {"reviewer": "test", "reason": "Approved"}])
    monkeypatch.setattr(
        animated_explainer.visual_review, "human_approval",
        lambda *_args: next(approvals),
    )
    monkeypatch.setattr(animated_explainer.visual_review, "sample_clip", lambda *_a, **_k: {})

    results = animated_explainer._search_and_cache_stock_candidate(
        "bird singing", "pexels", "test-task", required_count=1,
    )

    assert len(saved) == 2
    assert len(results) == 2
    assert results[0]["human_approval"] is None
    assert results[1]["human_approval"]["reviewer"] == "test"


def _stock_candidates(query, asset_ids):
    return [
        MaterialInfo(
            provider="pexels", url=f"https://cdn.pexels.com/{asset_id}.mp4", duration=8,
            source_info={
                "provider": "pexels", "asset_id": asset_id,
                "source_page": f"https://pexels.com/video/{asset_id}/",
                "license": "Pexels License", "license_url": "https://pexels.com/license",
                "title": query, "description": query,
            },
        )
        for asset_id in asset_ids
    ]


def _patch_real_stock_selection(monkeypatch, tmp_path, results_by_query, *, fail_urls=()):
    attempted = []
    failed_urls = set(fail_urls)
    cache_root = tmp_path / "isolated-material-cache"
    (cache_root / "cache_material_search").mkdir(parents=True, exist_ok=True)
    (cache_root / "cache_videos").mkdir(parents=True, exist_ok=True)
    local_task_dir = tmp_path / "isolated-task"
    local_task_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(
        animated_explainer.utils, "storage_dir",
        lambda name="": str(cache_root / name),
    )
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(local_task_dir))
    monkeypatch.setattr(animated_explainer.material, "get_api_key", lambda _name: "test-key")
    monkeypatch.setattr(
        animated_explainer.material, "_search_videos_with_cache",
        lambda **kwargs: results_by_query.get(kwargs["search_term"], []),
    )

    def save(item, _directory):
        attempted.append(item.url)
        return "" if item.url in failed_urls else str(tmp_path / f"{item.source_info['asset_id']}.mp4")

    monkeypatch.setattr(animated_explainer.material, "_save_material_item", save)
    monkeypatch.setattr(animated_explainer, "_probe_video", lambda _path: {
        "duration": 8, "width": 1920, "height": 1080, "codec": "h264",
    })
    monkeypatch.setattr(animated_explainer.task_artifacts, "file_digest", lambda path: str(path).split("/")[-1])
    monkeypatch.setattr(
        animated_explainer.visual_review, "human_approval",
        lambda asset_id, digest: {"reviewer": "offline-test", "reason": f"approved {asset_id}/{digest}"},
    )
    monkeypatch.setattr(
        animated_explainer, "_prepare_stock_candidate",
        lambda candidate, _task, _seconds: candidate["path"],
    )
    return attempted


def test_fifteen_second_topic_uses_three_distinct_five_second_clips(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    attempted = _patch_real_stock_selection(
        monkeypatch, tmp_path, {"eggs": _stock_candidates("eggs", ["egg-1", "egg-2", "egg-3"])},
    )
    beats = [{
        "beat_id": "eggs", "start": 0.0, "end": 15.0,
        "visual_mode": "illustration", "top_visual": "topic_diagram",
    }]

    _resolved, segments = animated_explainer.resolve_storyboard_media(
        "test-task", beats, provider="pexels", stock_terms=["eggs"],
    )

    assert len(attempted) == 3
    assert len(segments) == 3
    assert all(segment["end"] - segment["start"] <= 5 for segment in segments)
    assert len({segment["asset_id"] for segment in segments}) == 3


def test_topic_keyword_count_and_clip_count_follow_mixed_narration_duration(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    topics = ["eggs", "pan", "spices", "stove", "plate", "serve"]
    durations = [15, 5, 5, 5, 5, 5]
    attempted = _patch_real_stock_selection(
        monkeypatch, tmp_path,
        {topic: _stock_candidates(topic, [f"{topic}-{index}" for index in range(3)])
         for topic in topics},
    )
    beats = []
    cursor = 0.0
    for topic, duration in zip(topics, durations):
        beats.append({
            "beat_id": topic, "start": cursor, "end": cursor + duration,
            "visual_mode": "illustration", "top_visual": "topic_diagram",
        })
        cursor += duration

    _resolved, segments = animated_explainer.resolve_storyboard_media(
        "test-task", beats, provider="pexels", stock_terms=topics,
    )

    assert len(segments) == 8
    assert len(attempted) == 8
    assert all(segment["end"] - segment["start"] <= 5 for segment in segments)


def test_forty_second_same_topic_scales_to_eight_short_clips(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    attempted = _patch_real_stock_selection(
        monkeypatch, tmp_path,
        {"weather": _stock_candidates("weather", [f"weather-{index}" for index in range(8)])},
    )
    beats = [{
        "beat_id": "weather", "start": 0.0, "end": 40.0,
        "visual_mode": "illustration", "top_visual": "topic_diagram",
    }]

    _resolved, segments = animated_explainer.resolve_storyboard_media(
        "test-task", beats, provider="pexels", stock_terms=["weather"],
    )

    assert len(segments) == 8
    assert len(attempted) == 8
    assert all(segment["end"] - segment["start"] <= 5 for segment in segments)


def test_cross_keyword_duplicate_is_excluded_before_counting_and_replaced(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    shared = _stock_candidates("rain bird", ["shared"])[0]
    replacement = _stock_candidates("rain bird", ["replacement"])[0]
    attempted = _patch_real_stock_selection(
        monkeypatch, tmp_path,
        {"bird": [shared], "rain bird": [shared, replacement]},
    )
    beats = [
        {"beat_id": "first", "start": 0.0, "end": 5.0, "top_visual": "topic_diagram"},
        {"beat_id": "second", "start": 5.0, "end": 10.0, "top_visual": "topic_diagram"},
    ]

    _resolved, segments = animated_explainer.resolve_storyboard_media(
        "test-task", beats, provider="pexels", stock_terms=["bird", "rain bird"],
    )

    assert [segment["asset_id"] for segment in segments] == ["shared", "replacement"]
    assert attempted == [shared.url, replacement.url]


def test_failed_selected_download_tries_only_same_keyword_replacement(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    candidates = _stock_candidates("eggs", ["bad-download", "good-replacement", "extra"])
    attempted = _patch_real_stock_selection(
        monkeypatch, tmp_path, {"eggs": candidates}, fail_urls=[candidates[0].url],
    )
    beat = {"beat_id": "eggs", "start": 0.0, "end": 5.0, "top_visual": "topic_diagram"}

    _resolved, segments = animated_explainer.resolve_storyboard_media(
        "test-task", [beat], provider="pexels", stock_terms=["eggs"],
    )

    assert attempted == [candidates[0].url, candidates[1].url]
    assert [segment["asset_id"] for segment in segments] == ["good-replacement"]


def test_short_stock_keyword_list_is_rejected_not_spread_across_beats(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    beats = [
        {"beat_id": "first", "start": 0.0, "end": 5.0},
        {"beat_id": "second", "start": 5.0, "end": 10.0},
    ]

    with pytest.raises(ValueError, match="one-to-one"):
        animated_explainer.resolve_storyboard_media(
            "test-task", beats, provider="pexels", stock_terms=["eggs"],
        )


def test_single_word_cached_keyword_reuses_relevant_asset_and_ignores_unrelated(tmp_path, monkeypatch):
    import hashlib

    cache_dir = tmp_path / "search-cache"
    video_dir = tmp_path / "video-cache"
    cache_dir.mkdir()
    video_dir.mkdir()
    url = "https://cdn.pexels.com/cached-eggs.mp4"
    cached_video = video_dir / f"vid-{hashlib.md5(url.encode()).hexdigest()}.mp4"
    cached_video.write_bytes(b"offline fixture")
    (cache_dir / "results.json").write_text(json.dumps({"items": [
        {"url": url, "duration": 8, "source_info": {
            "provider": "pexels", "asset_id": "cached-eggs", "title": "eggs cooking",
            "description": "fresh eggs in a pan", "source_page": "https://pexels.com/video/eggs/",
            "license": "Pexels License", "tags": [],
        }},
        {"url": "https://cdn.pexels.com/legs.mp4", "duration": 8, "source_info": {
            "provider": "pexels", "asset_id": "legs", "title": "legs running",
            "description": "runner training", "source_page": "https://pexels.com/video/legs/",
            "license": "Pexels License", "tags": [],
        }},
    ]}), encoding="utf-8")
    monkeypatch.setattr(
        animated_explainer.utils, "storage_dir",
        lambda name="": str(cache_dir if name == "cache_material_search" else video_dir),
    )
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    monkeypatch.setattr(animated_explainer, "_probe_video", lambda _path: {
        "duration": 8, "width": 1920, "height": 1080, "codec": "h264",
    })
    monkeypatch.setattr(animated_explainer.task_artifacts, "file_digest", lambda _path: "cached-digest")
    monkeypatch.setattr(
        animated_explainer.visual_review, "human_approval",
        lambda *_args: {"reviewer": "offline-test", "reason": "approved"},
    )
    monkeypatch.setattr(animated_explainer, "_prepare_stock_candidate", lambda *_args: str(cached_video))
    searched = []
    monkeypatch.setattr(
        animated_explainer, "_search_and_cache_stock_candidate",
        lambda *args, **kwargs: searched.append((args, kwargs)) or [],
    )

    cached = animated_explainer._cached_stock_candidates("eggs", "pexels")
    beat = {"beat_id": "eggs", "start": 0.0, "end": 5.0, "top_visual": "topic_diagram"}
    _resolved, segments = animated_explainer.resolve_storyboard_media(
        "test-task", [beat], provider="pexels", stock_terms=["eggs"],
    )

    assert [item["asset_id"] for item in cached] == ["cached-eggs"]
    assert not searched
    assert [segment["asset_id"] for segment in segments] == ["cached-eggs"]


def test_pexels_candidate_without_tags_uses_title_and_description(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    candidate = _stock_candidates("eggs cooking", ["no-tags"])[0]
    attempted = _patch_real_stock_selection(monkeypatch, tmp_path, {"eggs": [candidate]})
    beat = {"beat_id": "eggs", "start": 0.0, "end": 5.0, "top_visual": "topic_diagram"}

    _resolved, segments = animated_explainer.resolve_storyboard_media(
        "test-task", [beat], provider="pexels", stock_terms=["eggs"],
    )

    assert attempted == [candidate.url]
    assert [segment["asset_id"] for segment in segments] == ["no-tags"]


def test_success_cleanup_removes_task_stock_files_but_keeps_source_metadata(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    media_dir = task_dir / "top-panel-media"
    media_dir.mkdir(parents=True)
    stock_file = media_dir / "source.mp4"
    stock_file.write_bytes(b"video")
    report = [{"asset_id": "pexels-1", "local_path": str(stock_file), "license": "Pexels License"}]
    (task_dir / "top-panel-storyboard.json").write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))

    removed = animated_explainer.cleanup_stock_media("test-task")

    assert removed == [str(stock_file)]
    assert not media_dir.exists()
    saved = json.loads((task_dir / "top-panel-storyboard.json").read_text(encoding="utf-8"))
    assert saved[0]["asset_id"] == "pexels-1"
    assert saved[0]["license"] == "Pexels License"
    assert saved[0]["local_path"] is None
    assert saved[0]["local_media_removed"] is True
