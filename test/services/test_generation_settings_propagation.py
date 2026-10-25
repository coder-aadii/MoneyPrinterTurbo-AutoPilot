from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import ast

from app.models.schema import VideoParams
from app.services import emotion_storyboard, generation_settings, llm


def _params(**updates):
    values = {
        "video_subject": "ISS gravity",
        "visual_style": "savage_animated_explainer",
        "narrator_preset": "desi_savage_aggressive",
        "profanity_level": "mild",
        "voice_name": "gemini:Charon-Male",
        "tts_provider": "gemini-tts",
        "tts_model": "gemini-3.8-flash-tts",
    }
    values.update(updates)
    return VideoParams(**values)


def test_resolver_uses_task_then_plan_then_profile_then_global_defaults():
    settings = generation_settings.resolve_generation_settings(
        task_override={"narrator_preset": "standard"},
        plan_override={
            "visual_style": "relevant_stock",
            "profanity_level": "mild",
        },
        channel_profile={
            "visual_style": "asmr_cutting",
            "narrator_preset": "desi_savage_aggressive",
            "profanity_level": "savage_unfiltered",
            "tts_model": "profile-model",
            "voice_name": "gemini:Charon-Male",
            "caption_preset": "word_by_word",
        },
        global_defaults={
            "visual_style": "auto",
            "narrator_preset": "standard",
            "tts_model": "global-model",
        },
    )
    assert settings.visual_style == "relevant_stock"
    assert settings.narrator_preset == "standard"
    assert settings.profanity_level == "mild"
    assert settings.tts_model == "profile-model"
    assert settings.voice_name == "gemini:Charon-Male"
    # A plan-level style switch applies the stock preset's caption defaults;
    # lower-priority ASMR profile settings do not leak into this job.
    assert settings.caption_preset == "sentence"


def test_new_video_params_and_channel_profiles_default_to_savage_style():
    from app.models.channel_profile import ChannelProfile

    assert VideoParams(video_subject="topic").visual_style == "savage_animated_explainer"
    assert ChannelProfile(display_name="New channel").visual_style == "savage_animated_explainer"


def test_auto_resolves_before_script_from_topic_and_pins_concrete_preset(tmp_path, monkeypatch):
    from app.services import task
    monkeypatch.setattr(task.utils, "task_dir", lambda task_id="": str(tmp_path / task_id))
    (tmp_path / "auto-topic").mkdir()
    params = VideoParams(
        video_subject="Village and City comparison", visual_style="auto",
        video_script="",
    )
    settings = generation_settings.resolve_task_settings(params, topic=params.video_subject)
    assert settings.visual_style == "auto"
    assert settings.effective_visual_style == "savage_animated_explainer"
    assert params.effective_visual_style == "savage_animated_explainer"
    assert params.narrator_preset == "desi_savage_aggressive"
    assert params.subtitle_display_mode == "word_by_word"
    assert "AUTO selected Savage" in settings.visual_style_reason

    params.video_script = ""
    with patch.object(llm, "generate_script", return_value="Savage Hindi narration") as generate:
        assert task.generate_script("auto-topic", params) == "Savage Hindi narration"
    assert generate.call_args.kwargs["visual_style"] == "savage_animated_explainer"
    assert generate.call_args.kwargs["narrator_preset"] == "desi_savage_aggressive"


def test_public_channel_preview_preserves_selected_advisory_verification_mode():
    from app.models.channel_profile import ChannelProfile
    from app.services.channel_profiles import build_preview

    profile = ChannelProfile(
        display_name="Scheduled facts", visual_style="savage_animated_explainer",
        publishing_policy="automatic_public", auto_public_enabled=True,
    )
    preview = build_preview(profile, {
        "video_subject": "ISS orbital free fall",
        "video_script": "The ISS and its crew fall around Earth together.",
    })
    assert preview.params.fact_verification_policy == "advisory"


def test_fact_check_mode_is_independent_of_visual_style_and_job_override():
    from app.models.channel_profile import ChannelProfile
    from app.services.channel_profiles import build_preview

    profile = ChannelProfile(
        display_name="Optional checks", visual_style="relevant_stock",
        fact_verification_policy="strict",
    )
    savage = build_preview(profile, {
        "video_subject": "A topic", "visual_style": "savage_animated_explainer",
    })
    disabled = build_preview(profile, {
        "video_subject": "A topic", "visual_style": "savage_animated_explainer",
        "fact_verification_policy": "off",
    })
    assert savage.params.fact_verification_policy == "strict"
    assert disabled.params.fact_verification_policy == "off"


