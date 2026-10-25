"""Deterministic narration beats shared by expressive TTS and animated scenes."""

from __future__ import annotations

import re


_WORD = re.compile(r"\S+")
_SENTENCE = re.compile(r"[^।?!\n]+[।?!]*|\n+")


def _classify(
    sentence: str,
    index: int,
    total: int,
    narrator_preset: str = "standard",
) -> dict:
    text = sentence.casefold()
    gravity_context = any(
        term in text
        for term in (
            "iss", "आईएसएस", "gravity", "ग्रैविटी", "गुरुत्व", "कक्षा",
            "धरती", "नब्बे प्रतिशत", "90%",
        )
    )
    if not gravity_context:
        is_hook = index == 0 and any(mark in sentence for mark in ("?", "क्या", "क्यों"))
        is_jargon_hook = index == 0 and any(term in text for term in (
            "client", "क्लाइंट", "server", "सर्वर", "तकनीकी नाम", "जर्गन",
        ))
        is_payoff = index == total - 1
        is_correction = any(
            term in text for term in ("गलत", "नहीं", "मिथक", "कौन बोला")
        )
        is_reveal = any(
            term in text for term in (
                "असल", "दरअसल", "यानी", "लेकिन", "पता चला", "सच है",
                "पाँच सौ टन", "पांच सौ टन", "500 टन", "बोइंग",
            )
        )
        if narrator_preset != "desi_savage_aggressive":
            return {
                "emotion": "neutral explanation", "intensity": 0.32,
                "delivery": "clear, measured explanation; natural pace and easy to understand",
                "pose": "explaining", "gesture": "point to the relevant diagram",
                "visual": "topic_hook" if is_hook else "topic_payoff" if is_payoff else "topic_reveal" if is_reveal else "topic_diagram",
                "sound_effect": None,
            }
        if is_hook or is_jargon_hook:
            return {
                "emotion": "mocking disbelief", "intensity": 0.78,
                "delivery": "assertive, sarcastic question; punchy, not shouted",
                "pose": "skeptical", "gesture": "head tilt then palm-up challenge",
                "visual": "topic_hook", "sound_effect": "pop",
            }
        if is_payoff:
            return {
                "emotion": "comedic payoff", "intensity": 0.82,
                "delivery": "playful, confident punchline; lift the final words without shouting",
                "pose": "celebrating", "gesture": "brief celebratory bounce",
                "visual": "topic_payoff", "sound_effect": "comic_surprise",
            }
        if is_correction:
            return {
                "emotion": "frustrated emphasis", "intensity": 0.74,
                "delivery": "mock the misconception with one controlled sarcastic emphasis",
                "pose": "frustrated_burst", "gesture": "brief forward lean then point to correction",
                "visual": "topic_myth_correction", "sound_effect": "cartoon_impact",
            }
        if is_reveal:
            return {
                "emotion": "surprised reveal", "intensity": 0.72,
                "delivery": "build curiosity, pause briefly, then land the factual reveal",
                "pose": "idea_reveal", "gesture": "point to the comparison as it appears",
                "visual": "topic_reveal", "sound_effect": "reveal",
            }
        return {
            "emotion": "assertive explanation", "intensity": 0.46,
            "delivery": "confident, conversational Hindi/Hinglish; keep the fact clear",
            "pose": "serious_fact", "gesture": "point to the relevant diagram",
            "visual": "topic_diagram", "sound_effect": None,
        }
    if narrator_preset != "desi_savage_aggressive":
        # Standard narration keeps the same useful topic visuals while removing
        # the sarcastic/frustrated performance and exaggerated reactions.
        if any(term in text for term in ("90%", "नब्बे", "धरती की", "ग्रैविटी")):
            visual = "earth_iss_gravity_comparison"
        elif any(term in text for term in ("आगे बढ़", "कक्षा", "गिरती रहती", "मिस करती")):
            visual = "orbiting_iss"
        elif any(term in text for term in ("साथ गिर", "छोड़ा हुआ पेन", "यात्री")):
            visual = "shared_freefall"
        elif index == 0 and any(mark in sentence for mark in ("?", "क्या", "क्यों")):
            visual = "gravity_misconception"
        elif index == total - 1:
            visual = "freefall_elevator_payoff"
        else:
            visual = "iss_orbit_context"
        return {
            "emotion": "neutral explanation",
            "intensity": 0.32,
            "delivery": "clear, neutral Hindi explanation; measured and easy to understand",
            "pose": "explaining",
            "gesture": "subtle speaking movement",
            "visual": visual,
            "sound_effect": None,
        }
    if index == 0 and any(mark in sentence for mark in ("?", "क्या", "क्यों")):
        return {
            "emotion": "mocking disbelief", "intensity": 0.78,
            "delivery": "young Indian male; assertive, sarcastic question; punchy, not shouted",
            "pose": "skeptical", "gesture": "head tilt then palm-up challenge",
            "visual": "gravity_misconception", "sound_effect": "pop",
        }
    if index == total - 1 and any(term in text for term in ("घोस्ट", "ghost", "फिजिक्स ने तुम्हें")):
        return {
            "emotion": "comedic payoff", "intensity": 0.82,
            "delivery": "playful, confident punchline; lift the final words without shouting",
            "pose": "celebrating", "gesture": "brief celebratory bounce",
            "visual": "freefall_elevator_payoff", "sound_effect": "comic_surprise",
        }
    if any(term in text for term in ("कौन बोला", "गलत")):
        return {
            "emotion": "frustrated emphasis", "intensity": 0.86,
            "delivery": "mock the misconception with one short controlled emphasis; keep the viewer on the narrator's side",
            "pose": "frustrated_burst", "gesture": "brief forward lean",
            "visual": "iss_gravity_comparison", "sound_effect": "cartoon_impact",
        }
    if any(term in text for term in ("घंटा", "नौटंकी", "चक्कर", "पंचलाइन")):
        return {
            "emotion": "sarcastic payoff", "intensity": 0.88,
            "delivery": "dry, smug punchline with a short pause before the final phrase",
            "pose": "smug", "gesture": "small shrug then hold",
            "visual": "freefall_elevator_payoff", "sound_effect": "comic_surprise",
        }
    if any(term in text for term in ("90%", "नब्बे", "धरती की", "ग्रैविटी")):
        return {
            "emotion": "confident explanation", "intensity": 0.42,
            "delivery": "clear, assured science explanation; emphasize the number, conversational pace",
            "pose": "serious_fact", "gesture": "point to the comparison label",
            "visual": "earth_iss_gravity_comparison", "sound_effect": None,
        }
    if any(term in text for term in ("आगे बढ़", "कक्षा", "गिरती रहती", "मिस करती")):
        return {
            "emotion": "explaining", "intensity": 0.48,
            "delivery": "steady explanation with forward momentum; articulate the orbital cause",
            "pose": "pointing_right", "gesture": "trace the curved orbit",
            "visual": "orbiting_iss", "sound_effect": "whoosh",
        }
    if any(term in text for term in ("साथ गिर", "छोड़ा हुआ पेन", "यात्री")):
        return {
            "emotion": "surprised reveal", "intensity": 0.7,
            "delivery": "build curiosity, pause briefly, then reveal that all objects fall together",
            "pose": "idea_reveal", "gesture": "point at the matching fall arrows",
            "visual": "shared_freefall", "sound_effect": "reveal",
        }
    if index == total - 1:
        return {
            "emotion": "comedic payoff", "intensity": 0.82,
            "delivery": "playful, confident punchline; lift the final words without shouting",
            "pose": "celebrating", "gesture": "brief celebratory bounce",
            "visual": "freefall_elevator_payoff", "sound_effect": "comic_surprise",
        }
    return {
        "emotion": "assertive explanation", "intensity": 0.46,
        "delivery": "confident, energetic Hindi/Hinglish; keep the factual explanation clear and conversational",
        "pose": "explaining", "gesture": "subtle speaking movement",
        "visual": "iss_orbit_context", "sound_effect": None,
    }


