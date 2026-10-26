import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import Mock, patch

from pydantic import ValidationError

from app.models.channel_profile import ChannelProfile
from app.controllers import base
from app.controllers.v1 import channels
from app.services import channel_profiles as service


class TestChannelProfiles(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = service.ChannelProfileStore(Path(self.temp.name) / "profiles.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def profile(self, **kwargs):
        return ChannelProfile(**{"display_name": "Facts", **kwargs})

    def test_two_profiles_persist_independently_and_keep_versions(self):
        sand = self.store.create(self.profile(asmr_categories=["kinetic_sand", "soap"]))
        foam = self.store.create(self.profile(display_name="Science", asmr_categories=["foam", "jelly"]))
        updated = self.store.update(sand.channel_id, sand.model_copy(update={"display_name": "Hindi Facts"}), 1)
        reopened = service.ChannelProfileStore(self.store.database_path)
        self.assertEqual(reopened.get(sand.channel_id, 1).display_name, "Facts")
        self.assertEqual(reopened.get(sand.channel_id).display_name, "Hindi Facts")
        self.assertEqual(reopened.get(foam.channel_id).asmr_categories, ["foam", "jelly"])
        self.assertEqual(updated.version, 2)

    def test_new_profiles_default_direct_while_legacy_rows_remain_upload_post(self):
        profile = self.store.create(self.profile())
        self.assertEqual(profile.publishing_backend, "youtube_direct")
        legacy = self.profile(display_name="Legacy", publishing_backend="upload_post")
        saved = self.store.create(legacy)
        with self.store._connection() as conn:
            row = conn.execute("SELECT config_json FROM channel_profile_versions WHERE channel_id=? AND version=1",
                               (saved.channel_id,)).fetchone()
            import json
            data = json.loads(row["config_json"])
            data.pop("publishing_backend")
            conn.execute("UPDATE channel_profile_versions SET config_json=? WHERE channel_id=? AND version=1",
                         (json.dumps(data), saved.channel_id))
        self.assertEqual(self.store.get(saved.channel_id).publishing_backend, "upload_post")

    def test_validation_rejects_bad_timezone_and_category(self):
        with self.assertRaises(ValidationError):
            self.profile(timezone="Mars/Olympus")
        with self.assertRaises(ValidationError):
            self.profile(asmr_categories=["unlisted"])
        with self.assertRaises(ValidationError):
            self.profile(generation_settings={"provider_api_key": "must-not-persist"})

    def test_stale_concurrent_update_cannot_overwrite_new_version(self):
        first = self.store.create(self.profile())
        barrier = threading.Barrier(2)

        def update(name):
            barrier.wait()
            try:
                return self.store.update(first.channel_id, first.model_copy(update={"display_name": name}), 1)
            except service.ProfileVersionConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, ["One", "Two"]))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.store.get(first.channel_id).version, 2)

    def test_preview_maps_preferences_and_preserves_job_content(self):
        profile = self.profile(language="hi", stock_video_style="satisfying_asmr_cutting",
                               asmr_categories=["soap", "jelly"], target_duration_seconds=28)
        preview = service.build_preview(profile, {"video_subject": "Verified fact", "video_script": "Exact script"})
        self.assertEqual(preview.params.video_subject, "Verified fact")
        self.assertEqual(preview.params.video_script, "Exact script")
        self.assertEqual(preview.params.video_language, "hi")
        self.assertEqual(preview.params.asmr_categories, ["soap", "jelly"])
        self.assertIn("28 seconds", preview.params.video_script_prompt)

    def test_preview_pins_channel_branding_for_manual_and_scheduled_snapshots(self):
        profile = self.profile().model_copy(update={
            "intro_enabled": True,
            "intro_text": "सुन भाई, aaj ka fact will blow your mind!",
            "intro_audio": {"asset_key": "intro/v2-" + "a" * 64 + ".mp3",
                            "content_hash": "a" * 64, "duration_seconds": 1.25,
                            "version": 2},
        })
        params = service.build_preview(profile).params
        self.assertEqual(params.channel_id, profile.channel_id)
        self.assertEqual(
            params.channel_branding["intro"]["text"],
            "सुन भाई, aaj ka fact will blow your mind!",
        )
        self.assertTrue(params.channel_branding["intro"]["enabled"])
        self.assertEqual(params.channel_branding["intro"]["audio"]["version"], 2)
        self.assertFalse(params.channel_branding["outro"]["enabled"])

    def test_blank_channel_voice_uses_saved_video_generator_voice(self):
        profile = self.profile(language="hi")
        with patch.object(service.config, "ui", {"voice_name": "gemini:Puck-Upbeat"}):
            preview = service.build_preview(profile)
        self.assertEqual(preview.params.voice_name, "gemini:Puck-Upbeat")

        explicit_profile = profile.model_copy(update={"voice_name": "azure-voice-id"})
        with patch.object(service.config, "ui", {"voice_name": "gemini:Puck-Upbeat"}):
            explicit_preview = service.build_preview(explicit_profile)
        self.assertEqual(explicit_preview.params.voice_name, "azure-voice-id")

    def test_profile_routes_auth_versioning_preview_and_secret_safe_output(self):
        with patch.object(channels.profiles, "store", self.store), \
             patch.object(base.config, "app", {"api_key": "test-key", "upload_post_api_key": "provider-key"}), \
             patch.object(channels.profiles, "verify_youtube_binding", return_value=None):
            with self.assertRaises(Exception):
                base.verify_token(Mock(headers={"x-api-key": "wrong"}))
            created = channels.create_channel(ChannelProfile(display_name="API channel"))
            self.assertNotIn("provider-key", created.model_dump_json())
            preview = channels.preview_channel(channels.PreviewRequest(
                channel_id=created.channel_id, video_subject="job topic", video_script="job script"
            ))
            self.assertEqual(preview.params.video_script, "job script")
            with self.assertRaises(Exception):
                channels.update_channel(created.channel_id, channels.ChannelProfileUpdate(
                    expected_version=0, profile=created
                ))

    def test_direct_youtube_binding_checks_local_profile_and_verified_channel_id(self):
        from unittest.mock import Mock
        from cryptography.fernet import Fernet
        from app.services.youtube_direct import OAUTH_SCOPES, YouTubeConnectionStore
        key = Fernet.generate_key().decode()
        with patch.dict("os.environ", {"MPT_YOUTUBE_TOKEN_ENCRYPTION_KEY": key}):
            connections = YouTubeConnectionStore(self.store.database_path)
            connection_id = connections.save_connection(
                "local-a", {"id": "UC-A", "snippet": {"title": "A"}}, "refresh", list(OAUTH_SCOPES))
            with patch.object(service, "store", Mock(database_path=self.store.database_path)):
                service.verify_publisher_binding(self.profile(
                    channel_id="local-a", publishing_backend="youtube_direct",
                    youtube_connection_id=connection_id, youtube_channel_id="UC-A"))
                with self.assertRaisesRegex(service.UploadProfileError, "does not match"):
                    service.verify_publisher_binding(self.profile(
                        channel_id="local-b", publishing_backend="youtube_direct",
                        youtube_connection_id=connection_id, youtube_channel_id="UC-A"))
                with self.assertRaisesRegex(service.UploadProfileError, "does not match"):
                    service.verify_publisher_binding(self.profile(
                        channel_id="local-a", publishing_backend="youtube_direct",
                        youtube_connection_id=connection_id, youtube_channel_id="UC-B"))

    @patch.object(service, "_provider_profiles")
    def test_upload_profile_isolation_and_youtube_verification(self, profiles):
        profiles.return_value = [
            {"username": "account-a", "social_accounts": {"youtube": {"username": "UC-A", "handle": "@a"}}},
            {"username": "account-b", "social_accounts": {"instagram": {"username": "b"}}},
        ]
        self.assertEqual(service.verify_youtube_binding("account-a")["youtube_handle"], "@a")
        with self.assertRaises(service.UploadProfileError):
            service.verify_youtube_binding("account-b")
        with self.assertRaises(service.UploadProfileError):
            service.verify_youtube_binding("account-c")

    @patch.object(service.requests, "get")
    def test_provider_response_sanitization_and_reauthorization(self, get):
        response = Mock()
        response.json.return_value = [{"username": "acct", "api_key": "secret", "social_accounts": {
            "youtube": {"username": "UC", "reauth_required": True, "access_token": "secret"}}}]
        response.raise_for_status.return_value = None
        get.return_value = response
        with patch.object(service.config, "app", {"upload_post_api_key": "fake-secret"}):
            public = service.upload_post_profiles()
            self.assertNotIn("access_token", str(public))
            with self.assertRaisesRegex(service.UploadProfileError, "reauthorization"):
                service.verify_youtube_binding("acct")


if __name__ == "__main__":
    unittest.main()
