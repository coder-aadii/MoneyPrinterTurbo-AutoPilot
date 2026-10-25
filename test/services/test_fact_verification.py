import hashlib
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from app.models.schema import VideoParams
from app.services import task
from app.services.state import MemoryState
from app.services.autopilot import AutopilotError, AutopilotService


def _report(script, state):
    return {
        "state": state,
        "script_hash": hashlib.sha256(script.encode("utf-8")).hexdigest(),
        "claims": ([{
            "claim_id": "supported-claim", "claim": script,
            "verified_evidence_ids": ["nasa", "esa"],
            "evidence": [
                {"evidence_id": "nasa", "title": "NASA", "url": "https://nasa.gov/a", "excerpt": "NASA source body."},
                {"evidence_id": "esa", "title": "ESA", "url": "https://esa.int/a", "excerpt": "ESA source body."},
            ],
        }] if state == "source_verified" else []),
    }


def test_strict_unsupported_claim_fails_before_tts():
    script = "A bird predicts tomorrow's weather."
    params = VideoParams(
        video_subject="Bird weather", video_script=script, video_source="local",
        visual_style="relevant_stock", fact_verification_policy="strict",
    )
    state = MemoryState()
    with patch.object(task.utils, "check_ffmpeg_ready", return_value=True), \
         patch.object(task.sm, "state", state), \
         patch.object(task.source_research, "verify_script", return_value=_report(script, "unsupported")), \
         patch.object(task, "generate_audio") as synthesize:
        task._run_pipeline("strict-unsupported", params, stop_at="audio")

    synthesize.assert_not_called()
    assert state.get_task("strict-unsupported")["failed_stage"] == "fact_verification"


def test_advisory_savage_preview_can_continue_with_visible_unverified_state():
    script = "A bird predicts tomorrow's weather."
    params = VideoParams(
        video_subject="Bird weather", video_script=script, video_source="local",
        visual_style="savage_animated_explainer", fact_verification_policy="advisory",
    )
    with patch.object(task.source_research, "verify_script", return_value=_report(script, "unsupported")) as verify, \
         patch.object(task.sm.state, "update_task"):
        assert task._verify_script_for_policy("advisory", params, script) == script
    verify.assert_called_once_with(script, timeout_seconds=8.0)
    assert params.fact_verification_state == "unsupported"
    assert params.fact_verification_script_hash == hashlib.sha256(script.encode()).hexdigest()


def test_advisory_unavailable_ddgs_never_blocks_savage_tts():
    script = "A supported science fact for an advisory preview."
    params = VideoParams(
        video_subject="Science fact", video_script=script, video_source="local",
        visual_style="savage_animated_explainer", fact_verification_policy="advisory",
    )
    state = MemoryState()
    with patch.object(task.utils, "check_ffmpeg_ready", return_value=True), \
         patch.object(task.sm, "state", state), \
         patch.object(task.source_research, "_research_provider_settings",
                      return_value=("ddgs", ["ddgs"], "")), \
         patch.object(task.source_research, "research_health", return_value={"ok": True}), \
         patch.object(task.source_research, "_search_provider",
                      side_effect=RuntimeError("DDGS unavailable")), \
         patch.object(task.voice, "get_audio_duration", return_value=28), \
         patch.object(task, "save_script_data"), \
         patch.object(task, "generate_audio", return_value=("audio.mp3", 28, object())) as synthesize:
        result = task._run_pipeline("advisory-ddgs-down", params, stop_at="audio")

    synthesize.assert_called_once()
    assert result["audio_file"] == "audio.mp3"
    assert result["audio_duration"] == 28
    snapshot = state.get_task("advisory-ddgs-down")
    assert snapshot["fact_verification_policy"] == "advisory"
    assert snapshot["fact_verification_state"] == "unavailable"
    assert snapshot["fact_verification_status"] == "unavailable"
    assert snapshot["fact_verification_report"]["claims"][0]["status"] == "unavailable"


def test_off_mode_skips_research_even_for_savage_style():
    script = "An unchecked narration."
    params = VideoParams(
        video_subject="Topic", video_script=script, visual_style="savage_animated_explainer",
        fact_verification_policy="off",
    )
    with patch.object(task.source_research, "verify_script") as verify, \
         patch.object(task.sm.state, "update_task"):
        assert task._verify_script_for_policy("off-mode", params, script) == script
    verify.assert_not_called()
    assert params.fact_verification_state == "unverified"
    assert params.fact_verification_report["status"] == "unverified"


