import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from app.services import channel_branding


class ChannelBrandingTests(unittest.TestCase):
    def test_assets_are_immutable_channel_scoped_references(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            with patch.object(channel_branding.utils, "storage_dir",
                              side_effect=lambda name, create=False: str(storage / name)):
                with patch.object(channel_branding.voice, "get_audio_duration", return_value=2.5):
                    saved = channel_branding.save_asset(
                        "channel-1", "intro", b"test audio", extension="mp3", version=1,
                        provider="gemini-tts", model="test-model", voice_name="Puck",
                    )
                self.assertEqual(saved["duration_seconds"], 2.5)
                self.assertEqual(saved["content_hash"], channel_branding.hashlib.sha256(
                    b"test audio").hexdigest())
                resolved = channel_branding.resolve_asset(
                    "channel-1", saved["asset_key"], expected_kind="intro"
                )
                self.assertEqual(resolved.read_bytes(), b"test audio")
                with self.assertRaises(FileNotFoundError):
                    channel_branding.resolve_asset(
                        "channel-2", saved["asset_key"], expected_kind="intro"
                    )

    def test_srt_offsets_use_actual_segment_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "caption.srt"
            source.write_text(
                "1\n00:00:00,250 --> 00:00:01,000\nनमस्ते\n", encoding="utf-8"
            )
            with patch.object(channel_branding.utils, "storage_dir", return_value=temporary):
                result = channel_branding.offset_srt(str(source), 1.375)
            self.assertIn("00:00:01,625 --> 00:00:02,375", result)

    def test_local_audio_assembly_preserves_both_segment_durations(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            inputs = []
            for index in range(2):
                source = directory / f"segment-{index}.wav"
                with wave.open(str(source), "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(24000)
                    audio.writeframes(b"\0\0" * 12000)
                inputs.append(str(source))
            output = directory / "assembled.mp3"
            self.assertTrue(channel_branding.voice._concat_audio_files(inputs, str(output)))
            duration = channel_branding.voice.get_audio_duration(str(output))
            self.assertGreaterEqual(duration, 0.95)
            self.assertLessEqual(duration, 1.15)


if __name__ == "__main__":
    unittest.main()
