import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.models.schema import MaterialInfo
from app.services import animated_explainer, emotion_storyboard, material, scene_spec


def test_visual_timeline_uses_audio_duration_when_only_sentence_cues_exist():
    narration = "Greenland और Iceland के बीच पानी बहता है। नाम Denmark Strait है।"
    beats = emotion_storyboard.build(narration)
    scheduled, word_cues = animated_explainer._visual_timeline(
        beats, [(0.0, 5.0, narration)], narration, 12.0, 2.0,
    )

    assert len(scheduled) == len(beats) == 2
    assert word_cues == []
    assert scheduled[0]["start"] == 2.0
    assert scheduled[-1]["end"] == 14.0
    assert all(item["timing_source"] == "estimated_visual_schedule" for item in scheduled)


def test_visual_timeline_keeps_measured_word_cues_and_branding_offset():
    narration = "एक दो। तीन चार।"
    beats = emotion_storyboard.build(narration)
    scheduled, word_cues = animated_explainer._visual_timeline(
        beats,
        [(0.0, .3, "एक"), (.3, .6, "दो।"), (.6, .9, "तीन"), (.9, 1.2, "चार।")],
        narration, 1.2, 1.0,
    )

    assert scheduled[0]["start"] == 1.0
    assert scheduled[-1]["end"] == 2.2
    assert all(item["timing_source"] == "measured_word_alignment" for item in scheduled)
    assert len(word_cues) == 4


def test_mixed_language_storyboard_limits_stock_queries_without_word_cues(tmp_path, monkeypatch):
    narration = (
        "अबे ये साला समंदर है या साइंस की कोई चूतियागिरी?! "
        "दुनिया का सबसे बड़ा झरना जमीन पर नहीं, समंदर के अंदर छिपा है! "
        "अब सवाल ये है, पानी के अंदर पानी गिर कैसे रहा है? "
        "भाई, असली खेल है तापमान और डेंसिटी का! "
        "ठंडा पानी ज्यादा भारी यानी घना होता है, इसलिए गर्म पानी के नीचे डूबने लगता है। "
        "Greenland और Iceland के बीच यही ठंडा पानी समुद्र की गहरी ढलान से नीचे बहता है! "
        "और ये ढलान करीब साढ़े तीन किलोमीटर की है! "
        "नाम है Denmark Strait Cataract! "
        "ऊपर समंदर बिल्कुल नॉर्मल दिखता है, और नीचे इतना बड़ा कांड चल रहा है! "
        "साला प्रकृति भी गजब की इंजीनियर निकली!"
    )
    terms = [
        "deep ocean", "underwater waterfall", "ocean currents", "temperature gradient",
        "cold water sinking", "Greenland sea", "underwater canyon", "Denmark Strait",
        "ocean surface", "nature landscape",
    ]
    beats = emotion_storyboard.build(narration)
    scheduled, cues = animated_explainer._visual_timeline(beats, [], narration, 50.0, 0.0)
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(tmp_path))
    monkeypatch.setattr(animated_explainer, "_cached_stock_candidates", lambda *_args: [])
    monkeypatch.setattr(
        animated_explainer, "_search_and_cache_stock_candidate",
        lambda *_args, **_kwargs: [],
    )
    resolved, segments = animated_explainer.resolve_storyboard_media(
        "mixed-storyboard", scheduled, provider="pexels", stock_terms=terms,
    )

    assert cues == []
    assert len(resolved) == len(beats) == len(terms) == 10
    assert segments == []
    report = json.loads((tmp_path / "top-panel-storyboard.json").read_text(encoding="utf-8"))
    assert 1 <= len({entry["query"] for entry in report}) <= 3
    assert all(entry["timing_source"] == "estimated_visual_schedule" for entry in report)


class RecordingDraw:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record


def test_comparison_scene_uses_validated_labels_without_a_topic_branch():
    from PIL import Image, ImageChops, ImageDraw
    scene = scene_spec.plan_scene("गाँव बनाम शहर।")
    assert scene.primitive == "comparison"
    assert scene.labels == ["गाँव", "शहर"]
    canvas = Image.new("RGB", (1080, 900), "white")
    animated_explainer._draw_scene_spec(
        ImageDraw.Draw(canvas), animated_explainer._font_path(), scene, .5)
    assert ImageChops.difference(canvas, Image.new("RGB", canvas.size, "white")).getbbox()


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
    monkeypatch.setattr(animated_explainer, "_probe_video", lambda _path: {
        "duration": 10, "width": 1920, "height": 1080, "codec": "h264",
    })
    monkeypatch.setattr(animated_explainer.task_artifacts, "file_digest", lambda _path: "abc123")
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