_PROFANITY = re.compile(r"(?i)\b(?:saala|saale|chutiya|chod|bhenchod|madarchod|gaand|fuck|shit)\b|साला|चूतिया|भेंचोद|मादरचोद")


def _visual_selection(sentence: str, mood: dict) -> dict:
    """Choose a beat-level visual and explain why it fits the narration."""
    text = sentence.casefold()
    if any(term in text for term in ("बादल", "cloud")):
        if any(term in text for term in ("बूंद", "बूँद", "कण", "droplet", "water drop")):
            return {
                "visual_mode": "animated_diagram",
                "top_visual": "cloud_droplets",
                "stock_query": None,
                "visual_rationale": "Show the tiny water and ice particles that make up a cloud; generic macro water footage would imply the wrong scale.",
            }
        if (any(term in text for term in ("हवाई जहाज़", "हवाई जहाज", "airplane", "aircraft"))
                and not any(term in text for term in ("टन", "बोइंग", "737"))):
            return {
                "visual_mode": "stock_video",
                "top_visual": "stock_clouds",
                "stock_query": "aerial moving cloudscape above the clouds",
                "visual_rationale": "Show real moving clouds while the short heading frames the airplane comparison; the saved script has no airplane footage candidate, so the comparison remains a visual title rather than misleading stock.",
            }
        if any(term in text for term in ("टन", "वजन", "भार", "बोइंग", "हवाई जहाज़", "airplane", "aircraft")):
            return {
                "visual_mode": "comparison",
                "top_visual": "cloud_weight_comparison",
                "stock_query": None,
                "visual_rationale": "An animated scale and airplane comparison communicates the surprising number more clearly than unrelated footage.",
            }
        if any(term in text for term in ("तैरता", "गुब्बारा", "float", "balloon", "मेहनत")):
            return {
                "visual_mode": "illustration",
                "top_visual": "cloud_punchline",
                "stock_query": None,
                "visual_rationale": "An original cartoon cloud reaction lands the spoken punchline without implying a literal mechanism.",
            }
        return {
            "visual_mode": "stock_video",
            "top_visual": "stock_clouds",
            "stock_query": "aerial moving cloudscape above the clouds",
            "visual_rationale": "Actual moving cloud footage anchors this real sky subject; fall back to a cloud illustration if no suitable approved clip is cached.",
        }
    if any(term in text for term in ("रेस्टोरेंट सोचो", "restaurant सोचो", "restaurant imagine")):
        return {
            "visual_mode": "illustration",
            "top_visual": "restaurant_setup",
            "stock_query": None,
            "visual_rationale": "Establish the restaurant analogy with an original illustrated scene before showing the customer ordering.",
        }
    if any(term in text for term in (
        "restaurant", "रेस्टोरेंट", "ऑर्डर", "order", "customer", "ग्राहक",
        "kitchen", "किचन", "खाना तैयार", "prepare",
    )):
        query = "restaurant customer placing a food order"
        if any(term in text for term in ("kitchen", "किचन", "खाना तैयार", "prepare")):
            query = "chef preparing a meal in a restaurant kitchen"
        return {
            "visual_mode": "stock_video",
            "top_visual": "kitchen_server" if any(term in text for term in ("kitchen", "किचन", "खाना तैयार", "prepare")) else "restaurant_order",
            "stock_query": query,
            "visual_rationale": "A recognizable real-world restaurant activity benefits from footage; use an illustrated scene if no approved local clip is available.",
        }
    if (any(term in text for term in ("client", "क्लाइंट"))
            and any(term in text for term in ("server", "सर्वर"))
            and not any(term in text for term in ("request", "रिक्वेस्ट", "response", "रिस्पॉन्स", "process", "प्रोसेस"))
            and any(term in text for term in ("माँगता", "मांगता", "जवाब देता", "देता है"))):
        return {
            "visual_mode": "animated_diagram",
            "top_visual": "client_server_summary",
            "stock_query": None,
            "visual_rationale": "A two-way Client/Server summary diagram makes the memorable takeaway explicit.",
        }
    if any(term in text for term in ("request", "रिक्वेस्ट", "response", "रिस्पॉन्स", "process", "प्रोसेस")):
        return {
            "visual_mode": "animated_diagram",
            "top_visual": "request_response_flow",
            "stock_query": None,
            "visual_rationale": "A labeled request → processing → response diagram makes this invisible technical flow clear.",
        }
    if any(term in text for term in ("browser", "ब्राउज़र", "youtube", "यूट्यूब")):
        return {
            "visual_mode": "animated_diagram",
            "top_visual": "browser_youtube_exchange",
            "stock_query": None,
            "visual_rationale": "Browser, server, and returned data are abstract communication; an original labeled diagram is more precise than generic footage.",
        }
    return {
        "visual_mode": "illustration",
        "top_visual": "terminology_roast" if any(term in text for term in ("client", "क्लाइंट", "server", "सर्वर", "नाम")) else mood["visual"],
        "stock_query": None,
        "visual_rationale": "Use the stickman and a topic-specific animated text, comparison, or reaction for this conceptual beat.",
    }


