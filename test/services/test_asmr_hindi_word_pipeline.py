import json
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.channel_profile import ChannelProfile
from app.models import const
from app.models.schema import TaskVideoRequest, VideoAspect, VideoConcatMode, VideoParams
from app.services import asmr_library, channel_profiles, material, subtitle, task
from app.services.state import MemoryState
from app.utils.subtitle_reader import moviepy_subtitles


def test_saved_profile_snapshot_keeps_asmr_and_word_mode(tmp_path):
    store = channel_profiles.ChannelProfileStore(tmp_path / "profiles.sqlite3")
    saved = store.create(ChannelProfile(
        display_name="Hindi ASMR", language="hi", video_source="pixabay",
        stock_video_style="satisfying_asmr_cutting", asmr_categories=["kinetic_sand", "soap"],
        subtitle_display_mode="word_by_word", voice_name="gemini:Puck-Upbeat",
    ))
    preview = channel_profiles.build_preview(store.get(saved.channel_id), {
        "video_subject": "समुद्र", "video_script": "कृष्ण प्रकाश देखता है।",
    })
    snapshot = json.loads(preview.params.model_dump_json())
    params = TaskVideoRequest.model_validate(snapshot)
    assert params.stock_video_style == "satisfying_asmr_cutting"
    assert params.subtitle_display_mode == "word_by_word"
    assert params.video_source == "pixabay"
    assert task.generate_terms("unused", params, params.video_script) == [
        "kinetic sand cutting", "kinetic sand slicing", "soap cutting", "soap shaving",
    ]


def test_asmr_rejects_nonstock_source_before_any_provider_call():
    with patch.object(material, "_download_videos_wavespeed_on_demand") as paid:
        with pytest.raises(material.NoRelevantAsmrMaterialsError, match="Pexels, Pixabay, or"):
            material.download_videos(
                task_id="unused", search_terms=["narration topic"], source="wavespeed",
                stock_video_style="satisfying_asmr_cutting",
            )
    paid.assert_not_called()


def test_asmr_task_refuses_unproven_clip_before_final_render(tmp_path):
    params = VideoParams(
        video_subject="समुद्र", video_script="कृष्ण प्रकाश देखता है।",
        stock_video_style="satisfying_asmr_cutting", video_source="pixabay",
        subtitle_enabled=False, bgm_type="none",
    )
    state = MemoryState()
    (tmp_path / "script.json").write_text(json.dumps({"material_sources": [{
        "local_file": "unrelated.mp4", "provider": "pixabay",
        "search_term": "deep sea specimen",
    }]}), encoding="utf-8")
    with patch.object(task.sm, "state", state), \
            patch.object(task.utils, "task_dir", return_value=str(tmp_path)), \
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True), \
            patch.object(task, "save_script_data"), \
            patch.object(task, "generate_audio", return_value=("audio.mp3", 2.0, object())), \
            patch.object(task, "get_video_materials", return_value=[str(tmp_path / "unrelated.mp4")]), \
            patch.object(task, "generate_final_videos") as render:
        task.start("task", params)
    render.assert_not_called()
    assert state.get_task("task")["failed_stage"] == "materials"
    assert "ASMR footage provenance" in state.get_task("task")["error"]


def test_asmr_task_accepts_verified_material_provenance(tmp_path):
    params = VideoParams(
        video_subject="समुद्र", video_script="कृष्ण प्रकाश देखता है।",
        stock_video_style="satisfying_asmr_cutting", video_source="pixabay",
        subtitle_enabled=False, bgm_type="none",
    )
    state = MemoryState()
    clip = tmp_path / "sand-cutting.mp4"
    (tmp_path / "script.json").write_text(json.dumps({"material_sources": [{
        "local_file": clip.name, "provider": "pixabay",
        "search_term": "kinetic sand cutting", "asmr_relevance_score": 5,
    }]}), encoding="utf-8")
    with patch.object(task.sm, "state", state), \
            patch.object(task.utils, "task_dir", return_value=str(tmp_path)), \
            patch.object(task.utils, "check_ffmpeg_ready", return_value=True), \
            patch.object(task, "save_script_data"), \
            patch.object(task, "generate_audio", return_value=("audio.mp3", 2.0, object())), \
            patch.object(task, "get_video_materials", return_value=[str(clip)]):
        result = task.start("task", params, stop_at="materials")
    assert result == {"materials": [str(clip)]}
    assert state.get_task("task")["state"] == const.TASK_STATE_COMPLETE


