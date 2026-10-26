from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import json

import pytest

from app.models.schema import MaterialInfo, VideoAspect
from app.services import asmr, material, material_cache


PIXABAY_144459 = {
    "id": 144459,
    "tags": "kinetic sand, sand, cutting, asmr",
    "duration": 6,
    "pageURL": "https://pixabay.com/videos/id-144459/",
    "videos": {
        "large": {
            "url": "https://cdn.pixabay.com/video/2020/01/01/144459_large.mp4",
            "width": 4096,
            "height": 2160,
        },
    },
}


def _candidate(asset_id, tags, duration=6):
    return MaterialInfo(
        provider="pixabay",
        url=f"https://cdn.pixabay.com/video/{asset_id}.mp4",
        duration=duration,
        source_info={
            "provider": "pixabay",
            "asset_id": str(asset_id),
            "source_page": f"https://pixabay.com/videos/id-{asset_id}/",
            "tags": tags.split(", "),
            "rendition": {"id": "large", "width": 4096, "height": 2160},
        },
    )


def test_live_pixabay_144459_metadata_and_rendition_are_accepted_for_portrait():
    response = type("Response", (), {
        "status_code": 200,
        "headers": {"content-type": "application/json"},
        "json": lambda _self: {"hits": [
            PIXABAY_144459,
            {**PIXABAY_144459, "id": 123, "tags": "ocean, waves, sand, coast"},
        ]},
    })()
    with patch.object(material, "get_api_key", return_value="test-key"), \
            patch.object(material, "trace_http_call", return_value=response) as request:
        items = material.search_videos_pixabay(
            "kinetic sand cutting", minimum_duration=3, video_aspect=VideoAspect.portrait
        )

    assert [item.source_info["asset_id"] for item in items] == ["144459", "123"]
    candidate = items[0]
    assert candidate.duration == 6
    assert candidate.source_info["tags"] == ["kinetic sand", "sand", "cutting", "asmr"]
    assert asmr.score_asmr_candidate(candidate, "kinetic sand cutting") > 0
    assert asmr.score_asmr_candidate(items[1], "kinetic sand cutting") == 0

    request_url = request.call_args.args[2]
    params = parse_qs(urlsplit(request_url).query)
    assert params["q"] == ["kinetic sand cutting"]
    assert params["per_page"] == ["100"]
    assert params["video_type"] == ["all"]
    assert "key" in params
    assert not {"lang", "order", "orientation", "min_duration"}.intersection(params)


def test_single_relevant_six_second_clip_reports_insufficient_unique_footage(tmp_path):
    candidate = _candidate(144459, "kinetic sand, sand, cutting, asmr")
    with patch.dict(material.config.app, {"material_directory": str(tmp_path)}), \
            patch.object(material, "_search_videos_with_cache", return_value=[candidate]):
        with pytest.raises(material.NoRelevantAsmrMaterialsError, match="Insufficient distinct ASMR footage"):
            material.download_videos(
                task_id="test-pixabay-asmr",
                search_terms=[],
                source="pixabay",
                video_aspect=VideoAspect.portrait,
                audio_duration=30,
                max_clip_duration=3,
                stock_video_style=asmr.SATISFYING_ASMR_CUTTING,
                asmr_categories=["kinetic_sand"],
            )


def test_pixabay_cover_crop_survives_cache_roundtrip_without_repeat_search(tmp_path):
    candidate = _candidate(144459, "kinetic sand, sand, cutting, asmr")
    with patch.object(material_cache, "_cache_dir", return_value=tmp_path), \
            patch.object(material, "search_videos_pixabay", return_value=[candidate]) as remote:
        arguments = dict(provider="pixabay", search_videos=remote,
                         search_term="kinetic sand cutting", minimum_duration=3,
                         video_aspect=VideoAspect.portrait, require_asmr_metadata=True)
        first = material._search_videos_with_cache(**arguments)
        second = material._search_videos_with_cache(**arguments)
    candidate.source_info["quality_review"] = {
        "status": "approved", "reviewer": "test-reviewer",
        "sampled_timestamps_seconds": [0.5, 2.0], "notes": "Clear cutting action.",
    }
    first[0].source_info["quality_review"] = candidate.source_info["quality_review"]
    second[0].source_info["quality_review"] = candidate.source_info["quality_review"]
    assert remote.call_count == 1
    assert second[0].source_info["asset_id"] == "144459"
    ranked = asmr.rank_asmr_candidates([
        ("kinetic sand cutting", first), ("kinetic sand slicing", second)])
    assert len(ranked) == 1
    assert ranked[0][1].source_info["asmr_relevance_score"] > 0


def test_cleanup_removes_only_task_owned_asmr_downloads_and_keeps_metadata(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    clip = task_dir / "pexels-clip.mp4"
    clip.write_bytes(b"stock clip")
    shared = tmp_path / "shared-cache.mp4"
    shared.write_bytes(b"shared clip")
    (task_dir / "script.json").write_text(json.dumps({
        "material_sources": [{
            "provider": "pexels", "local_file": clip.name,
            "license": "Pexels License", "search_term": "kinetic sand cutting",
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(material.utils, "task_dir", lambda _task: str(task_dir))

    removed = material.cleanup_task_stock_materials(
        "test-task", [str(clip), str(shared)]
    )

    assert removed == [clip.name]
    assert not clip.exists()
    assert shared.exists()
    saved = json.loads((task_dir / "script.json").read_text(encoding="utf-8"))
    record = saved["material_sources"][0]
    assert record["license"] == "Pexels License"
    assert record["search_term"] == "kinetic sand cutting"
    assert record["local_file_removed"] is True
