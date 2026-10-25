from app.services import emotion_storyboard


def test_savage_semantic_beats_keep_viewer_as_ally_and_choose_concrete_visuals():
    script = (
        "ये Client-Server नाम सुनकर लगता है टेक्निकल चक्कर है। "
        "एक रेस्टोरेंट सोचो: ग्राहक खाना ऑर्डर करता है। "
        "किचन खाना तैयार करता है। "
        "Client Request भेजता है और Server Response देता है। "
        "Browser YouTube Server से वीडियो माँगता है। "
        "Client माँगता है, Server जवाब देता है।"
    )
    beats = emotion_storyboard.build(
        script, narrator_preset="desi_savage_aggressive"
    )

    assert all(beat["humor_target"] == "jargon_or_misconception" for beat in beats)
    assert beats[0]["emotion"] == "mocking disbelief"
    assert beats[0]["top_visual"] == "terminology_roast"
    assert beats[1]["visual_mode"] == "illustration"
    assert beats[1]["top_visual"] == "restaurant_setup"
    assert beats[1]["stock_query"] is None
    assert beats[2]["stock_query"] == "chef preparing a meal in a restaurant kitchen"
    assert beats[2]["top_visual"] == "kitchen_server"
    assert beats[3]["visual_mode"] == "animated_diagram"
    assert beats[3]["top_visual"] == "request_response_flow"
    assert beats[4]["visual_mode"] == "animated_diagram"
    assert beats[4]["top_visual"] == "browser_youtube_exchange"
    assert beats[4]["caption_emphasis"] == ["Server", "Browser", "YouTube"]
    assert beats[-1]["top_visual"] == "client_server_summary"


def test_savage_profanity_delivery_targets_the_idea_not_the_viewer():
    beat = emotion_storyboard.build(
        "ये साला jargon कितना उलझा हुआ है!",
        narrator_preset="desi_savage_aggressive",
    )[0]
    assert "never at the viewer" in beat["delivery"]
    assert beat["humor_target"] == "jargon_or_misconception"


def test_cloud_fact_storyboard_splits_reveal_droplets_weight_and_payoff():
    script = (
        "क्या तुम्हें पता है कि मध्यम आकार के बादल का वजन एक हवाई जहाज़ से भी ज़्यादा हो सकता है? "
        "हाँ, यह सच है! बादल में पानी की बूंदों और बर्फ के कणों का भार इतना होता है कि एक औसत बादल "
        "लगभग पाँच सौ टन तक भारी हो सकता है, जो बोइंग सात सौ सैंतीस से ज़्यादा है। "
        "तो बादल को देखकर सोचना कि वह आसमान में तैरता एक विशाल पानी का गुब्बारा है, जो मेहनत कर रहा है।"
    )
    beats = emotion_storyboard.build(script, narrator_preset="desi_savage_aggressive")
    assert [beat["short_visual_title"] for beat in beats] == [
        "बादल vs प्लेन", "सच में?", "छोटी बूंदें", "500 टन?!", "बादल ओवरटाइम?"
    ]
    assert [beat["visual_mode"] for beat in beats] == [
        "stock_video", "animated_diagram", "animated_diagram", "comparison", "illustration"
    ]
    assert beats[0]["stock_query"] == "aerial moving cloudscape above the clouds"
    assert [(beat["start_word"], beat["end_word"]) for beat in beats] == [
        (0, 20), (20, 24), (24, 42), (42, 59), (59, 80)
    ]
