import json
import math
import os
import os.path
import re
import subprocess
import tempfile
import threading
import unicodedata
from pathlib import Path
from timeit import default_timer as timer

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None
from loguru import logger

from app.config import config
from app.models import const
from app.services import alignment_quality, hindi_alignment_charset, task_artifacts
from app.services.alignment_quality import spoken_words as _spoken_words
from app.utils import utils
from app.utils.subtitle_reader import file_to_subtitles as file_to_subtitles
from app.utils.subtitle_writer import write_subtitle_file

model_size = config.whisper.get("model_size", "large-v3")
device = config.whisper.get("device", "cpu")
compute_type = config.whisper.get("compute_type", "int8")
initial_prompt = config.whisper.get("initial_prompt", "") or None
model = None
_model_init_lock = threading.Lock()


def _hindi_alignment_environment() -> dict[str, str]:
    """Read alignment settings from the process environment or root .env."""
    names = {
        "MPT_HINDI_ALIGNMENT_PYTHON",
        "MPT_HINDI_ALIGNMENT_MODEL",
        "MPT_HINDI_ALIGNMENT_BLANK_ID",
    }
    environment = os.environ.copy()
    missing = names.difference(environment)
    if not missing:
        return environment
    env_file = Path(utils.root_dir()) / ".env"
    try:
        with env_file.open(encoding="utf-8") as source:
            for line in source:
                name, separator, value = line.partition("=")
                name = name.strip().removeprefix("export ").strip()
                if separator and name in missing:
                    environment[name] = value.strip().strip("\"'")
    except OSError:
        pass
    return environment


def preflight_hindi_alignment_script(script: str, *, source_text: str | None = None) -> list[dict]:
    """Check the exact canonical transcript against the configured CTC vocab."""
    normalized = unicodedata.normalize(
        "NFC", utils.normalize_script_for_subtitle_matching(script)
    )
    alignment_text = " ".join(
        token for token in normalized.split()
        if any(unicodedata.category(char)[0] in {"L", "N"} for char in token)
    )
    latin_tokens = re.findall(
        r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*(?![A-Za-z0-9_])",
        alignment_text,
    )
    errors = []
    if source_text is not None:
        from app.services import hindi_script
        errors.extend(hindi_script.source_symbol_issues(source_text))
    for char in sorted(set(alignment_text)):
        if unicodedata.category(char)[0] in {"S", "C"}:
            errors.append({
                "code": "unsupported_alignment_symbol", "character": char,
                "message": f"Unsupported alignment symbol {char!r}; spell out the intended speech.",
            })
    if latin_tokens:
        for token in dict.fromkeys(latin_tokens):
            if token.casefold() == "bc":
                suggestion = (
                    "replace it with the exact, unambiguous words you intend the narrator to say; "
                    "the abbreviation is ambiguous and will not be expanded automatically"
                )
            else:
                suggestion = "add a curated Devanagari pronunciation or spell the intended pronunciation"
            errors.append({
                "code": "unresolved_latin_token", "token": token,
                "message": f"Unsupported Hindi alignment token {token!r}; {suggestion}.",
            })
    environment = _hindi_alignment_environment()
    model_path = Path(environment.get("MPT_HINDI_ALIGNMENT_MODEL", "")).expanduser()
    if not model_path.is_absolute():
        model_path = Path(utils.root_dir()) / model_path
    vocab_file = model_path / "vocab.json"
    try:
        dictionary = json.loads(vocab_file.read_text(encoding="utf-8"))
        if not isinstance(dictionary, dict):
            raise ValueError("vocab.json must contain a JSON object")
    except (OSError, ValueError, TypeError) as exc:
        errors.append({
            "code": "alignment_vocabulary_unavailable",
            "message": (
                "Cannot verify Hindi alignment characters before TTS because the configured "
                f"alignment vocabulary is unavailable ({type(exc).__name__}: {exc}). "
                "Configure a local MPT_HINDI_ALIGNMENT_MODEL/vocab.json first."
            ),
        })
    else:
        missing = hindi_alignment_charset.missing_alignment_characters(alignment_text, dictionary)
        if missing:
            errors.append({
                "code": "unsupported_alignment_characters", "characters": missing,
                "message": f"Hindi alignment model cannot represent characters: {missing}",
            })
    return errors