def test_auto_topic_specific_stock_requires_a_configured_source():
    with patch.dict(generation_settings.config.app, {"pexels_api_keys": ["configured"]}):
        settings = generation_settings.resolve_generation_settings(
            task_override={
                "visual_style": "auto", "video_subject": "Butterfly migration",
                "video_source": "pexels",
            },
            topic="Butterfly migration",
        )
    assert settings.effective_visual_style == "relevant_stock"
    assert settings.narrator_preset == "standard"

    with patch.dict(generation_settings.config.app, {"pexels_api_keys": []}):
        unavailable = generation_settings.resolve_generation_settings(
            task_override={"visual_style": "auto", "video_subject": "Butterfly migration",
                           "video_source": "pexels"},
            topic="Butterfly migration",
        )
    assert unavailable.effective_visual_style == "savage_animated_explainer"

    settings = generation_settings.resolve_generation_settings(
        task_override={"video_subject": "topic"},
        global_defaults={"visual_style": "savage_animated_explainer"},
    )
    assert settings.effective_visual_style == "savage_animated_explainer"


def test_visual_defaults_do_not_replace_explicit_personality():
    settings = generation_settings.resolve_generation_settings(
        task_override={
            "visual_style": "savage_animated_explainer",
            "narrator_preset": "standard",
        },
        global_defaults={"narrator_preset": "desi_savage_aggressive"},
    )
    assert settings.narrator_preset == "standard"


def test_audio_identity_tracks_synthesis_inputs_but_not_visual_style():
    script = "ISS पर ग्रैविटी रहती है।"
    base = generation_settings.resolve_task_settings(_params())
    plan = emotion_storyboard.build(
        script, narrator_preset=base.narrator_preset,
        profanity_level=base.profanity_level,
    )
    identity = generation_settings.synthesis_fingerprint(script, base, plan)
    assert generation_settings.synthesis_fingerprint(script, base, plan) == identity

    visual_only = generation_settings.resolve_task_settings(
        _params(visual_style="relevant_stock")
    )
    assert generation_settings.synthesis_fingerprint(script, visual_only, plan) == identity

    for change in (
        {"voice_name": "gemini:Kore-Female"},
        {"tts_model": "gemini-2.5-flash-preview-tts"},
        {"narrator_preset": "standard"},
        {"profanity_level": "savage_unfiltered"},
    ):
        changed = generation_settings.resolve_task_settings(_params(**change))
        changed_plan = emotion_storyboard.build(
            script, narrator_preset=changed.narrator_preset,
            profanity_level=changed.profanity_level,
        )
        assert generation_settings.synthesis_fingerprint(
            script, changed, changed_plan
        ) != identity


def test_standard_and_savage_storyboards_differ_at_semantic_beats():
    script = "ISS पर ग्रैविटी नहीं? अबे भाई, वहाँ करीब नब्बे प्रतिशत ग्रैविटी है।"
    standard = emotion_storyboard.build(script, narrator_preset="standard")
    savage = emotion_storyboard.build(
        script, narrator_preset="desi_savage_aggressive", profanity_level="mild"
    )
    assert [beat["text"] for beat in standard] == [beat["text"] for beat in savage]
    assert all(beat["emotion"] == "neutral explanation" for beat in standard)
    assert savage[0]["emotion"] == "mocking disbelief"
    assert savage[1]["emotion"] == "confident explanation"
    assert [beat["start_word"] for beat in standard] == [beat["start_word"] for beat in savage]
    resolved = emotion_storyboard.resolve(savage, [(i, i + 0.3, word) for i, word in enumerate(script.split())])
    assert all("start" in beat and "end" in beat for beat in resolved)


def test_common_bhai_address_stays_explanatory_and_final_ghost_line_is_payoff():
    beats = emotion_storyboard.build(
        "आईएसएस पर ग्रैविटी नहीं? भाई, वहाँ नब्बे प्रतिशत ग्रैविटी है। "
        "फिजिक्स ने तुम्हें घोस्ट कर दिया!",
        narrator_preset="desi_savage_aggressive",
    )
    assert beats[1]["emotion"] == "confident explanation"
    assert beats[-1]["emotion"] == "comedic payoff"
    assert beats[-1]["pose"] == "celebrating"


