"""Confidence policy for measured Hindi CTC cues; never repairs word timings."""

import math
import unicodedata


def spoken_words(text: str) -> list[str]:
    return [
        "".join(char for char in unicodedata.normalize("NFC", token).casefold()
                if unicodedata.category(char)[0] in {"L", "M", "N"})
        for token in text.split()
        if any(unicodedata.category(char)[0] in {"L", "N"} for char in token)
    ]


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def evaluate(aligned: dict, script: str, policy: str = "balanced") -> dict:
    """Evaluate every word, preserving raw measurements and all diagnostic issues.

    Scores are CTC model scores, not probabilities of correct pronunciation.
    Balanced admits isolated words scoring at least 0.15 when their measured
    timings are valid and anchored by confident neighbours (or the audio edge),
    at most three and at most 5% of the transcript. Coverage checks are deliberately
    conservative: large gaps or sparse cues require review, even if they are silence.
    """
    if policy not in {"balanced", "strict"}:
        raise ValueError(f"unknown Hindi alignment policy: {policy}")
    limits = {
        "confidence_threshold": 0.35, "balanced_floor": 0.15,
        "max_low_confidence_words": 3, "max_low_confidence_proportion": 0.05,
        "max_consecutive_low_confidence": 1, "max_neighbour_gap_seconds": 0.75,
        "min_span_coverage": 0.80, "min_cue_coverage": 0.35,
        "max_uncovered_gap_seconds": 3.0,
    }
    expected = [word for word in script.split() if spoken_words(word)]
    words = aligned.get("words") if isinstance(aligned, dict) else None
    duration = aligned.get("audio_duration") if isinstance(aligned, dict) else None
    report = {
        "version": 1, "policy": policy, "thresholds": limits,
        "accepted": False, "audio_duration": duration, "expected_word_count": len(expected),
        "word_count": len(words) if isinstance(words, list) else 0,
        "words": words, "structural_errors": [], "confidence_errors": [],
        "warnings": [], "low_confidence_words": [], "metrics": {},
    }
    structural = report["structural_errors"]
    confidence = report["confidence_errors"]
    if not _finite(duration) or duration <= 0:
        structural.append({"code": "invalid_audio_duration", "message": "audio duration must be positive and finite"})
    if not expected or not isinstance(words, list) or len(words) != len(expected):
        structural.append({"code": "word_count", "message": "all script words must have measured cues"})
    if not isinstance(words, list):
        return report
    character_coverage = aligned.get("character_word_coverage")
    if character_coverage is not None:
        coverage_counts = {
            "aligned_character_count": sum(
                item.get("measured_character_count", 0)
                for item in character_coverage if isinstance(item, dict)
            ) if isinstance(character_coverage, list) else 0,
            "expected_character_count": sum(
                item.get("character_count", 0)
                for item in character_coverage if isinstance(item, dict)
            ) if isinstance(character_coverage, list) else 0,
        }
        report["metrics"].update(coverage_counts)
        if not isinstance(character_coverage, list) or len(character_coverage) != len(expected):
            structural.append({
                "code": "character_coverage_count",
                "message": "raw character alignment does not cover each script word",
                "character_word_count": len(character_coverage) if isinstance(character_coverage, list) else None,
            })
        else:
            for i, item in enumerate(character_coverage):
                if (not isinstance(item, dict)
                        or item.get("measured_character_count") != item.get("character_count")
                        or item.get("character_count", 0) == 0):
                    structural.append({
                        "code": "missing_character_alignment", "word_index": i + 1,
                        "expected": expected[i], "character_coverage": item,
                    })
    valid_times = []
    low_indices = []
    run = longest = 0
    for i, word in enumerate(words):
        if not isinstance(word, dict):
            structural.append({"code": "invalid_word", "word_index": i + 1})
            valid_times.append(False)
            continue
        start, end, score = (word.get(k) for k in ("start", "end", "score"))
        item = {"word_index": i + 1, **word}
        if (i >= len(expected) or not isinstance(word.get("word"), str)
                or spoken_words(word["word"]) != spoken_words(expected[i])):
            structural.append({**item, "code": "word_mismatch", "expected": expected[i] if i < len(expected) else None})
        valid = (_finite(start) and _finite(end) and _finite(duration)
                 and 0 <= start < end <= duration + 0.001)
        if not valid:
            structural.append({**item, "code": "invalid_timing"})
        if i and valid and valid_times[-1] and start < words[i - 1]["end"] - 0.000001:
            structural.append({**item, "code": "overlap", "previous_end": words[i - 1]["end"]})
        valid_times.append(valid)
        if not _finite(score) or not 0 <= score <= 1:
            confidence.append({**item, "code": "invalid_score"})
        if _finite(score) and score < limits["confidence_threshold"]:
            low_indices.append(i)
            report["low_confidence_words"].append(item)
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    proportion = len(low_indices) / len(words) if words else 0
    report["metrics"].update(low_confidence_count=len(low_indices),
                             low_confidence_proportion=proportion,
                             longest_low_confidence_run=longest)
    if words and all(valid_times) and not structural:
        gaps = [words[0]["start"], duration - words[-1]["end"]]
        gaps.extend(b["start"] - a["end"] for a, b in zip(words, words[1:]))
        span = (words[-1]["end"] - words[0]["start"]) / duration
        occupancy = sum(w["end"] - w["start"] for w in words) / duration
        report["metrics"].update(span_coverage=span, cue_coverage=occupancy,
                                 largest_uncovered_gap_seconds=max(gaps))
        if (span < limits["min_span_coverage"] or occupancy < limits["min_cue_coverage"]
                or max(gaps) > limits["max_uncovered_gap_seconds"]):
            structural.append({"code": "temporal_coverage", "message": "substantial uncovered audio requires review"})
    for i in low_indices:
        word = words[i]
        neighbour_indices = [j for j in (i - 1, i + 1) if 0 <= j < len(words)]
        neighbours_ok = bool(neighbour_indices) and all(
            isinstance(words[j], dict) and _finite(words[j].get("score"))
            and words[j]["score"] >= limits["confidence_threshold"]
            and valid_times[j] for j in (i - 1, i + 1)
            if 0 <= j < len(words)
        )
        if neighbours_ok and valid_times[i]:
            gaps = [
                word["start"] - words[j]["end"] if j < i
                else words[j]["start"] - word["end"]
                for j in neighbour_indices
            ]
            if i == 0:
                gaps.append(word["start"])
            if i == len(words) - 1:
                gaps.append(duration - word["end"])
            neighbours_ok = max(gaps) <= limits["max_neighbour_gap_seconds"]
        item = {"word_index": i + 1, **word}
        if (policy == "balanced" and word["score"] >= limits["balanced_floor"]
                and neighbours_ok and valid_times[i]):
            report["warnings"].append({**item, "code": "marginal_confidence",
                                       "message": "original CTC timing retained; pronunciation is uncertain"})
        else:
            confidence.append({**item, "code": "low_confidence_requires_review"})
    if policy == "balanced" and (
        len(low_indices) > limits["max_low_confidence_words"]
        or proportion > limits["max_low_confidence_proportion"]
        or longest > limits["max_consecutive_low_confidence"]
    ):
        confidence.append({
            "code": "aggregate_confidence",
            "message": "aggregate low-confidence limit exceeded",
            "low_confidence_count": len(low_indices),
            "max_low_confidence_words": limits["max_low_confidence_words"],
            "low_confidence_proportion": proportion,
            "max_low_confidence_proportion": limits["max_low_confidence_proportion"],
            "longest_low_confidence_run": longest,
            "max_consecutive_low_confidence": limits["max_consecutive_low_confidence"],
        })
    report["accepted"] = not structural and not confidence
    return report