def _ensure_model_loaded(*, local_only: bool = False) -> bool:
    """Load the large Whisper model once, even when jobs start concurrently."""
    global model
    if model is not None:
        return True

    with _model_init_lock:
        if model is not None:
            return True
        model_path = f"{utils.root_dir()}/models/whisper-{model_size}"
        model_bin_file = f"{model_path}/model.bin"
        if not os.path.isdir(model_path) or not os.path.isfile(model_bin_file):
            model_path = model_size

        logger.info(
            f"loading model: {model_path}, device: {device}, compute_type: {compute_type}"
        )
        try:
            model = WhisperModel(
                model_size_or_path=model_path, device=device, compute_type=compute_type,
                **({"local_files_only": True} if local_only else {}),
            )
        except Exception as e:
            logger.error(
                f"failed to load model: {e} \n\n"
                f"********************************************\n"
                f"this may be caused by network issue. \n"
                f"please download the model manually and put it in the 'models' folder. \n"
                f"see [README.md FAQ](https://github.com/harry0703/MoneyPrinterTurbo) for more details.\n"
                f"********************************************\n\n"
            )
            return False
    return True


def create(
    audio_file,
    subtitle_file: str = "",
    word_level: bool = False,
    log_details: bool = True,
    language: str | None = None,
    local_only: bool = False,
):
    if WhisperModel is None:
        logger.warning("faster_whisper not available, skipping whisper subtitle generation")
        return ""
    if not _ensure_model_loaded(local_only=local_only):
        return None

    if log_details:
        logger.info(f"start, output file: {subtitle_file}")
    if not subtitle_file:
        subtitle_file = f"{audio_file}.srt"

    segments, info = model.transcribe(
        audio_file,
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        **({"initial_prompt": initial_prompt} if initial_prompt else {}),
        **({"language": language} if language else {}),
    )

    logger.info(
        f"detected language: '{info.language}', probability: {info.language_probability:.2f}"
    )

    start = timer()
    subtitles = []

    def recognized(seg_text, seg_start, seg_end):
        seg_text = seg_text.strip()
        if not seg_text:
            return

        if log_details:
            msg = "[%.2fs -> %.2fs] %s" % (seg_start, seg_end, seg_text)
            logger.debug(msg)

        subtitles.append(
            {"msg": seg_text, "start_time": seg_start, "end_time": seg_end}
        )

    for segment in segments:
        if not segment.words:
            # Faster Whisper exposes words as Optional[List[Word]]. A segment
            # can still have usable text and timestamps when alignment yields
            # no words; keep that subtitle in both sentence and word modes.
            recognized(segment.text, segment.start, segment.end)
            continue

        if word_level:
            for word in segment.words:
                cleaned_word = word.word.strip()
                if cleaned_word:
                    recognized(cleaned_word, word.start, word.end)
            continue

        words_idx = 0
        words_len = len(segment.words)

        seg_start = 0
        seg_end = 0
        seg_text = ""

        if segment.words:
            is_segmented = False
            for word in segment.words:
                if not is_segmented:
                    seg_start = word.start
                    is_segmented = True

                seg_end = word.end
                # Accumulate words; only trailing punctuation ends a sentence.
                seg_text += word.word

                if word.word.rstrip().endswith(tuple(const.PUNCTUATIONS)):
                    # Punctuation inside a word (3.14, 1,000, 12:30) is not a
                    # sentence boundary. Remove only actual trailing delimiters.
                    seg_text = seg_text.rstrip().rstrip("".join(const.PUNCTUATIONS))
                    recognized(seg_text, seg_start, seg_end)

                    is_segmented = False
                    seg_text = ""

                if words_idx == 0 and segment.start < word.start:
                    seg_start = word.start
                if words_idx == (words_len - 1) and segment.end > word.end:
                    seg_end = word.end
                words_idx += 1

        if not seg_text:
            continue

        recognized(seg_text, seg_start, seg_end)

    end = timer()

    diff = end - start
    logger.info(f"complete, elapsed: {diff:.2f} s")

    idx = 1
    lines = []
    for subtitle in subtitles:
        text = subtitle.get("msg")
        if text:
            lines.append(
                utils.text_to_srt(
                    idx, text, subtitle.get("start_time"), subtitle.get("end_time")
                )
            )
            idx += 1

    sub = "\n".join(lines) + "\n"
    if not lines:
        logger.warning("transcription produced no subtitle cues")
        return
    write_subtitle_file(subtitle_file, sub)
    if log_details:
        logger.info(f"subtitle file created: {subtitle_file}")