def test_hindi_word_cues_retain_marks_and_match_script(tmp_path):
    srt = tmp_path / "subtitle.srt"
    srt.write_text(
        "1\n00:00:00,100 --> 00:00:00,500\nकृष्ण\n\n"
        "2\n00:00:00,500 --> 00:00:01,000\nप्रकाश\n\n"
        "3\n00:00:01,000 --> 00:00:01,400\nदेखता\n\n"
        "4\n00:00:01,400 --> 00:00:01,700\nहै।\n\n",
        encoding="utf-8",
    )
    assert subtitle.validate_word_cues(str(srt), "कृष्ण प्रकाश देखता है।")
    assert [text for _time, text in moviepy_subtitles(str(srt))] == [
        "कृष्ण", "प्रकाश", "देखता", "है।",
    ]
    assert not subtitle.validate_word_cues(str(srt), "कृष्ण प्रकाश बनाता है।")
    srt.write_text("1\n00:00:00,100 --> 00:00:01,700\nकृष्ण प्रकाश देखता है।\n\n", encoding="utf-8")
    assert not subtitle.validate_word_cues(str(srt), "कृष्ण प्रकाश देखता है।")


def test_gemini_edge_word_mode_uses_audio_alignment(tmp_path):
    script = "कृष्ण प्रकाश देखता है।"
    params = TaskVideoRequest(
        video_subject="Hindi alignment fixture", video_script=script,
        subtitle_enabled=True, subtitle_display_mode="word_by_word",
        voice_name="gemini:Puck-Upbeat", video_language="hi", visual_style="asmr_cutting",
    )
    def aligned(*, subtitle_file, **_kwargs):
        Path(subtitle_file).write_text(
            "1\n00:00:00,100 --> 00:00:00,500\nकृष्ण\n\n"
            "2\n00:00:00,500 --> 00:00:01,000\nप्रकाश\n\n"
            "3\n00:00:01,000 --> 00:00:01,400\nदेखता\n\n"
            "4\n00:00:01,400 --> 00:00:01,700\nहै।\n\n", encoding="utf-8",
        )
    with patch.object(task.utils, "task_dir", return_value=str(tmp_path)), \
            patch.dict(task.config.app, {"subtitle_provider": "edge"}), \
            patch.object(task.subtitle, "align_hindi_words", side_effect=lambda _audio, _script, target, **_kwargs:
                         (aligned(subtitle_file=target) or True)) as align, \
            patch.object(task.voice, "create_subtitle") as estimate:
        result = task.generate_subtitle("task", params, script, object(), "audio.mp3")
    assert align.call_count == 1
    assert result == str(tmp_path / "subtitle.srt")
    assert subtitle.validate_word_cues(result, script)
    assert align.call_args.args[:2] == ("audio.mp3", script)
    estimate.assert_not_called()


def test_gemini_edge_rejects_sentence_timeline_in_word_mode(tmp_path):
    params = TaskVideoRequest(
        video_subject="Hindi alignment fixture", video_script="कृष्ण प्रकाश",
        subtitle_enabled=True, subtitle_display_mode="word_by_word",
        voice_name="gemini:Puck-Upbeat", video_language="hi", visual_style="asmr_cutting",
    )
    with patch.object(task.utils, "task_dir", return_value=str(tmp_path)), \
            patch.dict(task.config.app, {"subtitle_provider": "edge"}), \
            patch.object(task.subtitle, "align_hindi_words", return_value=False):
        assert task.generate_subtitle("task", params, "कृष्ण प्रकाश", object(), "audio.mp3") == ""
    assert not (tmp_path / "subtitle.srt").exists()


