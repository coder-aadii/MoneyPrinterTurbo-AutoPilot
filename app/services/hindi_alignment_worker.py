"""Optional WhisperX worker; run with MPT_HINDI_ALIGNMENT_PYTHON's interpreter.

This module intentionally imports no application dependencies, so WhisperX and
its PyTorch stack can live outside the project's locked Python environment.
"""

import json
import os
import sys
import unicodedata
from pathlib import Path

from app.services.hindi_alignment_charset import missing_alignment_characters


def character_word_coverage(segments: list[dict]) -> list[dict]:
    """Group the raw, non-interpolated character boundaries by spoken word."""
    words = []
    current = []
    for segment in segments:
        for char in segment.get("chars", []):
            value = char.get("char", "")
            if value.isspace():
                if current:
                    words.append(current)
                    current = []
            elif any(unicodedata.category(item)[0] in {"L", "M", "N"} for item in value):
                current.append(char)
    if current:
        words.append(current)
    return [{
        "characters": "".join(char["char"] for char in word),
        "character_count": len(word),
        "measured_character_count": sum(
            all(key in char for key in ("start", "end", "score"))
            and all(isinstance(char[key], (int, float)) for key in ("start", "end", "score"))
            for char in word
        ),
    } for word in words]


def main() -> None:
    import whisperx

    audio_path, script_path, output_path = map(Path, sys.argv[1:4])
    script = alignment_text(script_path.read_text(encoding="utf-8"))
    audio = whisperx.load_audio(str(audio_path))
    duration = len(audio) / 16000
    model_name = os.environ.get("MPT_HINDI_ALIGNMENT_MODEL", "theainerd/Wav2Vec2-large-xlsr-hindi")
    model, metadata = whisperx.load_align_model(
        language_code="hi", device="cpu", model_name=model_name,
        model_cache_only=True,
    )
    dictionary = metadata["dictionary"]
    # The Vakyansh checkpoint emits CTC blank on <s> (ID 0), although its
    # Transformers config advertises <pad> (ID 1). WhisperX normally infers
    # the blank from <pad>; allow an explicit checkpoint-specific override so
    # blank emissions are not treated as repeated transcript frames.
    blank_id = os.environ.get("MPT_HINDI_ALIGNMENT_BLANK_ID", "").strip()
    if blank_id:
        try:
            blank_id = int(blank_id)
        except ValueError as exc:
            raise ValueError("MPT_HINDI_ALIGNMENT_BLANK_ID must be an integer") from exc
        if not 0 <= blank_id < model.config.vocab_size:
            raise ValueError("MPT_HINDI_ALIGNMENT_BLANK_ID is outside the model vocabulary")
        dictionary["<pad>"] = blank_id
    missing = missing_alignment_characters(script, dictionary)
    if missing:
        raise ValueError(f"Hindi alignment model cannot represent script characters: {missing}")
    result = whisperx.align(
        [{"start": 0.0, "end": duration, "text": script}],
        model, metadata, audio, "cpu",
        interpolate_method="ignore",
        return_char_alignments=True,
    )
    output_path.write_text(json.dumps({
        "model": model_name,
        "audio_duration": duration,
        "words": result["word_segments"],
        "character_word_coverage": character_word_coverage(result["segments"]),
    }, ensure_ascii=False), encoding="utf-8")


def alignment_text(script: str) -> str:
    """Drop standalone punctuation tokens that are not spoken words.

    WhisperX emits punctuation-only tokens as word segments (for example, an
    en dash surrounded by spaces). The caller validates coverage against
    spoken words, so feeding those tokens to CTC creates a false extra word.
    Punctuation attached to a spoken token remains intact.
    """
    return " ".join(
        token for token in script.split()
        if any(unicodedata.category(char)[0] in {"L", "N"} for char in token)
    )


if __name__ == "__main__":
    main()
