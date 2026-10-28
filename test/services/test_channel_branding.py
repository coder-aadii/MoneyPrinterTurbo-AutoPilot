import tempfile
import unittest
import wave
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

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

    def test_profile_image_is_normalized_and_bound_to_a_channel_hash(self):
        image_bytes = BytesIO()
        Image.new("RGB", (40, 24), "navy").save(image_bytes, format="JPEG")
        def storage_dir(name, create=False):
            return str(Path(temporary) / name)
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            channel_branding.utils, "storage_dir", side_effect=storage_dir,
        ):
            saved = channel_branding.save_profile_image(
                "channel-a", image_bytes.getvalue(), version=1,
            )
            resolved = channel_branding.resolve_profile_image(
                "channel-a", saved["asset_key"],
            )
            self.assertEqual(resolved.suffix, ".png")
            self.assertEqual(channel_branding.hashlib.sha256(resolved.read_bytes()).hexdigest(),
                             saved["content_hash"])
            with self.assertRaises(FileNotFoundError):
                channel_branding.resolve_profile_image("channel-b", saved["asset_key"])

    def test_saved_audio_and_timed_subtitles_render_a_hash_bound_local_video(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            channel_branding.utils, "storage_dir",
            side_effect=lambda name, create=False: str(Path(temporary) / name),
        ):
            audio_path = Path(temporary) / "intro.wav"
            with wave.open(str(audio_path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(b"\0\0" * 24000)
            saved_audio = channel_branding.save_asset(
                "channel-video", "outro", audio_path.read_bytes(), extension="wav",
                version=1, cues=b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
            )
            profile_bytes = BytesIO()
            Image.new("RGB", (80, 80), "purple").save(profile_bytes, format="PNG")
            profile_image = channel_branding.save_profile_image(
                "channel-video", profile_bytes.getvalue(), version=1,
            )
            output_path = Path(temporary) / "preview.mp4"
            saved_video = channel_branding.generate_video(
                "channel-video", "outro", saved_audio, display_name="Test Channel",
                aspect="9:16", profile_image=profile_image,
                output_path=str(output_path), version=1,
            )
            resolved = channel_branding.resolve_video(
                "channel-video", saved_video["asset_key"], expected_kind="outro",
            )
            self.assertEqual(saved_video["audio_hash"], saved_audio["content_hash"])
            self.assertTrue(saved_video["subtitle_hash"])
            self.assertEqual(saved_video["avatar_hash"], profile_image["content_hash"])
            self.assertEqual(saved_video["aspect"], "9:16")
            self.assertEqual(saved_video["layout_version"], 2)
            self.assertEqual(channel_branding.hashlib.sha256(resolved.read_bytes()).hexdigest(),
                             saved_video["content_hash"])
            from moviepy import VideoFileClip
            with VideoFileClip(str(resolved), audio=False) as preview:
                self.assertEqual(tuple(preview.size), (1080, 1920))
                frame = preview.get_frame(0.2)
                self.assertGreater(frame[585, 540, 0], 80)  # profile image occupies top visual panel
                self.assertGreater((frame[1055:1840, 260:820].max(axis=2) < 80).sum(), 1000)

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