def test_savage_stock_never_substitutes_selected_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(tmp_path))
    monkeypatch.setattr(animated_explainer, "_cached_stock_candidates", lambda *_args: [])
    searched = []
    pexels_candidate = {
        "provider": "pexels", "asset_id": "pexels-1",
        "source_page": "https://www.pexels.com/video/pexels-1/",
        "license": "Pexels License", "license_url": "https://www.pexels.com/license/",
        "url": "https://cdn.pexels.com/pexels-1.mp4", "path": str(tmp_path / "pexels-1.mp4"),
        "sha256": "a" * 64, "duration": 6, "width": 1920, "height": 1080,
        "codec": "h264", "tags": ["school", "teacher"], "relevance_score": 2,
        "human_approval": {"reviewer": "test", "reason": "Relevant school footage approved."},
        "prepared_path": str(tmp_path / "prepared-pexels-1.mp4"),
    }

    def search(_query, provider, _task, **_kwargs):
        searched.append(provider)
        return [pexels_candidate] if provider == "pexels" else []

    monkeypatch.setattr(animated_explainer, "_search_and_cache_stock_candidate", search)
    beats, segments = animated_explainer.resolve_storyboard_media(
        "test-task", [{
            "beat_id": "beat_01", "start": 0.0, "end": 4.0,
            "visual_mode": "illustration", "top_visual": "topic_diagram",
        }], provider="pixabay", stock_terms=["school teacher"],
    )

    assert searched == ["pixabay"]
    assert segments == []
    assert beats[0]["effective_media_type"] == "illustration"


def test_explicit_stock_sources_are_exclusive_and_auto_uses_configured_order(monkeypatch):
    monkeypatch.setitem(material.config.app, "stock_video_provider_order",
                        ["coverr", "pexels", "pixabay"])
    monkeypatch.setattr(material, "stock_video_provider_configured", lambda provider: provider != "pixabay")
    assert material.stock_video_provider_order("pexels") == ["pexels"]
    assert material.stock_video_provider_order("pixabay") == ["pixabay"]
    assert material.stock_video_provider_order("coverr") == ["coverr"]
    assert material.stock_video_provider_order("auto") == ["coverr", "pexels"]


def test_transformation_storyboard_uses_few_stock_queries_and_generic_primitive(tmp_path, monkeypatch):
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(tmp_path))
    monkeypatch.setattr(animated_explainer, "_cached_stock_candidates", lambda *_args: [])
    searched = []
    monkeypatch.setattr(animated_explainer, "_search_and_cache_stock_candidate",
                        lambda query, provider, _task, **_kwargs: searched.append((query, provider)) or [])
    narration = ("अबे ये साली जेलीफ़िश है या ज़िंदगी का रीस्टार्ट बटन?! "
                 "इंसान बूढ़ा होता है। नाम है इम्मॉर्टल जेलीफ़िश! "
                 "जब इसे चोट लगती है तो ये वापस अपनी शुरुआती अवस्था में पहुंच सकती है! "
                 "और फिर उसी अवस्था से नई जेलीफ़िश विकसित हो सकती हैं! "
                 "लेकिन रुक भाई, इसे मछली खा जाए तो गेम ओवर!")
    beats = emotion_storyboard.schedule_visuals_from_duration(
        emotion_storyboard.build(narration), 20)
    resolved, segments = animated_explainer.resolve_storyboard_media(
        "jellyfish", beats, provider="pexels",
        stock_terms=["irrelevant"] * len(beats),
    )
    assert not segments
    assert all(provider == "pexels" for _, provider in searched)
    assert len(set(query for query, _ in searched)) <= 3
    assert sum(beat["scene_spec"]["primitive"] == "transformation" for beat in resolved) >= 2
    assert all(beat["top_visual"] == "scene_spec_v1" for beat in resolved)
    assert all(beat["scene_spec"]["illustrative"] for beat in resolved)


