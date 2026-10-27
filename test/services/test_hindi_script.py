import json
from unittest.mock import patch

from app.services.hindi_script import build_representations
from app.services.hindi_alignment_worker import alignment_text
from app.services.animated_explainer import _display_cues
from app.models.schema import VideoParams
from app.services import subtitle, task


def test_curated_technical_terms_and_hinglish_keep_caption_text_and_strip_markdown():
    result = build_representations(
        "तू है **Client**, Server Request Response Browser YouTube और Wi-Fi!"
    )
    assert result.spoken_script == (
        "तू है क्लाइंट, सर्वर रिक्वेस्ट रिस्पॉन्स ब्राउज़र यूट्यूब और वाई-फाई!"
    )
    assert result.display_words[2].text == "Client,"
    assert result.display_script == "तू है Client, Server Request Response Browser YouTube और Wi-Fi!"
    assert result.display_words[2].spoken_start == 2
    assert result.display_words[2].spoken_end == 3
    assert "**" not in result.spoken_script


def test_one_display_token_maps_to_multiple_alignment_words_and_numerals():
    result = build_representations("90% लोग Client")
    assert result.spoken_script == "नब्बे प्रतिशत लोग क्लाइंट"
    assert result.display_words[0].spoken_start == 0
    assert result.display_words[0].spoken_end == 2
    assert result.display_words[1].spoken_start == 2
    assert result.display_words[2].spoken_start == 3
    assert alignment_text(result.spoken_script) == result.spoken_script


def test_pure_hindi_english_terms_abbreviations_and_unknown_identifiers():
    hindi = build_representations("नमस्ते दुनिया!")
    assert hindi.spoken_script == "नमस्ते दुनिया!"
    english_terms = build_representations("API CPU YouTube", "hi")
    assert english_terms.spoken_script == "एपीआई सीपीयू यूट्यूब"
    assert build_representations("ID-15 user_id https://x.test", "hi").spoken_script == (
        "ID-15 user_id https://x.test"
    )
    assert build_representations("Hello API", "en").spoken_script == "Hello API"


def test_unseen_uppercase_initialisms_use_letter_names_without_dictionary_entries():
    result = build_representations("QZX और R2D")
    assert result.spoken_script == "क्यू ज़ेड एक्स और आर दो डी"
    assert result.display_words[0].spoken_end == 3
    assert result.display_words[1].spoken_start == 3


def test_hyphenated_technical_words_preserve_display_and_align_as_pronunciation():
    result = build_representations("वाई-फाई Wi-Fi")
    assert result.spoken_script == "वाई-फाई वाई-फाई"
    assert [item.text for item in result.display_words] == ["वाई-फाई", "Wi-Fi"]


def test_chatgpt_and_ai_map_to_devanagari_before_hindi_alignment(tmp_path, monkeypatch):
    editable = "क्या ChatGPT और AI शिक्षकों की मदद कर सकते हैं?"
    params = VideoParams(
        video_subject="AI for teachers", video_language="Hindi",
        voice_name="gemini:Puck", subtitle_display_mode="word_by_word",
    )
    spoken = task._represent_script(params, editable).spoken_script
    assert spoken == "क्या चैटजीपीटी और एआई शिक्षकों की मदद कर सकते हैं?"
    _set_test_vocab(tmp_path, monkeypatch, spoken)
    assert task._alignment_preflight_errors(params, spoken, editable) == []


def test_ambiguous_hinglish_proper_nouns_require_resolution_before_tts(tmp_path, monkeypatch):
    editable = (
        "Greenland और Iceland के बीच ठंडा पानी बहता है। "
        "इसे Denmark Strait Cataract कहते हैं।"
    )
    params = VideoParams(
        video_subject="Underwater waterfall", video_language="Hindi",
        voice_name="gemini:Puck", subtitle_display_mode="word_by_word",
    )
    spoken = task._represent_script(params, editable).spoken_script
    _set_test_vocab(tmp_path, monkeypatch, spoken)
    errors = task._alignment_preflight_errors(params, spoken, editable)
    assert any(item["code"] == "unresolved_latin_pronunciation" for item in errors)

    params.pronunciation_overrides = {
        "Greenland": "ग्रीनलैंड", "Iceland": "आइसलैंड",
        "Denmark": "डेनमार्क", "Strait": "स्ट्रेट", "Cataract": "कैटरैक्ट",
    }
    spoken = task._represent_script(params, editable).spoken_script
    _set_test_vocab(tmp_path, monkeypatch, spoken)
    assert task._alignment_preflight_errors(params, spoken, editable) == []

    with patch.object(task, "resolve_custom_audio_file", return_value=None), \
         patch.object(task.utils, "task_dir", return_value=str(tmp_path)), \
         patch.object(task.voice, "tts", return_value=object()) as synthesize, \
         patch.object(task.voice, "get_audio_duration", return_value=8), \
         patch.object(task.sm.state, "update_task"):
        task.generate_audio("mixed-language-audio", params, editable)

    assert synthesize.call_args.kwargs["text"] == spoken