def test_task_script_fallback_and_duration_rewrite_keep_resolved_generation_settings(tmp_path, monkeypatch):
    from app.services import task
    monkeypatch.setattr(task.utils, "task_dir", lambda task_id="": str(tmp_path / task_id))
    (tmp_path / "task-id").mkdir()

    params = _params(
        video_script="", min_duration_seconds=25, max_duration_seconds=30,
        video_script_prompt="Use a short spoken format.",
    )
    with patch.object(llm, "generate_script", return_value="हिंदी स्क्रिप्ट") as generate:
        assert task.generate_script("task-id", params) == "हिंदी स्क्रिप्ट"
    assert generate.call_args.kwargs["visual_style"] == "savage_animated_explainer"
    assert generate.call_args.kwargs["narrator_preset"] == "desi_savage_aggressive"
    assert generate.call_args.kwargs["profanity_level"] == "mild"

    with patch.object(llm, "generate_script", return_value="संशोधित हिंदी स्क्रिप्ट") as rewrite:
        revised = task._revise_script_for_duration("पुराना पाठ", params, 12.0)
    assert revised == "संशोधित हिंदी स्क्रिप्ट"
    assert rewrite.call_args.kwargs["visual_style"] == "savage_animated_explainer"
    assert rewrite.call_args.kwargs["narrator_preset"] == "desi_savage_aggressive"
    assert rewrite.call_args.kwargs["profanity_level"] == "mild"


def test_three_script_styles_change_prompt_and_mocked_generated_text():
    cases = [
        ("relevant_stock", "standard", "A calm, factual classroom explanation."),
        ("savage_animated_explainer", "desi_savage_aggressive", "अबे भाई, ISS में ग्रैविटी खत्म नहीं होती!"),
        ("savage_animated_explainer", "standard", "गुरुत्वाकर्षण खत्म नहीं, बस स्टेशन और यात्री साथ-साथ गिर रहे हैं—इसलिए तैरते दिखते हैं!"),
    ]
    prompts = []

    def mocked_provider(prompt, **_kwargs):
        prompts.append(prompt)
        if "Narration personality: Desi Savage Hindi" in prompt:
            return cases[1][2]
        if "licensed stock footage" in prompt:
            return cases[0][2]
        return cases[2][2]

    outputs = []
    with patch.object(llm, "_generate_response", side_effect=mocked_provider):
        for visual_style, narrator_preset, _expected in cases:
            outputs.append(llm.generate_script(
                video_subject="Does gravity disappear on the ISS?",
                language="Hindi",
                visual_style=visual_style,
                narrator_preset=narrator_preset,
                profanity_level="mild",
            ))
    assert len(set(prompts)) == 3
    assert "licensed stock footage" in prompts[0]
    assert "hybrid explainer" in prompts[1]
    assert "Narration personality: Standard" in prompts[2]
    assert outputs == [case[2] for case in cases]
    assert len(set(outputs)) == 3


def test_script_api_and_legacy_video_generator_forward_selected_settings():
    from app.controllers.v1 import llm as llm_controller
    from app.models.schema import VideoScriptRequest

    body = VideoScriptRequest(
        video_subject="ISS gravity",
        visual_style="savage_animated_explainer",
        narrator_preset="standard",
        profanity_level="mild",
    )
    with patch.object(llm, "generate_script", return_value="ISS fact") as generate:
        llm_controller.generate_video_script(SimpleNamespace(), body)
    assert generate.call_args.kwargs["visual_style"] == "savage_animated_explainer"
    assert generate.call_args.kwargs["narrator_preset"] == "standard"
    assert generate.call_args.kwargs["profanity_level"] == "mild"

    source = Path(__file__).resolve().parents[2] / "webui" / "Main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_render_local_script_generation"
    )
    call = next(
        node for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "llm"
        and node.func.attr == "generate_script"
    )
    assert {item.arg for item in call.keywords} >= {
        "visual_style", "narrator_preset", "profanity_level"
    }


