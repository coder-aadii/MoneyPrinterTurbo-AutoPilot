from unittest.mock import patch

from app.models.schema import VideoParams
from app.services import script_quality, task


def test_successful_llm_script_draft_is_cached_per_task_and_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(task.utils, "task_dir", lambda task_id="": str(tmp_path / task_id))
    (tmp_path / "cache-task").mkdir()
    params = VideoParams(
        video_subject="cloud weight", video_language="Hindi",
        visual_style="savage_animated_explainer", narrator_preset="desi_savage_aggressive",
    )
    draft = "बादल रूई जैसे दिखते हैं, मगर तराजू पर पाँच सौ टन निकलें—अब ये आसमान का ओवरटाइम कर्मचारी है!"
    with patch.object(task.llm, "generate_script", return_value=draft) as generate:
        assert task.generate_script("cache-task", params) == draft
        assert task.generate_script("cache-task", params) == draft
    generate.assert_called_once()


def test_rejected_savage_draft_fails_task_and_never_calls_tts(tmp_path, monkeypatch):
    monkeypatch.setattr(task.utils, "task_dir", lambda task_id="": str(tmp_path / task_id))
    (tmp_path / "rejected-task").mkdir()
    params = VideoParams(
        video_subject="desert beetle heat adaptation", video_language="Hindi",
        video_script_prompt="", visual_style="savage_animated_explainer",
        narrator_preset="desi_savage_aggressive", video_source="local",
    )
    rejected = script_quality.failed_draft_result(
        "रेगिस्तान में कार खड़ी करते समय एक कीड़ा चिल्ला रहा था।",
        ["unsupported biological mechanism phrasing"],
    )
    with patch.object(task.sm.state, "update_task"), \
            patch.object(task, "_mark_task_failed", return_value={"state": "failed"}) as failed, \
            patch.object(task.llm, "generate_script", return_value=rejected), \
            patch.object(task, "generate_audio") as generate_audio:
        result = task._run_pipeline("rejected-task", params, stop_at="video")
    assert result == {"state": "failed"}
    assert failed.call_args.args[1] == "script"
    assert "script_quality_issues" in failed.call_args.kwargs["details"]
    assert "rejected_script_draft" in failed.call_args.kwargs["details"]
    generate_audio.assert_not_called()
    assert (tmp_path / "rejected-task" / "script.json").is_file()