def test_unresolved_latin_term_is_reported_before_tts(tmp_path, monkeypatch):
    script = "यह QZX वैज्ञानिक शब्द Cloudberry के बारे में है।"
    params = VideoParams(
        video_subject="unseen terminology", video_language="hi",
        voice_name="gemini:Puck", subtitle_display_mode="word_by_word",
    )
    spoken = task._represent_script(params, script).spoken_script
    _set_test_vocab(tmp_path, monkeypatch, spoken)
    with patch.object(task, "_mark_task_failed") as fail, \
         patch.object(task.voice, "tts") as synthesize:
        result = task.generate_audio("unresolved-term", params, script)

    assert result == (None, None, None)
    fail.assert_called_once()
    assert fail.call_args.args[1] == "preflight"
    assert "Cloudberry" in fail.call_args.kwargs["details"]["preflight_errors"][0]["tokens"]
    synthesize.assert_not_called()


def test_english_script_does_not_use_hindi_alignment_preflight():
    params = VideoParams(
        video_subject="English topic", video_language="English",
        voice_name="gemini:Puck", subtitle_display_mode="word_by_word",
    )
    assert task._alignment_preflight_errors(params, "Greenland Iceland Denmark Strait", "") == []


def test_caption_display_words_receive_the_combined_spoken_timing_span():
    result = build_representations("90% लोग")
    aligned = [(0.1, 0.3, "नब्बे"), (0.3, 0.6, "प्रतिशत"), (0.7, 0.9, "लोग")]
    assert _display_cues(aligned, result.display_map()) == [
        (0.1, 0.6, "90%"), (0.7, 0.9, "लोग")
    ]


def test_explicit_hinglish_slang_override_preserves_display_to_alignment_span():
    result = build_representations("यह bc था", pronunciation_overrides={"bc": "बी सी"})
    assert result.spoken_script == "यह बी सी था"
    mapping = next(item for item in result.display_map() if item["text"] == "bc")
    assert mapping == {"text": "bc", "spoken_start": 1, "spoken_end": 3}


def _set_test_vocab(tmp_path, monkeypatch, text):
    vocab = {char: index for index, char in enumerate(set(text))}
    (tmp_path / "vocab.json").write_text(json.dumps(vocab, ensure_ascii=False), encoding="utf-8")
    worker_python = tmp_path / "alignment-python"
    worker_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(subtitle, "_hindi_alignment_environment", lambda: {
        "MPT_HINDI_ALIGNMENT_MODEL": str(tmp_path),
        "MPT_HINDI_ALIGNMENT_PYTHON": str(worker_python),
    })


def test_alignment_preflight_accepts_hinglish_abbreviations_and_explicit_override(tmp_path, monkeypatch):
    plain = build_representations("सच यह है कि bc काम करता है").spoken_script
    _set_test_vocab(tmp_path, monkeypatch, plain)
    errors = subtitle.preflight_hindi_alignment_script(plain, source_text="सच यह है कि bc काम करता है")
    assert any(error["code"] == "unresolved_latin_pronunciation" for error in errors)

    resolved = build_representations(
        "सच यह है कि bc काम करता है", pronunciation_overrides={"bc": "बी सी"}
    )
    _set_test_vocab(tmp_path, monkeypatch, resolved.spoken_script)
    assert subtitle.preflight_hindi_alignment_script(
        resolved.spoken_script, source_text="सच यह है कि bc काम करता है"
    ) == []


def test_preflight_detects_unknown_alignment_characters_and_dropped_symbols(tmp_path, monkeypatch):
    _set_test_vocab(tmp_path, monkeypatch, "सच है")
    errors = subtitle.preflight_hindi_alignment_script("सच क़ है", source_text="सच क़ है")
    assert any(error["code"] == "unsupported_alignment_characters" for error in errors)
    assert any(error.get("character") == "✨" for error in subtitle.preflight_hindi_alignment_script(
        "सच है", source_text="सच ✨ है"
    ))


def test_hinglish_tts_runs_and_cached_audio_avoids_repeating_tts(tmp_path, monkeypatch):
    spoken = "सच यह है कि बी सी काम करता है क्लाइंट"
    _set_test_vocab(tmp_path, monkeypatch, spoken)
    params = VideoParams(
        video_subject="client behavior", video_language="Hindi", voice_name="gemini:Puck",
        subtitle_display_mode="word_by_word", pronunciation_overrides={"bc": "बी सी"},
    )
    with patch.object(task, "resolve_custom_audio_file", return_value=None), \
         patch.object(task.utils, "task_dir", return_value=str(tmp_path)), \
         patch.object(task.voice, "tts", return_value=object()) as tts, \
         patch.object(task.voice, "get_audio_duration", return_value=2), \
         patch.object(task.sm.state, "update_task"):
        result = task.generate_audio("preflight-task", params, "यह bc काम करता है")
    assert result[:2] == (str(tmp_path / "audio.mp3"), 2)
    assert tts.call_args.kwargs["text"] == "यह बी सी काम करता है"

    audio = tmp_path / "cached.mp3"
    audio.write_bytes(b"cached audio")
    monkeypatch.setattr(task.task_artifacts, "load_cached_artifact", lambda *args: {"path": str(audio)})
    monkeypatch.setattr(task.voice, "get_audio_duration", lambda _path: 2.5)
    params.spoken_script = spoken
    with patch.object(task.voice, "tts") as tts:
        result = task.generate_audio("preflight-task", params, "सच यह है कि क्लाइंट काम करता है")
    assert result[:2] == (str(audio), 3)
    tts.assert_not_called()
