import os
import io
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from cryptography.fernet import Fernet
from loguru import logger
import requests

from app.services import youtube_direct


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class FakeOAuthHTTP:
    def __init__(self):
        self.counter = 0

    def post(self, url, **kwargs):
        self.counter += 1
        return FakeResponse(payload={
            "access_token": f"access-{self.counter}",
            "refresh_token": f"refresh-secret-{self.counter}",
            "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly",
            "expires_in": 3600,
            "token_type": "Bearer",
        })

    def get(self, url, **kwargs):
        token = kwargs["headers"]["Authorization"].split()[-1]
        channel = token.split("-")[-1]
        return FakeResponse(payload={"items": [{"id": f"UC-{channel}", "snippet": {"title": f"Channel {channel}"}}]})


class TestYouTubeDirect(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = Path(self.temp.name) / "autopilot.sqlite3"
        self.key = Fernet.generate_key().decode()
        self.env = patch.dict(os.environ, {"MPT_YOUTUBE_TOKEN_ENCRYPTION_KEY": self.key})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temp.cleanup)

    def _bound_context(self, store, video, *, channel="UC-A", local="local", job="job"):
        connection_id = store.save_connection(local, {"id": channel}, "refresh-token", list(youtube_direct.OAUTH_SCOPES))
        context = youtube_direct._session_binding(
            job_id=job, local_channel_id=local, connection_id=connection_id,
            youtube_channel_id=channel, artifact_sha256=youtube_direct._artifact_sha256(video))
        return connection_id, context

    def test_oauth_connections_bind_independent_channels_and_encrypt_tokens(self):
        store = youtube_direct.YouTubeConnectionStore(self.database)
        http = FakeOAuthHTTP()
        publisher = youtube_direct.YouTubeDirectPublisher(store, http)
        with patch.object(youtube_direct, "_oauth_config", return_value=("client", "secret", "https://mpt.test/callback")):
            saved = []
            for local_id in ("channel-a", "channel-b"):
                state, url = store.begin_oauth(local_id)
                self.assertIn("access_type=offline", url)
                scopes = parse_qs(urlparse(url).query)["scope"][0].split()
                self.assertEqual(scopes, list(youtube_direct.OAUTH_SCOPES))
                saved.append(publisher.oauth_callback(state, "fake-code"))
        self.assertEqual([item["youtube_channel_id"] for item in saved], ["UC-1", "UC-2"])
        first = store.get_connection(saved[0]["connection_id"])
        second = store.get_connection(saved[1]["connection_id"])
        self.assertEqual(first["local_channel_id"], "channel-a")
        self.assertEqual(second["local_channel_id"], "channel-b")
        raw_db = self.database.read_bytes()
        self.assertNotIn(b"refresh-secret-1", raw_db)
        self.assertNotIn(b"refresh-secret-2", raw_db)
        # Reauthorization for one profile keeps the old immutable connection ID,
        # allowing already-created jobs to retain their pinned destination.
        old_connection = saved[0]["connection_id"]
        with patch.object(youtube_direct, "_oauth_config", return_value=("client", "secret", "https://mpt.test/callback")):
            state, _ = store.begin_oauth("channel-a")
            renewed = publisher.oauth_callback(state, "fake-code")
        self.assertNotEqual(old_connection, renewed["connection_id"])
        self.assertEqual(store.get_connection(old_connection)["youtube_channel_id"], "UC-1")
        self.assertEqual(store.connection_for_channel("channel-a")["connection_id"], renewed["connection_id"])

    def test_oauth_rejects_multiple_owned_channels_without_saving_any_identity(self):
        store = youtube_direct.YouTubeConnectionStore(self.database)

        class MultiChannelHTTP(FakeOAuthHTTP):
            def get(self, url, **kwargs):
                return FakeResponse(payload={"items": [
                    {"id": "UC-PERSONAL", "snippet": {"title": "Personal"}},
                    {"id": "UC-BRAND", "snippet": {"title": "Brand channel"}},
                ]})

        publisher = youtube_direct.YouTubeDirectPublisher(store, MultiChannelHTTP())
        with patch.object(youtube_direct, "_oauth_config", return_value=("client", "secret", "https://mpt.test/callback")):
            state, _ = store.begin_oauth("local-channel")
            with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "Set the intended channel as your YouTube default"):
                publisher.oauth_callback(state, "fake-code")
        self.assertIsNone(store.connection_for_channel("local-channel"))
        with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "invalid or expired"):
            store.consume_oauth_state(state)

    def test_oauth_reports_empty_channel_lookup_separately(self):
        store = youtube_direct.YouTubeConnectionStore(self.database)

        class EmptyHTTP(FakeOAuthHTTP):
            def get(self, url, **kwargs):
                return FakeResponse(payload={"items": []})

        publisher = youtube_direct.YouTubeDirectPublisher(store, EmptyHTTP())
        with patch.object(youtube_direct, "_oauth_config", return_value=("client", "secret", "https://mpt.test/callback")):
            state, _ = store.begin_oauth("local-channel")
            with self.assertRaisesRegex(youtube_direct.YouTubePublisherError,
                                        "No YouTube channel found for this authorized Google identity"):
                publisher.oauth_callback(state, "fake-code")
        self.assertIsNone(store.connection_for_channel("local-channel"))

    def test_oauth_collapses_duplicate_channel_resources_by_id(self):
        store = youtube_direct.YouTubeConnectionStore(self.database)

        class DuplicateHTTP(FakeOAuthHTTP):
            def get(self, url, **kwargs):
                self.channel_request = (url, kwargs["params"])
                return FakeResponse(payload={"items": [
                    {"id": "UC-ONE", "snippet": {"title": "Facts Sun Le Bhai"}},
                    {"id": "UC-ONE", "snippet": {"title": "Facts Sun Le Bhai"}},
                ]})

        http = DuplicateHTTP()
        publisher = youtube_direct.YouTubeDirectPublisher(store, http)
        with patch.object(youtube_direct, "_oauth_config", return_value=("client", "secret", "https://mpt.test/callback")):
            state, _ = store.begin_oauth("local-channel")
            result = publisher.oauth_callback(state, "fake-code")
        self.assertEqual(result["youtube_channel_id"], "UC-ONE")
        self.assertEqual(http.channel_request[1], {"part": "id,snippet", "mine": "true", "maxResults": 50})

    def test_channel_diagnostic_logs_metadata_without_credentials(self):
        output = io.StringIO()
        sink_id = logger.add(output, format="{message}")
        self.addCleanup(logger.remove, sink_id)

        class DiagnosticHTTP:
            def get(self, url, **kwargs):
                return FakeResponse(payload={"items": [
                    {"id": "UC-SAFE", "snippet": {"title": "Facts Sun Le Bhai"}},
                    {"id": "UC-SECOND", "snippet": {"title": "Second channel"}},
                ]})

        publisher = youtube_direct.YouTubeDirectPublisher(
            youtube_direct.YouTubeConnectionStore(self.database), DiagnosticHTTP())
        publisher._list_channels("access-token-must-not-appear")
        diagnostic = output.getvalue()
        self.assertIn('item_count=2', diagnostic)
        self.assertIn("status=200", diagnostic)
        self.assertIn("top_level_keys=['items']", diagnostic)
        self.assertIn("page_info=None", diagnostic)
        self.assertNotIn("access-token-must-not-appear", diagnostic)
        self.assertNotIn("UC-SAFE", diagnostic)
        self.assertNotIn("Facts Sun Le Bhai", diagnostic)

    def test_wrong_fernet_key_and_corrupt_ciphertext_fail_closed(self):
        encrypted = youtube_direct.YouTubeConnectionStore.encrypt_secret("private-value")
        url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=x"
        binding = {"job_id": "j", "local_channel_id": "l", "connection_id": "c",
                   "youtube_channel_id": "UC-A", "artifact_sha256": "hash"}
        encrypted_session = youtube_direct.YouTubeConnectionStore.encrypt_session(url, binding)
        wrong_key = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"MPT_YOUTUBE_TOKEN_ENCRYPTION_KEY": wrong_key}):
            with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "cannot be authenticated"):
                youtube_direct.YouTubeConnectionStore.decrypt_secret(encrypted)
            with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "cannot be authenticated"):
                youtube_direct.YouTubeConnectionStore.decode_session(encrypted_session, binding)
        corrupted = bytearray(encrypted)
        corrupted[12] = ord("A") if corrupted[12] != ord("A") else ord("B")
        with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "cannot be authenticated"):
            youtube_direct.YouTubeConnectionStore.decrypt_secret(bytes(corrupted))

    def test_legacy_plaintext_session_is_explicitly_unverified_and_versioned(self):
        store = youtube_direct.YouTubeConnectionStore(self.database)
        url = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=legacy"
        binding = {"job_id": "j", "local_channel_id": "l", "connection_id": "c",
                   "youtube_channel_id": "UC-A", "artifact_sha256": "hash"}
        decoded, legacy, unverified = store.decode_session(url, binding)
        self.assertEqual(decoded, url)
        self.assertTrue(legacy)
        self.assertTrue(unverified)
        migrated = store.encrypt_session(decoded, binding, legacy_unverified=True)
        decoded, legacy, unverified = store.decode_session(migrated, binding)
        self.assertEqual(decoded, url)
        self.assertFalse(legacy)
        self.assertTrue(unverified)
        with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "binding or version"):
            store.decode_session(migrated, {**binding, "job_id": "another-job"})

    def test_untrusted_session_origin_and_artifact_mismatch_reject_before_network(self):
        video = Path(self.temp.name) / "short.mp4"
        video.write_bytes(b"abcdefgh")
        store = youtube_direct.YouTubeConnectionStore(self.database)
        _, context = self._bound_context(store, video)
        context["artifact_sha256"] = "wrong-hash"
        publisher = youtube_direct.YouTubeDirectPublisher(store, object())
        with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "untrusted origin"):
            publisher.resume("https://evil.example/upload?uploadType=resumable&upload_id=x", str(video),
                             "token", "UC-A", session_context=context)
        with patch.object(youtube_direct, "_validate_mp4", return_value=8):
            with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "artifact hash"):
                publisher.resume("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=x",
                                 str(video), "token", "UC-A", session_context=context)

    def test_oauth_missing_and_refresh_changed_scopes_require_reauthorization(self):
        store = youtube_direct.YouTubeConnectionStore(self.database)

        class MissingScopeHTTP(FakeOAuthHTTP):
            def post(self, url, **kwargs):
                return FakeResponse(payload={"access_token": "a", "refresh_token": "r", "scope": "openid",
                                             "expires_in": 3600, "token_type": "Bearer"})

        publisher = youtube_direct.YouTubeDirectPublisher(store, MissingScopeHTTP())
        with patch.object(youtube_direct, "_oauth_config", return_value=("client", "secret", "https://mpt.test/callback")):
            state, _ = store.begin_oauth("local")
            with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "missing required YouTube scopes"):
                publisher.oauth_callback(state, "code")
        self.assertIsNone(store.connection_for_channel("local"))

        connection_id = store.save_connection("local", {"id": "UC-A"}, "refresh", list(youtube_direct.OAUTH_SCOPES))

        class ChangedScopeHTTP:
            def post(self, url, **kwargs):
                return FakeResponse(payload={"access_token": "new", "scope": youtube_direct.OAUTH_SCOPES[0],
                                             "expires_in": 3600, "token_type": "Bearer"})

        changed = youtube_direct.YouTubeDirectPublisher(store, ChangedScopeHTTP())
        with patch.object(youtube_direct, "_oauth_config", return_value=("client", "secret", "https://mpt.test/callback")):
            with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "missing required YouTube scopes") as caught:
                changed.access_token(connection_id)
        self.assertTrue(caught.exception.reauthorization_required)

    def test_channels_list_rejects_malformed_shape_and_exhausts_pagination(self):
        class Pages:
            def __init__(self):
                self.params = []

            def get(self, url, **kwargs):
                self.params.append(kwargs["params"])
                if len(self.params) == 1:
                    return FakeResponse(payload={"items": [{"id": "UC-A", "snippet": {"title": "A"}}],
                                                  "nextPageToken": "page-2"})
                return FakeResponse(payload={"items": [{"id": "UC-B", "snippet": {"title": "B"}}]})

        pages = Pages()
        publisher = youtube_direct.YouTubeDirectPublisher(youtube_direct.YouTubeConnectionStore(self.database), pages)
        self.assertEqual([item["id"] for item in publisher._list_channels("secret")], ["UC-A", "UC-B"])
        self.assertEqual(pages.params[1]["pageToken"], "page-2")

        class Malformed:
            def get(self, url, **kwargs):
                return FakeResponse(payload={"items": {"not": "a list"}})

        publisher = youtube_direct.YouTubeDirectPublisher(youtube_direct.YouTubeConnectionStore(self.database), Malformed())
        with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "malformed channel list"):
            publisher._list_channels("secret")

        class Truncated:
            def get(self, url, **kwargs):
                return FakeResponse(payload={"items": [{"id": "UC-A", "snippet": {"title": "A"}}],
                                             "pageInfo": {"totalResults": 2}})

        publisher = youtube_direct.YouTubeDirectPublisher(youtube_direct.YouTubeConnectionStore(self.database), Truncated())
        with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "omitted a required channel page"):
            publisher._list_channels("secret")

    def test_channels_list_normalizes_google_empty_response_and_oauth_reports_no_channel(self):
        class EmptyChannels:
            def get(self, url, **kwargs):
                return FakeResponse(payload={"kind": "youtube#channelListResponse",
                                             "pageInfo": {"totalResults": 0, "resultsPerPage": 0}})

        publisher = youtube_direct.YouTubeDirectPublisher(
            youtube_direct.YouTubeConnectionStore(self.database), EmptyChannels())
        self.assertEqual(publisher._list_channels("must-not-be-logged"), [])

        class EmptyOAuthHTTP(FakeOAuthHTTP):
            def get(self, url, **kwargs):
                return FakeResponse(payload={"pageInfo": {"totalResults": 0}})

        publisher = youtube_direct.YouTubeDirectPublisher(
            youtube_direct.YouTubeConnectionStore(self.database), EmptyOAuthHTTP())
        with patch.object(youtube_direct, "_oauth_config",
                          return_value=("client", "secret", "https://mpt.test/callback")):
            state, _ = publisher.store.begin_oauth("local-channel")
            with self.assertRaisesRegex(youtube_direct.YouTubePublisherError,
                                        "No YouTube channel found for this authorized Google identity"):
                publisher.oauth_callback(state, "fake-code")

    def test_resumable_connection_error_and_503_reconcile_before_resending(self):
        video = Path(self.temp.name) / "short.mp4"
        video.write_bytes(b"abcdefgh")
        store = youtube_direct.YouTubeConnectionStore(self.database)
        _, context = self._bound_context(store, video)

        class RecoveryHTTP:
            def __init__(self):
                self.responses = [FakeResponse(308), requests.ConnectionError("lost response"),
                                  FakeResponse(308, headers={"Range": "bytes=0-3"}),
                                  FakeResponse(201, {"id": "v", "snippet": {"channelId": "UC-A"}})]
                self.calls = []

            def put(self, url, **kwargs):
                self.calls.append(kwargs["headers"]["Content-Range"])
                next_response = self.responses.pop(0)
                if isinstance(next_response, Exception):
                    raise next_response
                return next_response

        http = RecoveryHTTP()
        publisher = youtube_direct.YouTubeDirectPublisher(store, http)
        with patch.object(youtube_direct, "_validate_mp4", return_value=8):
            result = publisher.resume("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=x",
                                      str(video), "token", "UC-A", session_context=context)
        self.assertEqual(result["id"], "v")
        self.assertEqual(http.calls[1], "bytes 0-7/8")
        self.assertEqual(http.calls[3], "bytes 4-7/8")

    def test_expired_session_is_uncertain_and_never_restarted_here(self):
        video = Path(self.temp.name) / "short.mp4"
        video.write_bytes(b"abcdefgh")
        store = youtube_direct.YouTubeConnectionStore(self.database)
        _, context = self._bound_context(store, video)

        class ExpiredHTTP:
            def put(self, url, **kwargs):
                return FakeResponse(404)

        publisher = youtube_direct.YouTubeDirectPublisher(store, ExpiredHTTP())
        with patch.object(youtube_direct, "_validate_mp4", return_value=8):
            with self.assertRaises(youtube_direct.YouTubePublisherError) as caught:
                publisher.resume("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=x",
                    str(video), "token", "UC-A", session_context=context)
        self.assertTrue(caught.exception.session_expired)
        self.assertFalse(caught.exception.confirmed)

    def test_no_progress_responses_are_bounded(self):
        video = Path(self.temp.name) / "short.mp4"
        video.write_bytes(b"abcdefgh")
        store = youtube_direct.YouTubeConnectionStore(self.database)
        _, context = self._bound_context(store, video)

        class NoProgressHTTP:
            calls = 0

            def put(self, url, **kwargs):
                self.calls += 1
                return FakeResponse(308)

        http = NoProgressHTTP()
        publisher = youtube_direct.YouTubeDirectPublisher(store, http)
        with patch.object(youtube_direct, "_validate_mp4", return_value=8):
            with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "no confirmed progress"):
                publisher.resume("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=x",
                                 str(video), "access", "UC-A", session_context=context)
        self.assertEqual(http.calls, 6)

    def test_http_5xx_and_expired_access_token_reconcile_before_more_bytes(self):
        video = Path(self.temp.name) / "short.mp4"
        video.write_bytes(b"abcdefgh")
        store = youtube_direct.YouTubeConnectionStore(self.database)
        _, context = self._bound_context(store, video)

        for status in (500, 502, 503, 504):
            class ServerErrorHTTP:
                def __init__(self):
                    self.responses = [FakeResponse(308), FakeResponse(status),
                                      FakeResponse(308, headers={"Range": "bytes=0-1"}),
                                      FakeResponse(201, {"id": "v", "snippet": {"channelId": "UC-A"}})]

                def put(self, url, **kwargs):
                    return self.responses.pop(0)

            publisher = youtube_direct.YouTubeDirectPublisher(store, ServerErrorHTTP())
            with patch.object(youtube_direct, "_validate_mp4", return_value=8):
                result = publisher.resume("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=x",
                                          str(video), "token", "UC-A", session_context=context)
            self.assertEqual(result["id"], "v")

        class ExpiredAccessHTTP:
            def __init__(self):
                self.calls = []
                self.responses = [FakeResponse(308), FakeResponse(401), FakeResponse(308),
                                  FakeResponse(201, {"id": "v2", "snippet": {"channelId": "UC-A"}})]

            def put(self, url, **kwargs):
                self.calls.append((kwargs["headers"]["Authorization"], kwargs["headers"]["Content-Range"]))
                return self.responses.pop(0)

        http = ExpiredAccessHTTP()
        publisher = youtube_direct.YouTubeDirectPublisher(store, http)
        with patch.object(youtube_direct, "_validate_mp4", return_value=8), \
             patch.object(publisher, "access_token", return_value=({"local_channel_id": "local", "youtube_channel_id": "UC-A"}, "fresh")) as refresh:
            result = publisher.resume("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=x",
                                      str(video), "expired", "UC-A", session_context=context)
        self.assertEqual(result["id"], "v2")
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(http.calls[2][0], "Bearer fresh")
        self.assertEqual(http.calls[3][0], "Bearer fresh")

    def test_quota_uses_pacific_date_rejects_bad_limit_and_is_atomic(self):
        store = youtube_direct.YouTubeConnectionStore(self.database)
        real_datetime = youtube_direct.datetime

        class FrozenDateTime(real_datetime):
            current = real_datetime(2026, 1, 2, 7, 30, tzinfo=timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz) if tz else cls.current

        with patch.object(youtube_direct, "datetime", FrozenDateTime):
            store.consume_insert_quota(1)
            FrozenDateTime.current = real_datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc)
            store.consume_insert_quota(1)
            with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "positive integer"):
                store.consume_insert_quota(0)
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: self._quota_attempt(store), range(8)))
        self.assertEqual(sum(outcomes), 3)

    @staticmethod
    def _quota_attempt(store):
        try:
            store.consume_insert_quota(3)
            return True
        except youtube_direct.YouTubePublisherError:
            return False

    def test_invalid_privacy_and_publish_timestamp_fail_preflight(self):
        with patch.object(youtube_direct, "_validate_mp4", return_value=5):
            with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "privacy"):
                youtube_direct.YouTubeDirectPublisher._validate_publish_inputs(
                    "f.mp4", "title", "", [], "friends", False, False, None)
            with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "future timezone-aware"):
                youtube_direct.YouTubeDirectPublisher._validate_publish_inputs(
                    "f.mp4", "title", "", [], "private", False, False, "2020-01-01T00:00:00Z")

    def test_mp4_must_be_readable_by_ffprobe(self):
        video = Path(self.temp.name) / "probe.mp4"
        video.write_bytes(b"container")
        good = type("Probe", (), {"returncode": 0, "stdout": "mov,mp4,m4a,3gp,3g2,mj2", "stderr": ""})()
        with patch.object(youtube_direct.subprocess, "run", return_value=good):
            self.assertEqual(youtube_direct._validate_mp4(video), len(b"container"))
        bad = type("Probe", (), {"returncode": 1, "stdout": "", "stderr": "invalid data"})()
        with patch.object(youtube_direct.subprocess, "run", return_value=bad):
            with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "readable MP4"):
                youtube_direct._validate_mp4(video)

    def test_brand_account_multi_channel_grant_fails_closed(self):
        # Brand Account access is represented by the same API-visible channel list;
        # an ID supplied by the UI cannot prove which identity videos.insert will use.
        store = youtube_direct.YouTubeConnectionStore(self.database)

        class BrandAccountHTTP(FakeOAuthHTTP):
            def get(self, url, **kwargs):
                return FakeResponse(payload={"items": [
                    {"id": "UC-FACTS", "snippet": {"title": "Facts Sun Le Bhai"}},
                    {"id": "UC-OTHER-BRAND", "snippet": {"title": "Other Brand"}},
                ]})

        publisher = youtube_direct.YouTubeDirectPublisher(store, BrandAccountHTTP())
        with patch.object(youtube_direct, "_oauth_config", return_value=("client", "secret", "https://mpt.test/callback")):
            state, _ = store.begin_oauth("facts-channel")
            with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "cannot safely bind an upload destination"):
                publisher.oauth_callback(state, "fake-code")
        self.assertIsNone(store.connection_for_channel("facts-channel"))

    def test_resumable_upload_reconciles_offset_and_validates_returned_channel(self):
        store = youtube_direct.YouTubeConnectionStore(self.database)

        class ResumeHTTP:
            def __init__(self):
                self.calls = 0

            def put(self, url, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse(308, headers={"Range": "bytes=0-2"})
                return FakeResponse(201, {"id": "video-123", "snippet": {"channelId": "UC-A"}, "status": {"privacyStatus": "private"}})

        video = Path(self.temp.name) / "short.mp4"
        video.write_bytes(b"abcdefgh")
        publisher = youtube_direct.YouTubeDirectPublisher(store, ResumeHTTP())
        _, context = self._bound_context(store, video, local="local-a", job="job-a")
        with patch.object(youtube_direct, "_validate_mp4", return_value=8):
            result = publisher.resume("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=fake",
                                      str(video), "access", "UC-A", session_context=context)
        self.assertEqual(result["id"], "video-123")
        self.assertEqual(publisher.http.calls, 2)
        with self.assertRaises(youtube_direct.YouTubePublisherError):
            publisher._validate_receipt({"id": "video-123", "snippet": {"channelId": "UC-B"}}, "UC-A")

    def test_local_daily_insert_limit_fails_closed(self):
        store = youtube_direct.YouTubeConnectionStore(self.database)
        store.consume_insert_quota(daily_limit=1)
        with self.assertRaisesRegex(youtube_direct.YouTubePublisherError, "daily safety limit"):
            store.consume_insert_quota(daily_limit=1)

    def test_rate_limit_error_keeps_retry_after_without_persisting_response_body(self):
        with self.assertRaises(youtube_direct.YouTubePublisherError) as caught:
            youtube_direct.YouTubeDirectPublisher._raise_http(
                FakeResponse(429, headers={"Retry-After": "45"}, text="sensitive response body"),
                "video upload")
        self.assertTrue(caught.exception.confirmed)
        self.assertEqual(caught.exception.retry_after, 45)
        self.assertNotIn("sensitive response body", str(caught.exception))

    def test_migrates_legacy_single_connection_schema_without_losing_pinned_token(self):
        token = Fernet(self.key.encode()).encrypt(b"old-refresh")
        conn = sqlite3.connect(self.database)
        conn.execute("CREATE TABLE youtube_connections (connection_id TEXT PRIMARY KEY, local_channel_id TEXT NOT NULL, youtube_channel_id TEXT NOT NULL, channel_title TEXT NOT NULL, encrypted_refresh_token BLOB NOT NULL, scopes_json TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(local_channel_id))")
        conn.execute("INSERT INTO youtube_connections VALUES ('old-id','local-a','UC-A','A',?,'[]','connected','2026-01-01','2026-01-01')", (token,))
        conn.commit()
        conn.close()
        store = youtube_direct.YouTubeConnectionStore(self.database)
        self.assertEqual(store.get_connection("old-id")["refresh_token"], "old-refresh")
        second_id = store.save_connection("local-a", {"id": "UC-B", "snippet": {"title": "B"}},
                                          "new-refresh", list(youtube_direct.OAUTH_SCOPES))
        self.assertEqual(store.get_connection("old-id")["youtube_channel_id"], "UC-A")
        self.assertEqual(store.get_connection(second_id)["youtube_channel_id"], "UC-B")

    def test_new_upload_sets_private_schedule_audience_and_synthetic_fields(self):
        store = youtube_direct.YouTubeConnectionStore(self.database)

        class UploadHTTP:
            def __init__(self):
                self.posts = []
                self.puts = 0

            def post(self, url, **kwargs):
                self.posts.append(kwargs["json"])
                return FakeResponse(200, headers={"Location": "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&upload_id=fake"})

            def put(self, url, **kwargs):
                self.puts += 1
                if self.puts == 1:
                    return FakeResponse(308)
                return FakeResponse(201, {"id": "scheduled-id", "snippet": {"channelId": "UC-A"}, "status": {"privacyStatus": "private"}})

        video = Path(self.temp.name) / "scheduled.mp4"
        video.write_bytes(b"video-data")
        http = UploadHTTP()
        publisher = youtube_direct.YouTubeDirectPublisher(store, http)
        with patch.object(publisher, "access_token", return_value=({"local_channel_id": "local-a", "youtube_channel_id": "UC-A"}, "access")), \
             patch.object(publisher, "_list_channels", return_value=[{"id": "UC-A"}]), \
             patch.object(store, "get_connection", return_value={"local_channel_id": "local-a", "youtube_channel_id": "UC-A"}), \
             patch.object(store, "consume_insert_quota"), \
             patch.object(youtube_direct, "_validate_mp4", return_value=10):
            result = publisher.publish(
                connection_id="conn", expected_channel_id="UC-A", video_path=str(video),
                title="Hindi fact", description="Original explanation", privacy="private",
                made_for_kids=False, contains_synthetic_media=True,
                publish_at="2035-01-01T00:00:00Z", session_context={
                    "job_id": "job-a", "local_channel_id": "local-a", "connection_id": "conn",
                    "youtube_channel_id": "UC-A", "artifact_sha256": youtube_direct._artifact_sha256(video)})
        self.assertEqual(result["id"], "scheduled-id")
        payload = http.posts[0]
        self.assertEqual(payload["status"]["privacyStatus"], "private")
        self.assertEqual(payload["status"]["publishAt"], "2035-01-01T00:00:00Z")
        self.assertFalse(payload["status"]["selfDeclaredMadeForKids"])
        self.assertTrue(payload["status"]["containsSyntheticMedia"])


if __name__ == "__main__":
    unittest.main()