def test_voice_preview_cache_reuses_exact_audio_and_ignores_visual_only_changes(tmp_path):
    from app.services import task

    script = "आईएसएस पर ग्रैविटी रहती है।"
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"mock mp3")
    submaker = SimpleNamespace(cues=[])

    def cached_for(params):
        settings = generation_settings.resolve_task_settings(params)
        plan = emotion_storyboard.build(
            script,
            narrator_preset=settings.narrator_preset,
            profanity_level=settings.profanity_level,
        )
        return {
            "audio_file": str(audio), "duration": 2.5, "sub_maker": submaker,
            "script": script, "voice_name": params.voice_name,
            "voice_rate": float(params.voice_rate),
            "voice_volume": float(params.voice_volume),
            "synthesis_fingerprint": generation_settings.synthesis_fingerprint(
                script, settings, plan
            ),
        }

    base = _params()
    cached = cached_for(base)
    with patch.object(task.utils, "task_dir", return_value=str(tmp_path)):
        assert task._resolve_reusable_voice_preview("task-id", base, script, cached)
        with patch.object(task.voice, "tts", side_effect=AssertionError("cache should be reused")), \
                patch.object(task.sm.state, "update_task", return_value=True):
            reused = task.generate_audio("task-id", base, script, cached)
        assert reused[0] == str(audio)
        assert reused[1] == 3
        assert reused[2] is submaker
        visual_only = _params(visual_style="relevant_stock")
        assert task._resolve_reusable_voice_preview(
            "task-id", visual_only, script, cached
        )

        for changes in (
            {"voice_name": "gemini:Kore-Female"},
            {"tts_model": "gemini-2.5-flash-preview-tts"},
            {"narrator_preset": "standard"},
            {"profanity_level": "savage_unfiltered"},
            {"tts_provider": "siliconflow"},
        ):
            changed = _params(**changes)
            assert task._resolve_reusable_voice_preview(
                "task-id", changed, script, cached
            ) is None


def test_narration_regeneration_is_followed_by_new_word_alignment(tmp_path):
    from app.services import task

    params = _params(
        subtitle_display_mode="word_by_word", video_language="hi", subtitle_enabled=True,
    )
    params.custom_audio_file = ""
    spoken = "आईएसएस पर ग्रैविटी रहती है।"
    audio_file = tmp_path / "audio.mp3"
    calls = []
    fake_submaker = SimpleNamespace(tts_compatibility={"status": "supported_native_metadata"})

    def synthesize(**kwargs):
        calls.append(kwargs)
        audio_file.write_bytes(b"mock audio bytes")
        return fake_submaker

    with patch.object(task.utils, "task_dir", return_value=str(tmp_path)), \
            patch.object(task.voice, "tts", side_effect=synthesize), \
            patch.object(task.voice, "get_audio_duration", return_value=3.0), \
            patch.object(task.sm.state, "update_task", return_value=True):
        audio, _duration, submaker = task.generate_audio("task-id", params, "ISS पर ग्रैविटी रहती है।")
        assert audio == str(audio_file)
        assert submaker is fake_submaker
        assert calls[0]["emotion_plan"][0]["text"] == spoken

        aligned = []

        def align(_audio, script, destination, *, policy):
            aligned.append(script)
            assert policy == "balanced"
            Path(destination).write_text(
                "1\n00:00:00,000 --> 00:00:00,800\nISS पर ग्रैविटी रहती है।\n",
                encoding="utf-8",
            )
            return True

        with patch.dict(task.config.app, {"subtitle_provider": "edge"}), \
                patch.object(task.subtitle, "align_hindi_words", side_effect=align), \
                patch.object(task.subtitle, "validate_word_cues", return_value=True):
            task.generate_subtitle("task-id", params, spoken, submaker, audio)
        assert aligned == [spoken]