def _short_visual_title(sentence: str, visual: dict, index: int) -> str:
    visual_id = str(visual.get("top_visual", ""))
    titles = {
        "stock_clouds": "बादल vs प्लेन",
        "cloud_plane_comparison": "बादल vs प्लेन",
        "cloud_droplets": "छोटी बूंदें",
        "cloud_weight_comparison": "500 टन?!",
        "cloud_punchline": "बादल ओवरटाइम?",
        "cloud_reveal": "सच में?",
        "restaurant_setup": "रेस्टोरेंट",
        "restaurant_order": "Client vs Server",
        "kitchen_server": "Server का काम",
        "request_response_flow": "Request → Response",
        "browser_youtube_exchange": "Browser ↔ YouTube",
        "client_server_summary": "नाम भारी, काम सीधा",
        "terminology_roast": "Client vs Server",
    }
    if visual_id in titles:
        return titles[visual_id]
    return "आज का सच" if index == 0 else "समझो ज़रा"


def build(
    script: str,
    *,
    narrator_preset: str = "standard",
    profanity_level: str = "savage_monetization_aware",
) -> list[dict]:
    """Build explicit word-indexed beats; no random or timer-driven emotion changes."""
    sentences = [match.group(0).strip() for match in _SENTENCE.finditer(script or "")
                 if match.group(0).strip()]
    expanded_sentences = []
    for sentence in sentences:
        split_at = sentence.casefold().find("लगभग")
        if (split_at > 0 and any(term in sentence.casefold() for term in ("बूंद", "बूँद", "कण"))
                and any(term in sentence.casefold() for term in ("टन", "बोइंग"))):
            expanded_sentences.extend((sentence[:split_at].strip(), sentence[split_at:].strip()))
        else:
            expanded_sentences.append(sentence)
    sentences = expanded_sentences
    beats = []
    word_cursor = 0
    for index, sentence in enumerate(sentences):
        word_count = len(_WORD.findall(sentence))
        if not word_count:
            continue
        mood = _classify(sentence, index, len(sentences), narrator_preset)
        contains_profanity = bool(_PROFANITY.search(sentence))
        delivery = mood["delivery"]
        intensity = mood["intensity"]
        if contains_profanity and profanity_level != "mild":
            delivery += "; aim the controlled comic emphasis at the confusing idea, never at the viewer"
            intensity = min(1.0, intensity + 0.04)
        visual_text = sentence
        if any(term in (script or "").casefold() for term in ("बादल", "cloud")) and not any(
            term in sentence.casefold() for term in ("बादल", "cloud")
        ):
            visual_text = "बादल " + sentence
        visual = _visual_selection(visual_text, mood)
        if (index == 1 and any(term in (script or "").casefold() for term in ("बादल", "cloud"))
                and any(term in sentence.casefold() for term in ("सच है", "हाँ", "yes"))):
            visual = {
                "visual_mode": "animated_diagram",
                "top_visual": "cloud_reveal",
                "stock_query": None,
                "visual_rationale": "A brief animated reveal emphasizes the saved narration's confirmation before the measured explanation.",
            }
        caption_terms = (
            ("Client", ("Client", "क्लाइंट")),
            ("Server", ("Server", "सर्वर")),
            ("Request", ("Request", "रिक्वेस्ट")),
            ("Response", ("Response", "रिस्पॉन्स")),
            ("Browser", ("Browser", "ब्राउज़र")),
            ("YouTube", ("YouTube", "यूट्यूब")),
        )
        beats.append({
            "beat_id": f"beat_{index + 1:02d}", "start_word": word_cursor,
            "end_word": word_cursor + word_count, "text": sentence,
            "emotion": mood["emotion"], "intensity": intensity,
            "delivery": delivery, "pose": mood["pose"],
            "gesture": mood["gesture"], "top_visual": visual["top_visual"],
            **visual,
            "short_visual_title": _short_visual_title(sentence, visual, index),
            "humor_target": "jargon_or_misconception" if narrator_preset == "desi_savage_aggressive" else None,
            "caption_emphasis": [display for display, variants in caption_terms
                                 if any(variant.casefold() in sentence.casefold() for variant in variants)],
            "sound_effect": mood["sound_effect"],
            "profanity_level": profanity_level,
            "contains_profanity": contains_profanity,
        })
        word_cursor += word_count
    return beats


