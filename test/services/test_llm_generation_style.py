from app.services import llm


def test_each_visual_style_gets_distinct_generation_guidance():
    styles = (
        "auto",
        "savage_animated_explainer",
        "relevant_stock",
        "asmr_cutting",
    )
    instructions = {
        style: llm.generation_style_instructions(visual_style=style)
        for style in styles
    }

    assert len(set(instructions.values())) == len(styles)
    assert "hybrid explainer" in instructions["savage_animated_explainer"]
    assert "Never direct an insult" in instructions["savage_animated_explainer"]
    assert "Never invent names, studies, statistics, or evidence" in instructions["savage_animated_explainer"]
    assert "licensed stock footage" in instructions["relevant_stock"]
    assert "approved ASMR library" in instructions["asmr_cutting"]


def test_animated_default_is_deshi_savage_and_profanity_setting_changes_prompt():
    animated = llm.build_script_prompt(
        video_subject="ISS gravity",
        visual_style="savage_animated_explainer",
        narrator_preset="desi_savage_aggressive",
    )
    mild = llm.build_script_prompt(
        video_subject="ISS gravity",
        visual_style="savage_animated_explainer",
        narrator_preset="desi_savage_aggressive",
        profanity_level="mild",
    )
    unfiltered = llm.build_script_prompt(
        video_subject="ISS gravity",
        visual_style="savage_animated_explainer",
        narrator_preset="desi_savage_aggressive",
        profanity_level="savage_unfiltered",
    )

    assert "Narration personality: Desi Savage Hindi" in animated
    assert "Do not write formal textbook prose" in animated
    assert "avoid profanity" in mild
    assert "Warn that repeated strong profanity" in unfiltered
    assert "hateful or discriminatory slurs" in unfiltered
    assert "mock a common misconception" in animated
    assert "never insult, belittle, or abuse" in animated
    assert "original stickman" in animated


def test_narration_personality_is_independent_from_visual_style():
    standard_animated = llm.generation_style_instructions(
        visual_style="savage_animated_explainer", narrator_preset="standard"
    )
    savage_stock = llm.generation_style_instructions(
        visual_style="relevant_stock", narrator_preset="desi_savage_aggressive"
    )

    assert "hybrid explainer" in standard_animated
    assert "Narration personality: Standard" in standard_animated
    assert "licensed stock footage" in savage_stock
    assert "Narration personality: Desi Savage Hindi" in savage_stock


def test_script_language_controls_script_not_topic_transliteration():
    hindi = llm.build_script_prompt(
        video_subject="kya tumhe gaon aur city ke bich ka difference pata hai?",
        language="Hindi",
        visual_style="savage_animated_explainer",
    )
    english = llm.build_script_prompt(
        video_subject="kya tumhe gaon aur city ke bich ka difference pata hai?",
        language="English",
        visual_style="savage_animated_explainer",
    )

    assert "write Hindi and Hinglish words in Devanagari" in hindi
    assert 'write "क्या तुम्हें पता है" rather than "kya tumhe pata hai"' in hindi
    assert "For English narration, write in English" in english