def validate_word_cues(subtitle_file: str, script: str) -> bool:
    """Reject sentence cues and transcripts that do not match spoken script."""
    cues = file_to_subtitles(subtitle_file)
    expected = _spoken_words(utils.normalize_script_for_subtitle_matching(script))
    if not cues or not expected:
        return False
    words = []
    previous_end = 0.0
    for _index, timing, text in cues:
        cue_words = _spoken_words(text)
        if len(cue_words) != 1:
            return False
        try:
            start, end = timing.split(" --> ")
            def seconds(value):
                hours, minutes, remainder = value.split(":")
                return int(hours) * 3600 + int(minutes) * 60 + float(remainder.replace(",", "."))
            start_seconds, end_seconds = seconds(start), seconds(end)
        except (ValueError, TypeError):
            return False
        if (not math.isfinite(start_seconds) or not math.isfinite(end_seconds)
                or start_seconds < previous_end - 0.000001 or end_seconds <= start_seconds):
            return False
        previous_end = end_seconds
        words.extend(cue_words)
    return words == expected


def align_hindi_words(audio_file: str, script: str, subtitle_file: str, *,
                      policy: str = "balanced") -> bool:
    """Cache measured CTC results and revalidate them under the selected policy."""
    environment = _hindi_alignment_environment()
    align_python = environment.get("MPT_HINDI_ALIGNMENT_PYTHON", "").strip()
    normalized = unicodedata.normalize("NFC", utils.normalize_script_for_subtitle_matching(script))
    destination = Path(subtitle_file).parent
    report_file = destination / "alignment-quality.json"
    cache_file = destination / "alignment-raw.json"
    cache_hit = False
    fingerprint = None
    try:
        if not align_python or not os.path.isfile(align_python):
            raise ValueError("Hindi word alignment requires MPT_HINDI_ALIGNMENT_PYTHON")
        if not _spoken_words(normalized) or not os.path.isfile(audio_file):
            raise ValueError("Hindi word alignment requires an audio file and spoken script")
        model_path = Path(environment.get("MPT_HINDI_ALIGNMENT_MODEL", ""))
        model_files = {
            str(p): [p.stat().st_size, p.stat().st_mtime_ns]
            for p in model_path.iterdir() if p.is_file()
        } if model_path.is_dir() and str(model_path) != "." else {}
        fingerprint = task_artifacts.cache_key({
            "audio_sha256": task_artifacts.file_digest(audio_file), "script": normalized,
            "worker_sha256": task_artifacts.file_digest(Path(__file__).with_name("hindi_alignment_worker.py")),
            "model_files": model_files,
            "settings": {key: environment.get(key) for key in (
                "MPT_HINDI_ALIGNMENT_PYTHON", "MPT_HINDI_ALIGNMENT_MODEL", "MPT_HINDI_ALIGNMENT_BLANK_ID")},
        })
        aligned = None
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if (cached["fingerprint"] == fingerprint
                    and task_artifacts.cache_key(cached["result"]) == cached["result_sha256"]):
                aligned = cached["result"]
                cache_hit = True
        except (OSError, ValueError, TypeError, KeyError):
            pass
        if aligned is None:
            with tempfile.TemporaryDirectory(prefix="hindi-align-") as temp_dir:
                input_file = os.path.join(temp_dir, "script.txt")
                result_file = os.path.join(temp_dir, "alignment.json")
                Path(input_file).write_text(normalized, encoding="utf-8")
                result = subprocess.run(
                    [align_python, "-m", "app.services.hindi_alignment_worker", audio_file, input_file, result_file],
                    cwd=utils.root_dir(), capture_output=True, text=True,
                    timeout=900, check=False, env=environment,
                )
                if result.returncode != 0 or not os.path.isfile(result_file):
                    raise ValueError(f"alignment worker exit={result.returncode}, detail={result.stderr[-2000:]}")
                aligned = json.loads(Path(result_file).read_text(encoding="utf-8"))
            # Store even rejected measurements so review or policy changes never
            # require synthesis or replacement of the original word boundaries.
            try:
                task_artifacts._write_json_atomic(cache_file, {
                    "fingerprint": fingerprint, "result": aligned,
                    "result_sha256": task_artifacts.cache_key(aligned),
                })
            except ValueError:
                pass  # Non-finite model output must be reported, never cached as valid.
        report = alignment_quality.evaluate(aligned, normalized, policy)
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        report = {"version": 1, "policy": policy, "accepted": False,
                  "structural_errors": [{"code": "alignment_worker", "message": str(exc)}],
                  "confidence_errors": [], "warnings": []}
    report.update(cache_hit=cache_hit, fingerprint=fingerprint)
    # JSON does not represent NaN/Infinity: preserve invalid values as diagnostic
    # strings; all finite timestamps are stored without alteration.
    def json_safe(value):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        if isinstance(value, dict):
            return {key: json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [json_safe(item) for item in value]
        return value
    task_artifacts._write_json_atomic(report_file, json_safe(report))
    for warning in report["warnings"]:
        logger.warning("Hindi alignment warning: {}", warning)
    for error in report["structural_errors"] + report["confidence_errors"]:
        if "word_index" in error:
            logger.error("Hindi word alignment rejected word {} {!r}: score={}, start={}, end={}, reason={}",
                         error["word_index"], error.get("word"), error.get("score"),
                         error.get("start"), error.get("end"), error["code"])
        else:
            logger.error("Hindi alignment rejected: {}", error)
    if not report["accepted"]:
        return False
    cues = [utils.text_to_srt(i, word["word"], word["start"], word["end"])
            for i, word in enumerate(aligned["words"], start=1)]
    write_subtitle_file(subtitle_file, "\n".join(cues) + "\n")
    return validate_word_cues(subtitle_file, normalized)


def transcribe_audio_bytes(audio_bytes: bytes) -> str:
    """Transcribe in-memory WAV audio and remove every temporary artifact."""
    if not isinstance(audio_bytes, bytes) or not audio_bytes:
        return ""

    with tempfile.TemporaryDirectory(prefix="whisper-transcript-") as temp_dir:
        audio_file = os.path.join(temp_dir, "reference.wav")
        subtitle_file = os.path.join(temp_dir, "reference.srt")
        with open(audio_file, "wb") as output:
            output.write(audio_bytes)

        create_result = create(audio_file, subtitle_file, log_details=False)
        if create_result in (None, "") and not os.path.isfile(subtitle_file):
            return ""
        subtitle_items = file_to_subtitles(subtitle_file)
        return " ".join(item[2].strip() for item in subtitle_items if item[2].strip())


def levenshtein_distance(s1, s2):
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def similarity(a, b):
    distance = levenshtein_distance(a.lower(), b.lower())
    max_length = max(len(a), len(b))
    return 1 - (distance / max_length)


def correct(subtitle_file, video_script):
    subtitle_items = file_to_subtitles(subtitle_file)
    normalized_script = utils.normalize_script_for_subtitle_matching(video_script)
    script_lines = utils.split_string_by_punctuations(normalized_script)

    corrected = False
    new_subtitle_items = []
    script_index = 0
    subtitle_index = 0

    while script_index < len(script_lines) and subtitle_index < len(subtitle_items):
        script_line = script_lines[script_index].strip()
        subtitle_line = subtitle_items[subtitle_index][2].strip()

        if script_line == subtitle_line:
            new_subtitle_items.append(subtitle_items[subtitle_index])
            script_index += 1
            subtitle_index += 1
        else:
            combined_subtitle = subtitle_line
            start_time = subtitle_items[subtitle_index][1].split(" --> ")[0]
            end_time = subtitle_items[subtitle_index][1].split(" --> ")[1]
            next_subtitle_index = subtitle_index + 1

            while next_subtitle_index < len(subtitle_items):
                next_subtitle = subtitle_items[next_subtitle_index][2].strip()
                if similarity(
                    script_line, combined_subtitle + " " + next_subtitle
                ) > similarity(script_line, combined_subtitle):
                    combined_subtitle += " " + next_subtitle
                    end_time = subtitle_items[next_subtitle_index][1].split(" --> ")[1]
                    next_subtitle_index += 1
                else:
                    break

            if similarity(script_line, combined_subtitle) > 0.8:
                logger.warning(
                    f"Merged/Corrected - Script: {script_line}, Subtitle: {combined_subtitle}"
                )
                new_subtitle_items.append(
                    (
                        len(new_subtitle_items) + 1,
                        f"{start_time} --> {end_time}",
                        script_line,
                    )
                )
                corrected = True
            else:
                logger.warning(
                    f"Mismatch - Script: {script_line}, Subtitle: {combined_subtitle}"
                )
                new_subtitle_items.append(
                    (
                        len(new_subtitle_items) + 1,
                        f"{start_time} --> {end_time}",
                        script_line,
                    )
                )
                corrected = True

            script_index += 1
            subtitle_index = next_subtitle_index

    # Process the remaining lines of the script.
    while script_index < len(script_lines):
        logger.warning(f"Extra script line: {script_lines[script_index]}")
        if subtitle_index < len(subtitle_items):
            new_subtitle_items.append(
                (
                    len(new_subtitle_items) + 1,
                    subtitle_items[subtitle_index][1],
                    script_lines[script_index],
                )
            )
            subtitle_index += 1
        else:
            new_subtitle_items.append(
                (
                    len(new_subtitle_items) + 1,
                    "00:00:00,000 --> 00:00:00,000",
                    script_lines[script_index],
                )
            )
        script_index += 1
        corrected = True

    if subtitle_index < len(subtitle_items):
        logger.warning(
            f"Dropping {len(subtitle_items) - subtitle_index} transcription cue(s) "
            "after the script ends"
        )
        corrected = True

    if corrected:
        content = "".join(
            f"{i + 1}\n{item[1]}\n{item[2]}\n\n"
            for i, item in enumerate(new_subtitle_items)
        )
        write_subtitle_file(subtitle_file, content)
        logger.info("Subtitle corrected")
    else:
        logger.success("Subtitle is correct")


if __name__ == "__main__":
    task_id = "c12fd1e6-4b0a-4d65-a075-c87abe35a072"
    task_dir = utils.task_dir(task_id)
    subtitle_file = f"{task_dir}/subtitle.srt"
    audio_file = f"{task_dir}/audio.mp3"

    subtitles = file_to_subtitles(subtitle_file)
    print(subtitles)

    script_file = f"{task_dir}/script.json"
    with open(script_file, "r") as f:
        script_content = f.read()
    s = json.loads(script_content)
    script = s.get("script")

    correct(subtitle_file, script)

    subtitle_file = f"{task_dir}/subtitle-test.srt"
    create(audio_file, subtitle_file)