def resolve(beats: list[dict], cues: list[tuple[float, float, str]]) -> list[dict]:
    """Resolve word-index ranges against actual Vakyansh/WhisperX aligned cues."""
    if not beats or not cues:
        return []
    resolved = []
    for beat in beats:
        start_word, end_word = int(beat["start_word"]), int(beat["end_word"])
        if start_word < 0 or end_word > len(cues) or end_word <= start_word:
            raise ValueError(f"emotion beat {beat.get('beat_id')} is outside aligned narration")
        resolved.append({
            **beat,
            "start": float(cues[start_word][0]),
            "end": float(cues[end_word - 1][1]),
            "start_word_text": cues[start_word][2],
            "end_word_text": cues[end_word - 1][2],
        })
    return resolved


def gemini_segments(beats: list[dict]) -> list[dict]:
    """Keep delivery instructions outside the transcript sent for exact recitation."""
    return [{
        "type": "text", "text": beat["text"],
        "annotations": [{"type": "speech_metadata", "style": beat["delivery"]}],
    } for beat in beats if beat.get("text", "").strip()]


def legacy_gemini_prompt(script: str, beats: list[dict]) -> str:
    """Build the documented prompt-guided format for pre-3.8 Gemini TTS."""
    profile = (
        "Audio Profile: Young adult Indian Hindi/Hinglish narrator; confident, clear, "
        "conversational, and expressive. Preserve natural pronunciation."
    )
    instructions = []
    for beat in beats:
        spoken = str(beat.get("text", "")).strip()
        delivery = str(beat.get("delivery", "")).strip()
        if spoken and delivery:
            instructions.append(f'- For the transcript words "{spoken}": {delivery}')
    director = "\n".join(instructions) or "- Speak naturally and clearly."
    return (
        f"{profile}\n\nScene: Short Hindi factual narration.\n\n"
        "Director's Notes: Apply the following performance directions without speaking them. "
        "Speak only the exact text in the Transcript section.\n"
        f"{director}\n\nTranscript:\n{script.strip()}"
    )
