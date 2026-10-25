from unittest.mock import patch

from app.models.schema import VideoParams
from app.services import hindi_numbers, task
from app.services.hindi_alignment_worker import alignment_text


def test_hindi_number_normalization_handles_numbers_dates_percentages_and_units():
    assert hindi_numbers.normalize_hindi_numbers("15 घर और 3 पेड़") == "पंद्रह घर और तीन पेड़"
    assert hindi_numbers.normalize_hindi_numbers("90% लोग") == "नब्बे प्रतिशत लोग"
    assert hindi_numbers.normalize_hindi_numbers("2026") == "दो हज़ार छब्बीस"
    assert hindi_numbers.normalize_hindi_numbers("2026-10-24") == "चौबीस अक्टूबर दो हज़ार छब्बीस"
    assert hindi_numbers.normalize_hindi_numbers("3.5 kg") == "तीन दशमलव पाँच किलोग्राम"
    assert hindi_numbers.normalize_hindi_numbers("15% और ३ दिन") == "पंद्रह प्रतिशत और तीन दिन"
    aligned = alignment_text(hindi_numbers.normalize_hindi_numbers("हर 15 घरों में 3 लाइटें हैं।"))
    assert not any(char.isascii() and char.isdigit() for char in aligned)


def test_english_text_and_hyphenated_identifiers_are_not_misread_as_hindi_numbers():
    assert hindi_numbers.normalize_hindi_numbers("15 days", "en") == "15 days"
    assert hindi_numbers.normalize_hindi_numbers("ID-15 is registered") == "ID-15 is registered"


def test_tts_receives_normalized_text_while_editable_script_is_preserved(tmp_path):
    params = VideoParams(
        video_subject="Village lights", video_script="हर 15 घरों पर 3 लाइटें हैं।",
        video_language="hi", visual_style="relevant_stock", voice_name="test:voice",
    )
    submaker = object()
    with patch.object(task, "resolve_custom_audio_file", return_value=None), \
         patch.object(task.utils, "task_dir", return_value=str(tmp_path)), \
         patch.object(task.voice, "tts", return_value=submaker) as synthesize, \
         patch.object(task.voice, "get_audio_duration", return_value=8), \
         patch.object(task.sm.state, "update_task"):
        task.generate_audio("numeric-audio", params, params.video_script)

    assert synthesize.call_args.kwargs["text"] == "हर पंद्रह घरों पर तीन लाइटें हैं।"
    assert params.video_script == "हर 15 घरों पर 3 लाइटें हैं।"
    assert params.spoken_script == synthesize.call_args.kwargs["text"]


def test_tts_receives_hinglish_pronunciations_without_markdown(tmp_path):
    editable = "तू **Client** है, YouTube पर 90% वीडियो देखता है।"
    params = VideoParams(
        video_subject="Hinglish", video_script=editable,
        video_language="hi", visual_style="relevant_stock", voice_name="test:voice",
    )
    with patch.object(task, "resolve_custom_audio_file", return_value=None), \
         patch.object(task.utils, "task_dir", return_value=str(tmp_path)), \
         patch.object(task.voice, "tts", return_value=object()) as synthesize, \
         patch.object(task.voice, "get_audio_duration", return_value=8), \
         patch.object(task.sm.state, "update_task"):
        task.generate_audio("hinglish-audio", params, editable)

    assert synthesize.call_args.kwargs["text"] == (
        "तू क्लाइंट है, यूट्यूब पर नब्बे प्रतिशत वीडियो देखता है।"
    )
    assert params.video_script == editable