def test_reviewed_local_clip_counts_nonoverlapping_source_duration(tmp_path):
    library = tmp_path / "asmr_library"
    library.mkdir()
    clip = library / "sand.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
        "-i", "color=c=tan:s=1080x1920:r=1:d=12", "-c:v", "mpeg4",
        "-y", str(clip),
    ], check=True)
    digest = hashlib.sha256(clip.read_bytes()).hexdigest()
    (library / "manifest.json").write_text(json.dumps({"clips": [{
        "file": clip.name, "sha256": digest, "category": "kinetic_sand",
        "license": "self-owned", "reviewed_cutting": True,
        "quality_review": {
            "status": "approved", "reviewer": "test reviewer",
            "sampled_timestamps_seconds": [1, 6, 11],
            "notes": "Fixture clip reviewed for cutting content.",
        },
    }]}), encoding="utf-8")
    with patch.object(asmr_library.utils, "storage_dir", return_value=str(library)), \
            patch.object(material, "_persist_material_sources") as persist:
        result = material.download_videos(
            task_id="local-test", search_terms=["narration topic"], source="local",
            video_aspect=VideoAspect.portrait, audio_duration=10,
            max_clip_duration=5, video_concat_mode=VideoConcatMode.random,
            stock_video_style="satisfying_asmr_cutting", asmr_categories=["kinetic_sand"],
        )
    assert result == [str(clip)]
    assert persist.call_args.args[1][0]["curated_reviewed"] is True
    assert material._asmr_usable_duration(12, 5, VideoConcatMode.random, 1.5) == 8
    assert material._asmr_usable_duration(12, 5, VideoConcatMode.sequential, 1.5) == 5


def test_library_rejects_overlapping_trims_of_one_source(tmp_path):
    library = tmp_path / "asmr_library"
    library.mkdir()
    for filename, seconds in (("first.mp4", 3), ("overlap.mp4", 4)):
        subprocess.run([
            "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
            "-i", f"color=c=tan:s=1080x1920:r=1:d={seconds}",
            "-c:v", "mpeg4", "-y", str(library / filename),
        ], check=True)
    entries = []
    for filename, start, end in (("first.mp4", 0, 3), ("overlap.mp4", 2, 6)):
        entries.append({
            "file": filename, "sha256": hashlib.sha256((library / filename).read_bytes()).hexdigest(),
            "category": "kinetic_sand", "license": "self-owned", "source_id": "one-take",
            "source_start_seconds": start, "source_end_seconds": end,
            "reviewed_cutting": True,
            "quality_review": {
                "status": "approved", "reviewer": "test reviewer",
                "sampled_timestamps_seconds": [0.5, 1.5, 2.5],
                "notes": "Fixture clip reviewed for cutting content.",
            },
        })
    (library / "manifest.json").write_text(json.dumps({"clips": entries}), encoding="utf-8")
    with patch.object(asmr_library.utils, "storage_dir", return_value=str(library)):
        clips = asmr_library.load_clips(["kinetic_sand"], VideoAspect.portrait)
    assert len(clips) == 1
    assert clips[0][1].source_info["source_start_seconds"] == 0


def test_hindi_alignment_writes_exact_devanagari_words_and_rejects_low_score(tmp_path):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    output = tmp_path / "subtitle.srt"
    words = [
        {"word": "कृष्ण", "start": 0.1, "end": 0.5, "score": 0.9},
        {"word": "है।", "start": 0.6, "end": 1.0, "score": 0.9},
    ]

    def write_result(arguments, **_kwargs):
        Path(arguments[-1]).write_text(json.dumps({
            "audio_duration": 1.1, "words": words,
        }, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    with patch.dict(os.environ, {"MPT_HINDI_ALIGNMENT_PYTHON": sys.executable}), \
            patch.object(subtitle.subprocess, "run", side_effect=write_result):
        assert subtitle.align_hindi_words(str(audio), "कृष्ण है।", str(output))
        assert [text for _timing, text in moviepy_subtitles(str(output))] == ["कृष्ण", "है।"]
        words[1]["score"] = 0.1
        audio.write_bytes(b"changed audio, invalidate cached alignment")
        assert not subtitle.align_hindi_words(str(audio), "कृष्ण है।", str(output))


def test_stock_key_uses_root_env_file_without_persisting_secret(tmp_path):
    (tmp_path / ".env").write_text('PEXELS_API_KEY="private-test-key"\n', encoding="utf-8")
    with patch.object(material.config, "root_dir", str(tmp_path)), \
            patch.dict(material.config.app, {"pexels_api_keys": []}), \
            patch.dict(os.environ, {"PEXELS_API_KEY": ""}):
        assert material.get_api_key("pexels_api_keys") == "private-test-key"
        assert material.config.app.get("pexels_api_keys") == []