def test_sentence_captions_preserve_phrase_timing_and_render_inside_panel():
    from PIL import Image, ImageChops
    script = "जेलीफ़िश फिर शुरू होती है।"
    groups = animated_explainer._sentence_groups(
        [(0.2, 1.4, script)], script, 1.6, 2.0)
    assert groups == [{"start": 2.2, "end": 3.4, "sentence": script}]
    beat = {**emotion_storyboard.build("नाम है इम्मॉर्टल जेलीफ़िश!")[0],
            "start": 2.0, "end": 3.6}
    with_caption = Image.fromarray(animated_explainer._draw_frame(
        "जेलीफ़िश", 4.0, 2.5, groups, [beat]))
    without_caption = Image.fromarray(animated_explainer._draw_frame(
        "जेलीफ़िश", 4.0, 2.5, [], [beat]))
    assert ImageChops.difference(
        with_caption.crop((60, 850, 1020, 1050)),
        without_caption.crop((60, 850, 1020, 1050)),
    ).getbbox() is not None


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


def test_saved_keyword_can_select_stock_for_transformation_beat(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    attempts = _patch_real_stock_selection(
        monkeypatch, tmp_path,
        {"cloud formation": _stock_candidates("cloud formation", ["cloud-clip"])},
    )
    beat = {
        "beat_id": "beat_01", "start": 0.0, "end": 4.0,
        "scene_spec": scene_spec.plan_scene(
            "Cloud formation then rain", index=0, total=1,
        ).model_dump(),
        "visual_mode": "illustration", "stock_query": None,
        "short_visual_title": "Cloud formation", "visual_rationale": "Saved keyword match.",
    }

    resolved, segments = animated_explainer.resolve_storyboard_media(
        "test-task", [beat], provider="pexels", stock_terms=["cloud formation"],
        stock_media_policy="manual_review",
    )

    assert attempts == ["https://cdn.pexels.com/cloud-clip.mp4"]
    assert resolved[0]["scene_spec"]["stock_query"] == "cloud formation"
    assert resolved[0]["effective_media_type"] == "stock_video"
    assert len(segments) == 1
    assert segments[0]["provider"] == "pexels"
    assert segments[0]["approved"] is True


def test_auto_stock_eligibility_uses_positive_search_match_and_checks_format(monkeypatch):
    candidate = {
        "provider": "pexels", "asset_id": "clip", "source_page": "https://pexels.com/video/clip/",
        "license": "Pexels License", "path": "/tmp/clip.mp4", "sha256": "digest",
        "relevance_score": 1,
    }
    monkeypatch.setattr(animated_explainer, "_probe_video", lambda _path: {
        "duration": 4, "width": 1920, "height": 1080, "codec": "h264",
    })
    monkeypatch.setattr(animated_explainer.task_artifacts, "file_digest", lambda _path: "digest")
    monkeypatch.setattr(animated_explainer.visual_review, "human_approval", lambda *_args: None)
    eligible, reason, evidence = animated_explainer._stock_eligibility(
        candidate, required_seconds=3, policy="auto_eligible",
    )
    assert (eligible, reason) == (True, "pexels_search_match")
    assert evidence["verifier"] == "provider_search"

    monkeypatch.setattr(animated_explainer, "_probe_video", lambda _path: {
        "duration": 4, "width": 1080, "height": 1920, "codec": "h264",
    })
    eligible, reason, _approval = animated_explainer._stock_eligibility(
        candidate, required_seconds=3, policy="manual_review",
    )
    assert (eligible, reason) == (False, "not_sufficient_resolution_landscape")


def test_automatic_stock_does_not_require_gemini_or_human_approval(monkeypatch):
    digest = "a" * 64
    candidate = {
        "provider": "pexels", "asset_id": "clip", "source_page": "https://pexels.com/video/clip/",
        "license": "Pexels License", "path": "/tmp/clip.mp4", "sha256": digest,
        "relevance_score": 1,
    }
    monkeypatch.setattr(animated_explainer, "_probe_video", lambda _path: {
        "duration": 4, "width": 1920, "height": 1080, "codec": "h264",
    })
    monkeypatch.setattr(animated_explainer.task_artifacts, "file_digest", lambda _path: digest)
    monkeypatch.setattr(animated_explainer.visual_review, "human_approval",
                        lambda *_args: pytest.fail("automatic mode must not use human approval"))

    eligible, reason, evidence = animated_explainer._stock_eligibility(
        candidate, required_seconds=3, policy="auto_eligible", query="jellyfish drifting",
    )
    assert eligible is True
    assert reason == "pexels_search_match"
    assert evidence["score"] == 1


def test_automatic_candidate_download_path_never_checks_human_approval(tmp_path, monkeypatch):
    item = MaterialInfo(
        provider="pexels", url="https://videos.pexels.com/automatic.mp4", duration=8,
        source_info={
            "provider": "pexels", "asset_id": "auto-clip",
            "source_page": "https://www.pexels.com/video/auto-clip/",
            "license": "Pexels License", "title": "jellyfish underwater",
            "tags": ["jellyfish"],
        },
    )
    monkeypatch.setattr(animated_explainer.utils, "storage_dir",
                        lambda name="": str(tmp_path / name))
    monkeypatch.setattr(animated_explainer.material, "stock_video_provider_configured",
                        lambda _provider: True)
    monkeypatch.setattr(animated_explainer.material, "_search_videos_with_cache",
                        lambda **_kwargs: [item])
    monkeypatch.setattr(animated_explainer.material, "_save_material_item",
                        lambda *_args: str(tmp_path / "automatic.mp4"))
    monkeypatch.setattr(animated_explainer, "_probe_video", lambda _path: {
        "duration": 8, "width": 1920, "height": 1080, "codec": "h264",
    })
    monkeypatch.setattr(animated_explainer.task_artifacts, "file_digest",
                        lambda _path: "c" * 64)
    monkeypatch.setattr(animated_explainer.visual_review, "human_approval",
                        lambda *_args: pytest.fail("automatic search path must not call human approval"))

    found = animated_explainer._search_and_cache_stock_candidate(
        "jellyfish", "pexels", "auto-clip-test", stock_media_policy="auto_eligible",
    )

    assert len(found) == 1
    assert "human_approval" not in found[0]


def test_automatic_search_match_does_not_sample_for_human_review(monkeypatch):
    item = MaterialInfo(
        provider="pexels", url="https://videos.pexels.com/uncertain.mp4", duration=8,
        source_info={
            "provider": "pexels", "asset_id": "uncertain",
            "source_page": "https://www.pexels.com/video/uncertain/",
            "license": "Pexels License", "title": "jellyfish",
        },
    )
    monkeypatch.setattr(animated_explainer.material, "stock_video_provider_configured",
                        lambda _provider: True)
    monkeypatch.setattr(animated_explainer.material, "_search_videos_with_cache",
                        lambda **_kwargs: [item])
    monkeypatch.setattr(animated_explainer, "_probe_video", lambda _path: {
        "duration": 8, "width": 1920, "height": 1080, "codec": "h264",
    })
    monkeypatch.setattr(animated_explainer.task_artifacts, "file_digest", lambda _path: "digest")
    downloaded = []
    monkeypatch.setattr(animated_explainer.material, "_save_material_item",
                        lambda *_args: downloaded.append(True) or "/tmp/pexels.mp4")
    monkeypatch.setattr(animated_explainer.visual_review, "human_approval",
                        lambda *_args: pytest.fail("automatic mode must not call human approval"))
    monkeypatch.setattr(animated_explainer.visual_review, "sample_clip",
                        lambda *_args, **_kwargs: pytest.fail("automatic mode must not sample for review"))

    found = animated_explainer._search_and_cache_stock_candidate(
        "jellyfish", "pexels", "auto-no-vision", stock_media_policy="auto_eligible",
    )
    assert len(found) == 1
    assert "human_approval" not in found[0]
    assert downloaded == [True]
    assert found[0]["relevance_score"] == 1


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
    save_directories = []
    monkeypatch.setattr(animated_explainer.utils, "storage_dir",
                        lambda name="": str(tmp_path / "persistent-storage" / name))
    monkeypatch.setattr(animated_explainer.material, "get_api_key", lambda _name: "test")
    monkeypatch.setattr(animated_explainer.material, "_search_videos_with_cache", lambda **_kwargs: candidates)
    monkeypatch.setattr(
        animated_explainer.material, "_save_material_item",
        lambda item, directory: (
            saved.append(item), save_directories.append(directory),
            str(tmp_path / f"{item.source_info['asset_id']}.mp4"),
        )[-1],
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
        stock_media_policy="manual_review",
    )
    assert len(saved) == 3
    assert len(results) == 3
    assert save_directories == [str(tmp_path / "persistent-storage" / "cache_videos")] * 3


def test_automatic_search_stops_after_required_validated_and_prepared_clips(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    candidates = [MaterialInfo(
        provider="pexels", url=f"https://cdn.pexels.com/auto-{i}.mp4", duration=8,
        source_info={
            "provider": "pexels", "asset_id": f"auto-{i}",
            "license": "Pexels License", "tags": ["bird", "singing"],
        },
    ) for i in range(5)]
    downloads, preparations = [], []
    monkeypatch.setattr(animated_explainer.material, "stock_video_provider_configured", lambda _p: True)
    monkeypatch.setattr(animated_explainer.material, "_search_videos_with_cache", lambda **_kw: candidates)
    monkeypatch.setattr(animated_explainer.material, "_save_material_item",
                        lambda item, _directory: downloads.append(item) or str(tmp_path / f"{item.source_info['asset_id']}.mp4"))
    monkeypatch.setattr(animated_explainer, "_probe_video", lambda _path: {
        "duration": 8, "width": 1920, "height": 1080, "codec": "h264",
    })
    monkeypatch.setattr(animated_explainer.task_artifacts, "file_digest",
                        lambda path: Path(path).name)
    monkeypatch.setattr(animated_explainer, "_prepare_stock_candidate",
                        lambda candidate, _task, _seconds: preparations.append(candidate["asset_id"]) or candidate["path"])

    found = animated_explainer._search_and_cache_stock_candidate(
        "bird singing", "pexels", "auto-budget", required_count=2,
        clip_seconds=4, stock_media_policy="auto_eligible",
    )

    assert len(found) == 2
    assert len(downloads) == len(preparations) == 2


def test_portrait_search_rendition_is_rejected_before_download(monkeypatch):
    item = MaterialInfo(
        provider="pexels", url="https://videos.pexels.com/portrait.mp4", duration=8,
        source_info={
            "provider": "pexels", "asset_id": "portrait", "license": "Pexels License",
            "source_page": "https://www.pexels.com/video/portrait/",
            "title": "jellyfish", "rendition": {"width": 1080, "height": 1920},
        },
    )
    monkeypatch.setattr(animated_explainer.material, "stock_video_provider_configured",
                        lambda _provider: True)
    monkeypatch.setattr(animated_explainer.material, "_search_videos_with_cache",
                        lambda **_kwargs: [item])
    downloads = []
    monkeypatch.setattr(animated_explainer.material, "_save_material_item",
                        lambda *args: downloads.append(args) or "/tmp/should-not-download.mp4")

    assert animated_explainer._search_and_cache_stock_candidate(
        "jellyfish", "pexels", "portrait-check",
    ) == []
    assert downloads == []


def test_pexels_provider_ranked_result_survives_sparse_metadata(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    candidate = MaterialInfo(
        provider="pexels", url="https://cdn.pexels.com/clip.mp4", duration=8,
        source_info={"provider": "pexels", "asset_id": "clip-1", "license": "Pexels License"},
    )
    monkeypatch.setattr(animated_explainer.material, "stock_video_provider_configured", lambda _provider: True)
    monkeypatch.setattr(animated_explainer.material, "_search_videos_with_cache", lambda **_kwargs: [candidate])
    monkeypatch.setattr(animated_explainer.material, "_save_material_item", lambda *_args: str(tmp_path / "clip.mp4"))
    monkeypatch.setattr(animated_explainer, "_probe_video", lambda _path: {
        "duration": 8, "width": 1920, "height": 1080, "codec": "h264",
    })
    monkeypatch.setattr(animated_explainer.task_artifacts, "file_digest", lambda _path: "digest")
    monkeypatch.setattr(animated_explainer.visual_review, "human_approval", lambda *_args: None)
    monkeypatch.setattr(animated_explainer.visual_review, "sample_clip", lambda *_a, **_k: {})

    results = animated_explainer._search_and_cache_stock_candidate(
        "underwater waterfall", "pexels", "test-task",
    )

    assert len(results) == 1
    assert results[0]["relevance_score"] == 1


def test_unconfigured_stock_fallback_is_skipped_without_provider_search(monkeypatch):
    searched = []
    monkeypatch.setattr(
        animated_explainer.material, "_search_videos_with_cache",
        lambda **kwargs: searched.append(kwargs) or [],
    )
    monkeypatch.setattr(animated_explainer.material, "stock_video_provider_configured", lambda _provider: False)

    assert animated_explainer._search_and_cache_stock_candidate(
        "underwater waterfall", "pixabay", "test-task",
    ) == []
    assert not searched


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
        stock_media_policy="manual_review",
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
        stock_media_policy="manual_review",
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

    video_dir = tmp_path / "video-cache"
    video_dir.mkdir()
    url = "https://cdn.pexels.com/cached-eggs.mp4"
    cached_video = video_dir / f"vid-{hashlib.md5(url.encode()).hexdigest()}.mp4"
    cached_video.write_bytes(b"offline fixture")
    unrelated_url = "https://cdn.pexels.com/legs.mp4"
    unrelated_video = video_dir / f"vid-{hashlib.md5(unrelated_url.encode()).hexdigest()}.mp4"
    unrelated_video.write_bytes(b"offline fixture")
    cache_items = [
        MaterialInfo(provider="pexels", url=url, duration=8, source_info={
            "provider": "pexels", "asset_id": "cached-eggs", "search_term": "eggs",
            "source_page": "https://pexels.com/video/eggs/", "license": "Pexels License",
        }),
        MaterialInfo(provider="pexels", url=unrelated_url, duration=8, source_info={
            "provider": "pexels", "asset_id": "legs", "search_term": "eggs",
            "source_page": "https://pexels.com/video/legs/", "license": "Pexels License",
        }),
    ]
    monkeypatch.setattr(
        animated_explainer.utils, "storage_dir",
        lambda name="": str(video_dir),
    )
    monkeypatch.setattr(animated_explainer.material.material_cache,
                        "load_material_search_cache",
                        lambda **kwargs: cache_items if kwargs["search_term"] == "eggs" else None)
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    monkeypatch.setattr(animated_explainer, "_probe_video", lambda _path: {
        "duration": 8, "width": 1920, "height": 1080, "codec": "h264",
    })
    monkeypatch.setattr(animated_explainer.task_artifacts, "file_digest", lambda _path: "cached-digest")
    monkeypatch.setattr(animated_explainer.visual_review, "human_approval",
                        lambda asset_id, *_args: (
                            {"reviewer": "offline-test", "reason": "Approved visible egg footage."}
                            if asset_id == "cached-eggs" else None
                        ))
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

    assert [item["asset_id"] for item in cached] == ["cached-eggs", "legs"]
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
    shared_cache = tmp_path / "shared-cache"
    shared_cache.mkdir()
    cached_source = shared_cache / "vid-shared.mp4"
    cached_source.write_bytes(b"persistent video cache")
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))
    monkeypatch.setattr(animated_explainer.utils, "storage_dir",
                        lambda _name="": str(shared_cache))

    removed = animated_explainer.cleanup_stock_media("test-task")

    assert removed == [str(stock_file)]
    assert not media_dir.exists()
    saved = json.loads((task_dir / "top-panel-storyboard.json").read_text(encoding="utf-8"))
    assert saved[0]["asset_id"] == "pexels-1"
    assert saved[0]["license"] == "Pexels License"
    assert saved[0]["local_path"] is None
    assert saved[0]["local_media_removed"] is True
    assert cached_source.is_file()


def test_success_cleanup_preserves_shared_stock_source_path(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    media_dir = task_dir / "top-panel-media"
    media_dir.mkdir(parents=True)
    (media_dir / "prepared-panel.mp4").write_bytes(b"prepared")
    shared = tmp_path / "cache_videos" / "shared-source.mp4"
    shared.parent.mkdir()
    shared.write_bytes(b"shared source")
    report_path = task_dir / "top-panel-storyboard.json"
    report_path.write_text(json.dumps([{"asset_id": "pexels-1", "local_path": str(shared)}]))
    monkeypatch.setattr(animated_explainer.utils, "task_dir", lambda _task: str(task_dir))

    animated_explainer.cleanup_stock_media("task")

    saved = json.loads(report_path.read_text())
    assert saved[0]["local_path"] == str(shared)
    assert saved[0]["shared_cache_retained"] is True
    assert shared.is_file()
