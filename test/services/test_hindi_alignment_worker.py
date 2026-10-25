from app.services.hindi_alignment_worker import alignment_text


def test_alignment_text_omits_standalone_dash_but_keeps_word_punctuation():
    assert alignment_text("हैं – दबाव गिरने पर।") == "हैं दबाव गिरने पर।"


def test_saved_savage_task_audio_rejects_the_low_confidence_hain_alignment():
    import io
    import json
    import os
    import tempfile
    from pathlib import Path

    import pytest
    from loguru import logger

    from app.services import subtitle
    from app.utils import utils

    root = Path(utils.root_dir())
    task_dir = root / "storage/tasks/4c73c727-8155-4206-ac91-1169d0485139"
    audio = task_dir / "audio.mp3"
    script_file = task_dir / "script.json"
    model = root / "models/hindi-alignment-vakyansh"
    alignment_python = subtitle._hindi_alignment_environment().get(
        "MPT_HINDI_ALIGNMENT_PYTHON", ""
    )
    if not (audio.is_file() and script_file.is_file() and model.is_dir()
            and alignment_python and os.path.isfile(alignment_python)):
        pytest.skip("saved task audio or local Vakyansh alignment setup is unavailable")

    script = json.loads(script_file.read_text(encoding="utf-8"))["script"]
    diagnostic = io.StringIO()
    sink = logger.add(diagnostic, level="ERROR", format="{message}")
    try:
        with tempfile.TemporaryDirectory(prefix="saved-hindi-alignment-test-") as temp_dir:
            subtitle_file = Path(temp_dir) / "captions.srt"
            aligned = subtitle.align_hindi_words(str(audio), script, str(subtitle_file))
            assert not aligned
            assert not subtitle_file.exists()
    finally:
        logger.remove(sink)

    message = diagnostic.getvalue()
    assert "Hindi word alignment rejected word 27 'हैं'" in message
    assert "score=0.005" in message
    assert "start=8.426" in message
    assert "end=8.487" in message