def test_verification_hash_binds_numeric_normalized_spoken_narration():
    source_script = "हर 15 घरों में 3 लाइटें हैं।"
    spoken_script = "हर पंद्रह घरों में तीन लाइटें हैं।"
    params = VideoParams(
        video_subject="Village lights", visual_style="savage_animated_explainer",
        video_language="hi",
    )
    with patch.object(task.source_research, "verify_script", return_value=_report(spoken_script, "unsupported")) as verify, \
         patch.object(task.sm.state, "update_task"):
        assert task._verify_script_for_policy("numeric-hash", params, source_script) == source_script

    verify.assert_called_once_with(spoken_script, timeout_seconds=8.0)
    assert params.fact_verification_script_hash == hashlib.sha256(spoken_script.encode()).hexdigest()


def test_publication_refuses_advisory_render_even_when_other_qa_passes():
    job = SimpleNamespace(
        job_id="public-job", state="generated", profile_snapshot={
            "publishing_policy": "automatic_public", "auto_public_enabled": True,
        }, mpt_task_id="public-task", script="A narration.",
        params_snapshot={"video_language": "hi"},
    )
    connection = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchone=lambda: {
        "state": "approved", "reviewed_by": "automatic-policy-after-qa",
    }))
    service = object.__new__(AutopilotService)
    service.store = SimpleNamespace(get_job=lambda _: job, _connection=lambda: nullcontext(connection))
    with patch.object(service, "qa_report", return_value={"passed": True}), \
         patch.object(task.sm.state, "get_task", return_value={
             "fact_verification_state": "unsupported",
             "fact_verification_script_hash": hashlib.sha256(b"A narration.").hexdigest(),
         }), \
         patch.object(service, "_public_requirements_pass", return_value=True), \
         patch.object(service, "_job_runnable", return_value=True):
        try:
            service.publish_job("public-job")
        except AutopilotError as exc:
            assert "explicit channel opt-in" in str(exc)
        else:
            raise AssertionError("advisory narration must never reach public publishing")


def test_opted_in_unverified_publication_is_labeled_in_metadata():
    job = SimpleNamespace(
        job_id="public-label", state="generated", profile_snapshot={
            "publishing_policy": "automatic_public", "allow_unverified_publication": True,
        }, params_snapshot={"fact_verification_policy": "off", "video_language": "hi"},
        mpt_task_id="public-label-task", script="A factual narration.", topic="A science topic",
    )
    service = object.__new__(AutopilotService)
    with patch.object(task.sm.state, "get_task", return_value={
        "fact_verification_state": "unverified",
    }):
        title, description = service._publication_metadata(job)
    assert title.startswith("[UNVERIFIED FACTS]")
    assert "did not verify the factual claims" in description


def test_duration_rewrite_reverifies_exact_new_script_before_second_tts():
    old = "A short supported statement."
    revised = "A longer supported statement with more detail."
    params = VideoParams(
        video_subject="Supported topic", video_script=old, video_source="local",
        visual_style="relevant_stock", fact_verification_policy="strict",
        min_duration_seconds=25, max_duration_seconds=30,
        duration_enforcement="strict",
        duration_recovery_attempts=1, duration_recovery_approved=True,
    )
    state = MemoryState()
    def verify(script):
        return _report(script, "source_verified")
    with patch.object(task.utils, "check_ffmpeg_ready", return_value=True), \
         patch.object(task.sm, "state", state), \
         patch.object(task.source_research, "verify_script", side_effect=verify) as research, \
         patch.object(task, "generate_audio", return_value=("audio.mp3", 20, object())) as synthesize, \
         patch.object(task.voice, "get_audio_duration", side_effect=[18, 27]), \
         patch.object(task.llm, "generate_script", return_value=revised), \
         patch.object(task, "generate_terms", return_value=["supported topic"]), \
         patch.object(task, "save_script_data"):
        task._run_pipeline("strict-duration", params, stop_at="audio")

    assert [call.args[0] for call in research.call_args_list] == [old, revised]
    assert synthesize.call_count == 2
    assert params.fact_verification_script_hash == hashlib.sha256(revised.encode()).hexdigest()
    assert params.fact_verification_state == "source_verified"
