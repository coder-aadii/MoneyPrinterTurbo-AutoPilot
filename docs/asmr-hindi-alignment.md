# ASMR footage and Hindi word captions

## Reviewed ASMR footage

An ASMR job searches only its selected cutting categories. Pexels and Pixabay
results are ranked by cutting-related metadata; narration keywords are never
used. A shortfall fails the job before rendering. One long source can contribute
several non-overlapping sections in random concat mode; sequential mode uses
one section per source. A clip is never looped to fill a shortage.

For footage you have visually checked and are licensed to use, create
`storage/asmr_library/manifest.json` and put the MP4s beside it. Example:

```json
{
  "clips": [
    {
      "file": "sand-cutting.mp4",
      "sha256": "<64-character lowercase SHA-256 of the MP4>",
      "category": "kinetic_sand",
      "license": "Pixabay Content License",
      "source_url": "https://pixabay.com/videos/.../",
      "reviewed_cutting": true
    }
  ]
}
```

Use `sha256sum storage/asmr_library/sand-cutting.mp4` to obtain the hash.
`reviewed_cutting=true` means the **whole file**, including its beginning and
end, was checked for the selected cutting category. Trim introductions,
non-cutting sections, and outros into separate MP4 files first. The file may
be an original clip (`"license": "self-owned"`) or a permitted stock clip
with a public source URL. The loader verifies the bytes, video dimensions,
duration, and the hash again before rendering. It de-duplicates identical
bytes. If you use multiple trims from one stock source URL, provide
`source_start_seconds` and `source_end_seconds` for **each** trim, measured on
the original source timeline. For self-owned clips, also set the same
`source_id` on all trims of one original. Overlapping trims are rejected, and
each range must match the resulting MP4 duration. Never register overlapping
or independently re-encoded copies as distinct footage.

Select `video_source = "local"` to use only this library. With Pexels or
Pixabay selected, approved local clips are considered first and relevant stock
results can fill any remaining duration. Do not add a category solely to fill
time. A Coverr API key does not imply that Coverr has suitable cutting clips;
the ASMR path currently does not accept Coverr candidates.

## Hindi word alignment for Gemini narration

Gemini supplies audio without word boundaries. For `video_language = "hi"`
and `subtitle_display_mode = "word_by_word"`, the app uses a separate local
WhisperX process to force-align the exact saved Hindi script to the generated
audio. The default model is
[`theainerd/Wav2Vec2-large-xlsr-hindi`](https://huggingface.co/theainerd/Wav2Vec2-large-xlsr-hindi).
Its model card reports about 300 million parameters and relatively high word
error rate; verify cues against the audio before publishing. Low-confidence,
missing, overlapping, or changed words make the job fail rather than inventing
timings. Sentence mode and other languages continue to use their existing
subtitle paths.

Install the optional worker outside the locked application environment:

```bash
uv venv --python .venv/bin/python /path/to/hindi-align-venv
uv pip install --python /path/to/hindi-align-venv/bin/python 'whisperx==3.8.6'
/path/to/hindi-align-venv/bin/python -m nltk.downloader punkt_tab
/path/to/hindi-align-venv/bin/python -c 'from huggingface_hub import snapshot_download; snapshot_download("theainerd/Wav2Vec2-large-xlsr-hindi")'
```

Set `MPT_HINDI_ALIGNMENT_PYTHON=/path/to/hindi-align-venv/bin/python` in
the server environment and restart it. Model loading is cache-only during a
job; the one-time model download must finish beforehand. WhisperX brings
PyTorch, Transformers, audio libraries, and the Hindi model, so allow several
gigabytes of disk and RAM and expect CPU alignment to take minutes. FFmpeg is
also required to decode the narration. The app keeps `faster-whisper` in its
own locked environment; no project dependency changes are needed.

For `Harveenchadha/vakyansh-wav2vec2-hindi-him-4200`, set
`MPT_HINDI_ALIGNMENT_BLANK_ID=0`. That checkpoint emits CTC blank on token ID
0 even though its Transformers config names token ID 1 (`<pad>`) as padding;
the worker applies this override only when explicitly configured. Do not use
the override with another checkpoint unless its CTC blank ID has been verified.

You can select another locally cached Hindi Wav2Vec2 CTC model with
`MPT_HINDI_ALIGNMENT_MODEL=<Hugging Face model ID>`. Its vocabulary must
represent every letter and combining mark in the script. Confirm the model's
license and test its timing on your narration before relying on it.

If the worker or model is missing, word mode fails explicitly. It will not
substitute estimated Edge sentence timings or a guessed Whisper transcript.