def test_unsupported_tts_delivery_controls_are_explicit():
    unsupported = generation_settings.tts_compatibility("siliconflow", "model-x")
    legacy = generation_settings.tts_compatibility(
        "gemini-tts", "gemini-2.5-flash-preview-tts"
    )
    assert unsupported["supported"] is False
    assert unsupported["status"] == "unsupported_delivery_controls"
    assert legacy["supported"] is True
    assert legacy["status"] == "supported_prompt_guided_legacy"
    prompt = emotion_storyboard.legacy_gemini_prompt(
        "अबे भाई!", [{"text": "अबे भाई!", "delivery": "frustrated emphasis"}]
    )
    assert "Director's Notes" in prompt
    assert "Speak only the exact text" in prompt

    from app.services import voice
    fake_submaker = SimpleNamespace()
    with patch.object(voice, "_single_tts", return_value=fake_submaker):
        result = voice.tts(
            text="ISS gravity.", voice_name="siliconflow:model:voice-Male",
            voice_rate=1.0, voice_file="unused.mp3", tts_provider="siliconflow",
            emotion_plan=[{"text": "ISS gravity.", "delivery": "neutral"}],
        )
    assert result.tts_compatibility["status"] == "unsupported_delivery_controls"
    assert result.tts_compatibility["supported"] is False
    assert "resolved_model_not_forwarded" in result.tts_compatibility["model_consumption"]


def test_gemini_38_request_contains_resolved_per_beat_delivery_metadata(tmp_path):
    import base64
    from app.services import voice

    class FakeAudio:
        def __len__(self):
            return 1000

        def export(self, destination, format):
            Path(destination).write_bytes(b"mock encoded audio")
            return SimpleNamespace(close=lambda: None)

    response = SimpleNamespace(
        raise_for_status=lambda: None,
        # Raw REST shape: the SDK's ``output_audio`` is a convenience accessor.
        json=lambda: {"steps": [{"type": "model_output", "content": [{
            "type": "audio", "data": base64.b64encode(b"mock wav").decode()
        }]}]},
    )
    requests_seen = []

    def mocked_request(_method, request, *args, **kwargs):
        requests_seen.append(kwargs["json"])
        return response

    script = "अबे भाई, ISS पर ग्रैविटी रहती है।"
    beat = {
        "text": script,
        "delivery": "assertive, sarcastic, clearly explanatory",
    }
    with patch.dict(voice.config.app, {"gemini_api_key": "test-only"}), \
            patch.object(voice, "_configure_pydub_ffmpeg"), \
            patch("pydub.AudioSegment.from_file", return_value=FakeAudio()), \
            patch.object(voice, "trace_http_call", side_effect=mocked_request):
        result = voice.gemini_tts(
            text=script, voice_name="Charon", voice_rate=1.0,
            voice_file=str(tmp_path / "gemini.mp3"), model="gemini-3.8-flash-tts",
            emotion_plan=[beat],
        )
    assert result is not None
    content = requests_seen[0]["input"][0]["content"]
    assert content[0]["text"] == script
    assert content[0]["annotations"] == [{
        "type": "speech_metadata", "style": beat["delivery"]
    }]


def test_legacy_gemini_request_receives_documented_delivery_prompt(tmp_path):
    from app.services import voice
    from google import genai

    class FakeAudio:
        def __len__(self):
            return 1000

        def export(self, destination, format):
            Path(destination).write_bytes(b"mock encoded audio")
            return SimpleNamespace(close=lambda: None)

    requests_seen = []

    class FakeModels:
        def generate_content(self, **kwargs):
            requests_seen.append(kwargs)
            part = SimpleNamespace(inline_data=SimpleNamespace(data=b"mock pcm"))
            content = SimpleNamespace(parts=[part])
            return SimpleNamespace(candidates=[SimpleNamespace(content=content)])

    class FakeClient:
        def __init__(self, **_kwargs):
            self.models = FakeModels()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    script = "अबे भाई, ISS पर ग्रैविटी रहती है।"
    plan = [{"text": script, "delivery": "confident and sarcastic"}]
    with patch.dict(voice.config.app, {"gemini_api_key": "test-only"}), \
            patch.object(voice, "_configure_pydub_ffmpeg"), \
            patch("pydub.AudioSegment.from_file", return_value=FakeAudio()), \
            patch.object(genai, "Client", FakeClient):
        result = voice.gemini_tts(
            text=script, voice_name="Charon", voice_rate=1.0,
            voice_file=str(tmp_path / "legacy-gemini.mp3"),
            model="gemini-2.5-flash-preview-tts", emotion_plan=plan,
        )
    assert result is not None
    assert requests_seen[0]["model"] == "gemini-2.5-flash-preview-tts"
    prompt = requests_seen[0]["contents"]
    assert "Director's Notes" in prompt
    assert "confident and sarcastic" in prompt
    assert f"Transcript:\n{script}" in prompt
