from unittest.mock import patch

from app.models.schema import VideoParams
from app.services import llm, script_quality, topic_quality


def test_savage_quality_gate_detects_generic_opening_abuse_and_flat_ending():
    script = "क्या तुम्हें पता है बादल भारी होते हैं और पानी की बूंदें होती हैं, यह जानना जरूरी है।"
    assert script_quality.savage_script_issues(script) == [
        "generic curiosity-question hook", "missing a specific, memorable hook",
        "no discernible topic-specific payoff ending",
    ]
    assert "viewer-directed insult or abuse" in script_quality.savage_script_issues(
        "अबे गधे, तुम्हें ये पता नहीं?"
    )
    assert script_quality.savage_script_issues(
        "बादल का वजन देखो—आसमान का ये रूई जैसा गुब्बारा 500 टन का निकला!"
    ) == []


def test_savage_quality_gate_allows_only_one_bounded_rewrite():
    responses = [
        "क्या तुम्हें पता है बादल का वजन बहुत ज्यादा होता है।",
        "रूई जैसा दिखने वाला बादल तराजू पर 500 टन निकले, अब ये आसमान में ओवरटाइम कर रहा है!",
    ]
    prompts = []

    def response(prompt, **kwargs):
        prompts.append(prompt)
        return responses.pop(0)

    with patch.object(llm, "_generate_response", side_effect=response):
        result = llm.generate_script(
            video_subject="Cloud weight",
            language="Hindi",
            visual_style="savage_animated_explainer",
            narrator_preset="desi_savage_aggressive",
            profanity_level="mild",
            savage_quality_policy="strict",
        )
    assert len(prompts) == 2
    assert "Bounded Savage Style Rewrite" in prompts[1]
    assert result.startswith("रूई जैसा दिखने वाला बादल")


def test_valid_topic_does_not_require_sarcasm_hook_or_profanity():
    topic = "ऑक्टोपस के तीन दिल खून कैसे पंप करते हैं?"
    assert topic_quality.topic_issues(topic) == []
    assert "topic lacks a concrete relationship, question, comparison, quantity, or mechanism" in topic_quality.topic_issues(
        "A fact about the Moon"
    )


def test_topic_generator_uses_topic_stage_validator_and_final_payload():
    prompts = []
    with patch.object(llm, "_generate_response", side_effect=lambda prompt, **kwargs: prompts.append(prompt) or "ऑक्टोपस के तीन दिल खून कैसे पंप करते हैं?"):
        result = llm.generate_topic(
            niche="प्रकृति",
            preferences=["समुद्री जीव"],
            language="Hindi",
            style_guidance="hybrid animated explainer",
        )
    assert result == "ऑक्टोपस के तीन दिल खून कैसे पंप करते हैं?"
    assert len(prompts) == 1
    assert "do not add a hook, joke, sarcasm, profanity, or punchline" in prompts[0].casefold()
    assert "TOPIC_DATA_JSON" in prompts[0]


def test_nonsensical_topic_is_rejected_after_bounded_topic_rewrite():
    responses = ["aa aa aa aa aa", "aa aa aa aa aa"]
    with patch.object(llm, "_generate_response", side_effect=lambda *args, **kwargs: responses.pop(0)):
        result = llm.generate_topic(niche="science", preferences=["animals"], language="Hindi")
    assert result.startswith("Error: topic rejected after one rewrite")


def test_advisory_keeps_coherent_script_with_imperfect_humour_without_rewrite():
    draft = "बादल पानी की बहुत छोटी बूंदों से बनते हैं और हवा में तैरते रहते हैं।"
    prompts = []
    with patch.object(llm, "_generate_response", side_effect=lambda prompt, **kwargs: prompts.append(prompt) or draft):
        result = llm.generate_script(
            video_subject="बादल कैसे बनते हैं",
            language="Hindi",
            visual_style="savage_animated_explainer",
            savage_quality_policy="advisory",
        )
    assert result == draft
    assert not result.startswith("Error: ")
    assert "missing a specific, memorable hook" in script_quality.savage_script_warnings(result)
    assert len(prompts) == 1


def test_strict_quality_policy_rewrites_once_then_returns_actual_draft_and_reasons():
    revised = "बादल पानी की छोटी बूंदों से बनते हैं और हवा में तैरते रहते हैं।"
    responses = [
        "क्या तुम्हें पता है बादल पानी की बूंदों से बनते हैं।",
        revised,
    ]
    prompts = []

    def response(prompt, **kwargs):
        prompts.append(prompt)
        return responses.pop(0)

    with patch.object(llm, "_generate_response", side_effect=response):
        result = llm.generate_script(
            video_subject="desert beetle temperature adaptation",
            language="Hindi",
            visual_style="savage_animated_explainer",
            narrator_preset="desi_savage_aggressive",
            savage_quality_policy="strict",
        )
    parsed = script_quality.parse_failed_draft(result)
    assert parsed is not None
    assert parsed[0] == revised
    assert "missing a specific, memorable hook" in "; ".join(parsed[1])
    assert len(prompts) == 2


def test_natural_concept_directed_savage_passes_but_viewer_insult_does_not():
    good = "बादल रूई जैसे दिखते हैं, मगर तराजू पर पाँच सौ टन निकलें—अब ये आसमान का ओवरटाइम कर्मचारी है!"
    assert script_quality.savage_script_issues(good) == []
    assert "viewer-directed insult or abuse" in script_quality.savage_script_issues(
        "अबे गधे, बादल भारी होते हैं!"
    )


def test_creative_warnings_are_separate_from_hard_validity_and_fact_policy():
    draft = "क्या तुम्हें पता है बादल पानी की छोटी बूंदों से बनते हैं।"
    assert script_quality.savage_script_hard_errors(draft) == []
    assert script_quality.savage_script_warnings(draft)
    assert script_quality.savage_script_hard_errors("बहुत बहुत बहुत बहुत")

    params = VideoParams(video_subject="clouds", fact_verification_policy="strict")
    assert params.fact_verification_policy == "strict"
    assert params.savage_quality_policy == "advisory"


def test_final_script_payload_keeps_style_and_pronunciation_after_long_user_prompt():
    captured = []
    response = "बादल रूई जैसे दिखते हैं, मगर तराजू पर पाँच सौ टन निकलें—अब ये आसमान का ओवरटाइम कर्मचारी है!"
    with patch.object(llm, "_generate_response", side_effect=lambda prompt, **kwargs: captured.append(prompt) or response):
        result = llm.generate_script(
            video_subject="बादल का वजन",
            language="Hindi",
            video_script_prompt="x" * 2000,
            visual_style="savage_animated_explainer",
            narrator_preset="desi_savage_aggressive",
        )
    assert result == response
    assert len(captured) == 1
    assert "# Speech and Pronunciation Requirements" in captured[0]
    assert 'never guess or expand ambiguous profanity such as "bc"' in captured[0]
    assert "# Visual and Narration Direction" in captured[0]


def test_oversized_prompt_fails_explicitly_without_silent_truncation_or_provider_call():
    with patch.object(llm, "_generate_response") as provider:
        result = llm.generate_script(video_subject="topic", video_script_prompt="x" * 2001)
    assert result.startswith("Error: video_script_prompt exceeds 2000 characters")
    provider.assert_not_called()
