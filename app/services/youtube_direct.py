"""Direct YouTube Data API publisher for the single-host Autopilot."""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sqlite3
import stat
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests
from cryptography.fernet import Fernet, InvalidToken
from loguru import logger

from app.config import config

YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
YOUTUBE_UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
OAUTH_SCOPES = ("https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube.readonly")
CHUNK_SIZE = 8 * 1024 * 1024
SESSION_FORMAT = "mpt-youtube-session"
SESSION_VERSION = 1
SESSION_HOSTS = {"www.googleapis.com", "youtube.googleapis.com"}
SESSION_PATH = "/upload/youtube/v3/videos"
_FERNET_PREFIX = b"gAAAA"


class YouTubePublisherError(RuntimeError):
    def __init__(self, message: str, *, confirmed: bool = False, retry_after: int | None = None,
                 session_expired: bool = False, reauthorization_required: bool = False,
                 temporary: bool = False):
        super().__init__(message)
        self.confirmed = confirmed
        self.retry_after = retry_after
        self.session_expired = session_expired
        self.reauthorization_required = reauthorization_required
        self.temporary = temporary


def _is_temporary(exc) -> bool:
    return isinstance(exc, requests.RequestException) or (
        isinstance(exc, YouTubePublisherError) and exc.temporary and not exc.session_expired)


def _wait_before_retry(exc):
    raw_delay = exc if isinstance(exc, str) else getattr(exc, "retry_after", None)
    if isinstance(raw_delay, str):
        if raw_delay.isdigit():
            delay = int(raw_delay)
        else:
            try:
                target = parsedate_to_datetime(raw_delay)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                delay = max(0, math.ceil((target - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                delay = 0
    else:
        delay = raw_delay if isinstance(raw_delay, int) and not isinstance(raw_delay, bool) else 0
    if delay > 30:
        raise YouTubePublisherError("YouTube requested a retry delay beyond the bounded recovery window",
                                    retry_after=delay, temporary=True)
    if delay > 0:
        time.sleep(delay)


def _validate_session_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query)
        if (parsed.scheme != "https" or parsed.hostname not in SESSION_HOSTS
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.path != SESSION_PATH or not query.get("upload_id")
                or query.get("uploadType") != ["resumable"] or parsed.fragment):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise YouTubePublisherError("YouTube resumable session URL has an untrusted origin or format", confirmed=True) from exc
    return value


def _artifact_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    try:
        with _open_regular_file(path) as source:
            before = os.fstat(source.fileno())
            while block := source.read(1024 * 1024):
                digest.update(block)
            after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise YouTubePublisherError("Video artifact changed while it was being validated", confirmed=True)
        return digest.hexdigest()
    except OSError as exc:
        raise YouTubePublisherError("Video artifact could not be read", confirmed=True) from exc


def _open_regular_file(path: str | Path):
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise YouTubePublisherError("Video artifact must be a regular file", confirmed=True)
        return os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise


def _hash_open_file(source) -> tuple[str, tuple[int, int, int]]:
    digest = hashlib.sha256()
    source.seek(0)
    before = os.fstat(source.fileno())
    while block := source.read(1024 * 1024):
        digest.update(block)
    after = os.fstat(source.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise YouTubePublisherError("Video artifact changed while it was being validated", confirmed=True)
    source.seek(0)
    return digest.hexdigest(), (after.st_size, after.st_mtime_ns, after.st_ino)


def _validate_mp4(path: str | Path) -> int:
    file_path = Path(path)
    try:
        stat = file_path.stat()
    except OSError as exc:
        raise YouTubePublisherError("Video artifact is missing or unreadable", confirmed=True) from exc
    if file_path.is_symlink() or not file_path.is_file() or file_path.suffix.lower() != ".mp4" or stat.st_size <= 0:
        raise YouTubePublisherError("Video artifact must be a non-empty MP4 file", confirmed=True)
    try:
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=format_name",
                                "-of", "default=noprint_wrappers=1:nokey=1", str(file_path)],
                               capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise YouTubePublisherError("MP4 validation via ffprobe failed", confirmed=True) from exc
    if probe.returncode or "mp4" not in probe.stdout.lower() and "mov" not in probe.stdout.lower():
        raise YouTubePublisherError("Video artifact is not a readable MP4 container", confirmed=True)
    return stat.st_size


def _session_binding(*, job_id: str, local_channel_id: str, connection_id: str,
                     youtube_channel_id: str, artifact_sha256: str) -> dict:
    return {"job_id": job_id, "local_channel_id": local_channel_id,
            "connection_id": connection_id, "youtube_channel_id": youtube_channel_id,
            "artifact_sha256": artifact_sha256}


def _oauth_config() -> tuple[str, str, str]:
    client_id = os.environ.get("MPT_YOUTUBE_CLIENT_ID") or config.app.get("youtube_oauth_client_id", "")
    client_secret = os.environ.get("MPT_YOUTUBE_CLIENT_SECRET") or config.app.get("youtube_oauth_client_secret", "")
    redirect_uri = os.environ.get("MPT_YOUTUBE_REDIRECT_URI") or config.app.get("youtube_oauth_redirect_uri", "")
    if not all(isinstance(item, str) and item.strip() for item in (client_id, client_secret, redirect_uri)):
        raise YouTubePublisherError("YouTube OAuth client ID, client secret, and redirect URI must be configured")
    return client_id.strip(), client_secret.strip(), redirect_uri.strip()


def _fernet() -> Fernet:
    key = os.environ.get("MPT_YOUTUBE_TOKEN_ENCRYPTION_KEY") or config.app.get("youtube_token_encryption_key", "")
    if not isinstance(key, str) or not key.strip():
        raise YouTubePublisherError("MPT_YOUTUBE_TOKEN_ENCRYPTION_KEY is required before connecting YouTube")
    try:
        return Fernet(key.strip().encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise YouTubePublisherError("YouTube token encryption key must be a valid Fernet key") from exc


class YouTubeConnectionStore:
    """Encrypted refresh tokens and single-use OAuth states in the Autopilot DB."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(str(self.database_path), timeout=20, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self):
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS youtube_oauth_states (
                    state TEXT PRIMARY KEY, local_channel_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS youtube_connections (
                    connection_id TEXT PRIMARY KEY, local_channel_id TEXT NOT NULL,
                    youtube_channel_id TEXT NOT NULL, channel_title TEXT NOT NULL,
                    encrypted_refresh_token BLOB NOT NULL, scopes_json TEXT NOT NULL,
                    state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS youtube_insert_quota (
                    quota_date TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0
                );
            """)
            # Serialize schema inspection and migration across simultaneous API
            # workers; a second initializer re-reads the committed schema afterward.
            conn.execute("BEGIN IMMEDIATE")
            unique_local = False
            for index in conn.execute("PRAGMA index_list(youtube_connections)").fetchall():
                if index["unique"]:
                    fields = [item["name"] for item in conn.execute(f"PRAGMA index_info('{index['name']}')").fetchall()]
                    unique_local |= fields == ["local_channel_id"]
            if unique_local:
                # Older builds stored only one auth token per MPT profile. Retain that
                # token row so already-created jobs can continue to use their pinned ID.
                conn.execute("ALTER TABLE youtube_connections RENAME TO youtube_connections_legacy")
                conn.execute("CREATE TABLE youtube_connections (connection_id TEXT PRIMARY KEY, local_channel_id TEXT NOT NULL, youtube_channel_id TEXT NOT NULL, channel_title TEXT NOT NULL, encrypted_refresh_token BLOB NOT NULL, scopes_json TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
                conn.execute("INSERT INTO youtube_connections (connection_id,local_channel_id,youtube_channel_id,channel_title,encrypted_refresh_token,scopes_json,state,created_at,updated_at) SELECT connection_id,local_channel_id,youtube_channel_id,channel_title,encrypted_refresh_token,scopes_json,state,created_at,updated_at FROM youtube_connections_legacy")
                conn.execute("DROP TABLE youtube_connections_legacy")
            conn.commit()

    def begin_oauth(self, local_channel_id: str) -> tuple[str, str]:
        _fernet()  # fail before redirect if tokens could not be stored securely
        client_id, _, redirect_uri = _oauth_config()
        state = uuid4().hex + uuid4().hex
        now = datetime.now(timezone.utc)
        with self._connection() as conn:
            conn.execute("DELETE FROM youtube_oauth_states WHERE created_at < ?",
                         ((now - timedelta(minutes=15)).isoformat(),))
            conn.execute("INSERT INTO youtube_oauth_states VALUES (?, ?, ?)",
                         (state, local_channel_id, now.isoformat()))
        params = {
            "client_id": client_id, "redirect_uri": redirect_uri,
            "response_type": "code", "scope": " ".join(OAUTH_SCOPES),
            "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
            "state": state,
        }
        return state, f"{GOOGLE_AUTH}?{urlencode(params)}"

    def consume_oauth_state(self, state: str) -> str:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT local_channel_id,created_at FROM youtube_oauth_states WHERE state=?",
                               (state,)).fetchone()
            conn.execute("DELETE FROM youtube_oauth_states WHERE state=?", (state,))
            conn.commit()
        if not row or datetime.fromisoformat(row["created_at"]) < datetime.now(timezone.utc) - timedelta(minutes=15):
            raise YouTubePublisherError("YouTube OAuth state is invalid or expired")
        return row["local_channel_id"]

    def save_connection(self, local_channel_id: str, channel: dict, refresh_token: str,
                        scopes: list[str]) -> str:
        if not isinstance(refresh_token, str) or not refresh_token or not isinstance(channel, dict) or not isinstance(channel.get("id"), str):
            raise YouTubePublisherError("Google OAuth connection data is incomplete", confirmed=True)
        scopes = YouTubeDirectPublisher._validate_scopes(" ".join(scopes) if isinstance(scopes, list) else scopes)
        connection_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        encrypted = _fernet().encrypt(refresh_token.encode("utf-8"))
        with self._connection() as conn:
            conn.execute("INSERT INTO youtube_connections VALUES (?, ?, ?, ?, ?, ?, 'connected', ?, ?)",
                         (connection_id, local_channel_id, channel["id"], channel.get("snippet", {}).get("title", ""),
                          encrypted, json.dumps(scopes), now, now))
        return connection_id

    def get_connection(self, connection_id: str) -> dict:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM youtube_connections WHERE connection_id=? AND state='connected'",
                               (connection_id,)).fetchone()
        if row is None:
            raise YouTubePublisherError("YouTube connection is missing or disconnected", confirmed=True)
        try:
            token = _fernet().decrypt(row["encrypted_refresh_token"]).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise YouTubePublisherError("YouTube token cannot be decrypted; restore the matching encryption key") from exc
        try:
            scopes = json.loads(row["scopes_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise YouTubePublisherError("Stored YouTube OAuth scope metadata is corrupt; reconnect this channel", confirmed=True,
                                        reauthorization_required=True) from exc
        if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
            raise YouTubePublisherError("Stored YouTube OAuth scope metadata is invalid; reconnect this channel", confirmed=True,
                                        reauthorization_required=True)
        return {**dict(row), "refresh_token": token, "scopes": scopes}

    def disconnect(self, connection_id: str):
        with self._connection() as conn:
            conn.execute("UPDATE youtube_connections SET encrypted_refresh_token=?,state='disconnected',updated_at=? WHERE connection_id=?",
                         (b"", datetime.now(timezone.utc).isoformat(), connection_id))

    def rotate_refresh_token(self, connection_id: str, refresh_token: str):
        if not isinstance(refresh_token, str) or not refresh_token:
            raise YouTubePublisherError("Google returned an invalid refresh token", confirmed=True,
                                        reauthorization_required=True)
        encrypted = _fernet().encrypt(refresh_token.encode("utf-8"))
        with self._connection() as conn:
            conn.execute("UPDATE youtube_connections SET encrypted_refresh_token=?,updated_at=? WHERE connection_id=? AND state='connected'",
                         (encrypted, datetime.now(timezone.utc).isoformat(), connection_id))

    @staticmethod
    def encrypt_secret(value: str) -> bytes:
        return _fernet().encrypt(value.encode("utf-8"))

    @staticmethod
    def decrypt_secret(value: str | bytes) -> str:
        raw = value if isinstance(value, bytes) else value.encode("utf-8")
        try:
            return _fernet().decrypt(raw).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise YouTubePublisherError("Encrypted YouTube secret cannot be authenticated; restore the matching encryption key") from exc

    @staticmethod
    def encrypt_session(session_url: str, binding: dict, *, legacy_unverified: bool = False) -> bytes:
        _validate_session_url(session_url)
        payload = {"format": SESSION_FORMAT, "version": SESSION_VERSION, "url": session_url,
                   "binding": binding, "legacy_unverified": legacy_unverified}
        return _fernet().encrypt(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())

    @staticmethod
    def decode_session(value: str | bytes, expected_binding: dict) -> tuple[str, bool, bool]:
        """Return (URL, legacy-format, legacy-unverified); migrate legacy values explicitly."""
        raw = value if isinstance(value, bytes) else value.encode("utf-8")
        if raw.startswith(_FERNET_PREFIX):
            try:
                plaintext = _fernet().decrypt(raw)
            except InvalidToken as exc:
                raise YouTubePublisherError("Encrypted YouTube session cannot be authenticated; restore the matching encryption key") from exc
            try:
                decoded = plaintext.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise YouTubePublisherError("Encrypted YouTube session has an invalid format", confirmed=True) from exc
            try:
                payload = json.loads(decoded)
            except json.JSONDecodeError:
                # Previous versions encrypted only the raw URL and recorded no binding.
                _validate_session_url(decoded)
                return decoded, True, True
            if (not isinstance(payload, dict) or payload.get("format") != SESSION_FORMAT
                    or payload.get("version") != SESSION_VERSION or payload.get("binding") != expected_binding
                    or not isinstance(payload.get("legacy_unverified"), bool)
                    or not isinstance(payload.get("url"), str)):
                raise YouTubePublisherError("YouTube session binding or version is invalid; upload remains unreconciled", confirmed=True)
            _validate_session_url(payload["url"])
            return payload["url"], False, payload["legacy_unverified"]
        # Explicit compatibility path for old plaintext session URLs only. A corrupt
        # Fernet token starts with the Fernet marker and can never reach this branch.
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise YouTubePublisherError("Legacy YouTube session has an invalid format", confirmed=True) from exc
        _validate_session_url(decoded)
        return decoded, True, True

    def connection_for_channel(self, local_channel_id: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute("SELECT connection_id,youtube_channel_id,channel_title,state,updated_at "
                               "FROM youtube_connections WHERE local_channel_id=? AND state='connected' "
                               "ORDER BY updated_at DESC, created_at DESC, rowid DESC LIMIT 1", (local_channel_id,)).fetchone()
        return dict(row) if row else None

    def disconnect_channel(self, local_channel_id: str):
        with self._connection() as conn:
            conn.execute("UPDATE youtube_connections SET encrypted_refresh_token=?,state='disconnected',updated_at=? WHERE local_channel_id=?",
                         (b"", datetime.now(timezone.utc).isoformat(), local_channel_id))

    def consume_insert_quota(self, daily_limit: int | None = None):
        configured = config.app.get("youtube_insert_daily_limit", 100) if daily_limit is None else daily_limit
        if isinstance(configured, bool) or not isinstance(configured, int) or configured <= 0:
            raise YouTubePublisherError("YouTube videos.insert daily limit must be a positive integer", confirmed=True)
        limit = configured
        day = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO youtube_insert_quota VALUES (?, 0)", (day,))
            row = conn.execute("SELECT attempts FROM youtube_insert_quota WHERE quota_date=?", (day,)).fetchone()
            if row["attempts"] >= limit:
                conn.rollback()
                raise YouTubePublisherError("MPT's configured YouTube videos.insert daily safety limit is reached", confirmed=True)
            conn.execute("UPDATE youtube_insert_quota SET attempts=attempts+1 WHERE quota_date=?", (day,))
            conn.commit()


class YouTubeDirectPublisher:
    """OAuth, identity verification, resumable upload, and same-session recovery."""

    def __init__(self, store: YouTubeConnectionStore, http=requests):
        self.store = store
        self.http = http

    def oauth_callback(self, state: str, code: str) -> dict:
        local_channel_id = self.store.consume_oauth_state(state)
        client_id, client_secret, redirect_uri = _oauth_config()
        response = self.http.post(GOOGLE_TOKEN, data={
            "code": code, "client_id": client_id, "client_secret": client_secret,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code",
        }, timeout=20, allow_redirects=False)
        self._raise_http(response, "OAuth token exchange")
        token = response.json()
        if not isinstance(token, dict):
            raise YouTubePublisherError("Google returned an invalid OAuth token response", confirmed=True)
        refresh_token = token.get("refresh_token")
        access = token.get("access_token")
        granted_scopes = token.get("scope")
        if not isinstance(refresh_token, str) or not refresh_token or not isinstance(access, str) or not access:
            raise YouTubePublisherError("Google returned an incomplete OAuth grant; revoke access and authorize again", confirmed=True,
                                        reauthorization_required=True)
        scopes = self._validate_scopes(granted_scopes)
        self._validate_token_lifetime(token)
        channels = self._unique_channels(self._list_channels(access))
        # Standard Data API uploads target the authenticated YouTube identity; there is
        # no arbitrary channel-id switch on videos.insert. A Google account's YouTube
        # default channel may influence third-party sign-in, but it is not proof that
        # this OAuth grant and videos.insert target exactly that channel. Require the
        # API identity lookup itself to resolve to exactly one channel.
        if not channels:
            raise YouTubePublisherError(
                "No YouTube channel found for this authorized Google identity. Confirm that the selected "
                "account manages the intended channel and that it is available to third-party apps, then "
                "revoke MPT access and reconnect."
            )
        if len(channels) > 1:
            raise YouTubePublisherError(
                "Google authorized multiple distinct YouTube channels, so MPT cannot safely bind an upload destination. "
                "Set the intended channel as your YouTube default channel, revoke MPT access, and reconnect. "
                "The authorized channel lookup must resolve to one channel before MPT can connect."
            )
        channel = channels[0]
        connection_id = self.store.save_connection(local_channel_id, channel, refresh_token,
                                                    scopes)
        return {"local_channel_id": local_channel_id, "connection_id": connection_id,
                "youtube_channel_id": channel["id"],
                "channel_title": channel.get("snippet", {}).get("title", ""),
                "status": "connected"}

    @staticmethod
    def _validate_scopes(value) -> list[str]:
        if not isinstance(value, str):
            raise YouTubePublisherError("Google OAuth grant did not report granted scopes; revoke access and authorize again", confirmed=True,
                                        reauthorization_required=True)
        scopes = value.split()
        if not set(OAUTH_SCOPES).issubset(scopes):
            raise YouTubePublisherError("Google OAuth grant is missing required YouTube scopes; revoke access and authorize again", confirmed=True,
                                        reauthorization_required=True)
        return scopes

    @staticmethod
    def _validate_token_lifetime(token: dict):
        expires = token.get("expires_in")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= 0:
            raise YouTubePublisherError("Google returned an invalid OAuth token lifetime", confirmed=True,
                                        reauthorization_required=True)
        token_type = token.get("token_type", "Bearer")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise YouTubePublisherError("Google returned an unsupported OAuth token type", confirmed=True,
                                        reauthorization_required=True)

    def _list_channels(self, access_token: str) -> list[dict]:
        channels = []
        page_token = None
        seen_tokens = set()
        raw_count = 0
        for _ in range(100):
            params = {"part": "id,snippet", "mine": "true", "maxResults": 50}
            if page_token:
                params["pageToken"] = page_token
            response = self.http.get(f"{YOUTUBE_API}/channels", params=params,
                                     headers={"Authorization": f"Bearer {access_token}"}, timeout=20,
                                     allow_redirects=False)
            self._raise_http(response, "YouTube channel identity lookup")
            try:
                payload = response.json()
            except (ValueError, requests.exceptions.JSONDecodeError) as exc:
                logger.info("YouTube channels.list response status={} top_level_keys=[] page_info=None item_count=None",
                            response.status_code)
                raise YouTubePublisherError("YouTube returned a malformed channel list; destination was not bound", confirmed=True) from exc
            if not isinstance(payload, dict):
                logger.info("YouTube channels.list response status={} top_level_keys=[] page_info=None item_count=None",
                            response.status_code)
                raise YouTubePublisherError("YouTube returned a malformed channel list; destination was not bound", confirmed=True)
            raw_items = payload.get("items")
            item_count = len(raw_items) if isinstance(raw_items, list) else None
            raw_page_info = payload.get("pageInfo")
            page_info_log = None
            if isinstance(raw_page_info, dict):
                page_info_log = {key: raw_page_info[key] for key in ("totalResults", "resultsPerPage")
                                 if isinstance(raw_page_info.get(key), int) and not isinstance(raw_page_info.get(key), bool)}
            logger.info("YouTube channels.list response status={} top_level_keys={} page_info={} item_count={}",
                        response.status_code, sorted(str(key)[:64] for key in payload.keys())[:32],
                        json.dumps(page_info_log, sort_keys=True, separators=(",", ":")) if page_info_log is not None else None,
                        item_count)
            # The Data API may omit `items` for a successful empty result. Accept
            # that shape only when pageInfo explicitly confirms there are no results.
            if "items" not in payload and isinstance(raw_page_info, dict) and raw_page_info.get("totalResults") == 0:
                payload["items"] = []
                raw_items = payload["items"]
            if not isinstance(raw_items, list):
                raise YouTubePublisherError("YouTube returned a malformed channel list; destination was not bound", confirmed=True)
            if payload.get("kind") not in (None, "youtube#channelListResponse"):
                raise YouTubePublisherError("YouTube returned an unexpected channel-list resource", confirmed=True)
            items = raw_items
            raw_count += len(items)
            page_info = payload.get("pageInfo")
            if page_info is not None and (
                    not isinstance(page_info, dict)
                    or isinstance(page_info.get("totalResults"), bool)
                    or not isinstance(page_info.get("totalResults"), int)
                    or page_info["totalResults"] < raw_count):
                raise YouTubePublisherError("YouTube returned invalid channel pagination metadata; destination was not bound", confirmed=True)
            for item in items:
                snippet = item.get("snippet") if isinstance(item, dict) else None
                channel_id = item.get("id") if isinstance(item, dict) else None
                if (not isinstance(channel_id, str) or not channel_id or not isinstance(snippet, dict)
                        or not isinstance(snippet.get("title"), str)):
                    raise YouTubePublisherError("YouTube returned a malformed channel identity; destination was not bound", confirmed=True)
                channels.append({"id": channel_id, "snippet": snippet})
            next_token = payload.get("nextPageToken")
            if next_token is None:
                if page_info is not None and page_info["totalResults"] > raw_count:
                    raise YouTubePublisherError("YouTube omitted a required channel page; destination was not bound", confirmed=True)
                break
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise YouTubePublisherError("YouTube returned invalid channel pagination; destination was not bound", confirmed=True)
            seen_tokens.add(next_token)
            page_token = next_token
        else:
            raise YouTubePublisherError("YouTube channel list exceeded the pagination safety bound", confirmed=True)
        return channels

    @staticmethod
    def _unique_channels(channels: list[dict]) -> list[dict]:
        """Collapse duplicate resource entries by channel ID, preserving API order."""
        unique = {}
        for channel in channels:
            unique.setdefault(channel["id"], channel)
        return list(unique.values())

    @staticmethod
    def _raise_http(response, action: str):
        if 200 <= response.status_code < 300 or response.status_code == 308:
            return
        retry_after = response.headers.get("Retry-After")
        code = None
        reason = None
        try:
            payload = response.json()
            if isinstance(payload, dict):
                error = payload.get("error")
                code = error.get("error") if isinstance(error, dict) else error
                errors = error.get("errors") if isinstance(error, dict) else None
                if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                    reason = errors[0].get("reason")
        except Exception:
            pass
        reauth = (response.status_code == 401
                  or code in {"invalid_grant", "unauthorized_client", "invalid_client"}
                  or reason in {"insufficientPermissions", "authError", "youtubeSignupRequired"})
        expired_session = response.status_code in {404, 410} and "session" in action.lower()
        confirmed = (response.status_code in {400, 401, 403, 404, 410, 429} or code == "invalid_grant") and not expired_session
        safe_code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else None
        safe_reason = reason if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", reason) else None
        diagnostic = ", ".join(item for item in (f"code={safe_code}" if safe_code else None,
                                                   f"reason={safe_reason}" if safe_reason else None) if item)
        raise YouTubePublisherError(f"{action} rejected by YouTube (HTTP {response.status_code}{'; ' + diagnostic if diagnostic else ''})",
                                    confirmed=confirmed,
                                    retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None,
                                    session_expired=expired_session,
                                    reauthorization_required=reauth,
                                    temporary=response.status_code in {408, 429, 500, 502, 503, 504})

    def access_token(self, connection_id: str) -> tuple[dict, str]:
        connection = self.store.get_connection(connection_id)
        client_id, client_secret, _ = _oauth_config()
        response = self.http.post(GOOGLE_TOKEN, data={
            "client_id": client_id, "client_secret": client_secret,
            "refresh_token": connection["refresh_token"], "grant_type": "refresh_token",
        }, timeout=20, allow_redirects=False)
        self._raise_http(response, "OAuth token refresh")
        token = response.json()
        if not isinstance(token, dict) or not isinstance(token.get("access_token"), str) or not token["access_token"]:
            raise YouTubePublisherError("Google returned an invalid token refresh response", confirmed=True,
                                        reauthorization_required=True)
        scopes = connection.get("scopes") or []
        if not set(OAUTH_SCOPES).issubset(scopes):
            raise YouTubePublisherError("Stored OAuth grant is missing required YouTube scopes; reconnect this channel", confirmed=True,
                                        reauthorization_required=True)
        if "scope" in token:
            refreshed_scopes = self._validate_scopes(token["scope"])
            if not set(OAUTH_SCOPES).issubset(refreshed_scopes):
                raise YouTubePublisherError("Google token refresh changed the granted YouTube scopes; reconnect this channel", confirmed=True,
                                            reauthorization_required=True)
        self._validate_token_lifetime(token)
        if token.get("refresh_token"):
            if not isinstance(token["refresh_token"], str):
                raise YouTubePublisherError("Google returned an invalid refresh token", confirmed=True,
                                            reauthorization_required=True)
            self.store.rotate_refresh_token(connection_id, token["refresh_token"])
        return connection, token["access_token"]

    def revoke(self, connection_id: str):
        connection = self.store.get_connection(connection_id)
        try:
            response = self.http.post("https://oauth2.googleapis.com/revoke",
                                      data={"token": connection["refresh_token"]}, timeout=15,
                                      allow_redirects=False)
            # Google treats a token which is already revoked as a failed revoke call;
            # local deletion still prevents MPT from using it again.
            self.store.disconnect(connection_id)
            if response.status_code not in (200, 400):
                raise YouTubePublisherError(f"Local connection was removed but Google token revocation returned HTTP {response.status_code}")
        except requests.RequestException as exc:
            self.store.disconnect(connection_id)
            raise YouTubePublisherError("Local connection was removed, but Google token revocation could not be confirmed") from exc

    def publish(self, *, connection_id: str, expected_channel_id: str, video_path: str,
                title: str, description: str = "", tags: list[str] | None = None,
                privacy: str = "private", made_for_kids: bool = False,
                contains_synthetic_media: bool = False, publish_at: str | None = None,
                session_context: dict, on_session=None) -> dict:
        connection, access = self.access_token(connection_id)
        if connection["youtube_channel_id"] != expected_channel_id:
            raise YouTubePublisherError("Pinned YouTube destination does not match the authorized connection", confirmed=True)
        channels = self._unique_channels(self._list_channels(access))
        if len(channels) != 1 or channels[0]["id"] != expected_channel_id:
            raise YouTubePublisherError("Authorized YouTube identity changed; reconnect and verify the pinned channel", confirmed=True)
        size = self._validate_publish_inputs(video_path, title, description, tags, privacy,
                                             made_for_kids, contains_synthetic_media, publish_at)
        artifact_hash = _artifact_sha256(video_path)
        required_context = {"connection_id": connection_id, "youtube_channel_id": expected_channel_id,
                            "artifact_sha256": artifact_hash}
        if (not isinstance(session_context, dict)
                or any(session_context.get(k) != v for k, v in required_context.items())
                or not session_context.get("job_id")
                or session_context.get("local_channel_id") != connection.get("local_channel_id")):
            raise YouTubePublisherError("Resumable upload is missing its immutable job, connection, channel, or artifact binding", confirmed=True)
        payload = {
            "snippet": {"title": title, "description": description, "tags": tags or [], "categoryId": "22"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids,
                       "containsSyntheticMedia": contains_synthetic_media},
        }
        if publish_at:
            payload["status"]["publishAt"] = publish_at
        self.store.consume_insert_quota()
        try:
            response = self.http.post(YOUTUBE_UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"},
                headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json; charset=UTF-8",
                         "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
                json=payload, timeout=30, allow_redirects=False)
        except requests.RequestException as exc:
            raise YouTubePublisherError("YouTube upload initialization outcome is uncertain; no second insert was attempted") from exc
        self._raise_http(response, "YouTube resumable upload initialization")
        session_url = response.headers.get("Location")
        if not session_url:
            raise YouTubePublisherError("YouTube did not return a resumable session URL; upload outcome is uncertain")
        _validate_session_url(session_url)
        if on_session:
            on_session(session_url)
        return self.resume(session_url, video_path, access, expected_channel_id,
                           session_context=session_context, on_session=on_session)

    @staticmethod
    def _validate_publish_inputs(video_path, title, description, tags, privacy,
                                 made_for_kids, contains_synthetic_media, publish_at) -> int:
        if not isinstance(privacy, str) or privacy not in {"private", "public", "unlisted"}:
            raise YouTubePublisherError("YouTube privacy must be private, public, or unlisted", confirmed=True)
        if (not isinstance(title, str) or not title.strip() or len(title) > 100
                or "<" in title or ">" in title):
            raise YouTubePublisherError("YouTube title must contain 1 to 100 characters and cannot include < or >", confirmed=True)
        if not isinstance(description, str) or "<" in description or ">" in description:
            raise YouTubePublisherError("YouTube description must be valid text and cannot include < or >", confirmed=True)
        try:
            if len(description.encode("utf-8")) > 5000:
                raise YouTubePublisherError("YouTube description cannot exceed 5000 UTF-8 bytes", confirmed=True)
        except UnicodeEncodeError as exc:
            raise YouTubePublisherError("YouTube description is not valid UTF-8 text", confirmed=True) from exc
        if not isinstance(tags, (list, type(None))) or any(not isinstance(tag, str) for tag in (tags or [])):
            raise YouTubePublisherError("YouTube tags must be a list of strings", confirmed=True)
        tag_list = tags or []
        tag_length = sum(len(tag) + (2 if " " in tag else 0) for tag in tag_list) + max(0, len(tag_list) - 1)
        if len(tag_list) > 500 or tag_length > 500:
            raise YouTubePublisherError("YouTube tags exceed the 500-character API limit", confirmed=True)
        if not isinstance(made_for_kids, bool) or not isinstance(contains_synthetic_media, bool):
            raise YouTubePublisherError("YouTube audience and synthetic-media flags must be boolean", confirmed=True)
        if publish_at is not None:
            if not isinstance(publish_at, str) or not publish_at:
                raise YouTubePublisherError("YouTube publishAt must be a future timezone-aware ISO 8601 timestamp", confirmed=True)
            if privacy != "private":
                raise YouTubePublisherError("YouTube scheduled videos must have private privacy", confirmed=True)
            try:
                scheduled = datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
                if scheduled.tzinfo is None or scheduled <= datetime.now(timezone.utc):
                    raise ValueError
            except (ValueError, TypeError) as exc:
                raise YouTubePublisherError("YouTube publishAt must be a future timezone-aware ISO 8601 timestamp", confirmed=True) from exc
        return _validate_mp4(video_path)

    def resume(self, session_url: str, video_path: str, access_token: str,
               expected_channel_id: str, *, session_context: dict,
               legacy_unverified: bool = False, on_session=None) -> dict:
        _validate_session_url(session_url)
        size = _validate_mp4(video_path)
        if session_context.get("youtube_channel_id") != expected_channel_id or not session_context.get("connection_id") or not session_context.get("job_id"):
            raise YouTubePublisherError("Resumable session is bound to a different job or YouTube destination", confirmed=True)
        connection = self.store.get_connection(session_context["connection_id"])
        if (connection.get("local_channel_id") != session_context.get("local_channel_id")
                or connection.get("youtube_channel_id") != expected_channel_id):
            raise YouTubePublisherError("Resumable session connection does not match its pinned channel", confirmed=True)
        retries = 0
        refreshed = False
        no_progress = 0
        last_reconciled_offset = None
        with _open_regular_file(video_path) as source:
            artifact_hash, file_signature = _hash_open_file(source)
            if artifact_hash != session_context.get("artifact_sha256"):
                raise YouTubePublisherError("Video artifact hash does not match the immutable resumable session binding", confirmed=True)
            if legacy_unverified:
                offset, completed, updated_url, retry_after = self._session_offset(session_url, size, access_token)
                if retry_after:
                    _wait_before_retry(retry_after)
                if updated_url:
                    session_url = updated_url
                    if on_session:
                        on_session(updated_url)
                if completed is not None:
                    self._validate_receipt(completed, expected_channel_id)
                    raise YouTubePublisherError("Legacy session completion was detected, but its original artifact binding is unknown; keep the publication unreconciled for operator review")
                raise YouTubePublisherError("Legacy resumable session was migrated without an artifact hash; it was reconciled but not resumed to avoid changing its contents")
            while True:
                try:
                    offset, completed, updated_url, retry_after = self._session_offset(session_url, size, access_token)
                except (requests.RequestException, YouTubePublisherError) as exc:
                    if isinstance(exc, YouTubePublisherError) and exc.reauthorization_required and not refreshed:
                        _, access_token = self.access_token(session_context["connection_id"])
                        refreshed = True
                        continue
                    if not _is_temporary(exc) or retries >= 3:
                        raise
                    retries += 1
                    _wait_before_retry(exc)
                    continue
                if updated_url:
                    session_url = updated_url
                    if on_session:
                        on_session(updated_url)
                if retry_after:
                    _wait_before_retry(retry_after)
                if last_reconciled_offset is not None and offset > last_reconciled_offset:
                    no_progress = 0
                    retries = 0
                last_reconciled_offset = offset
                if completed is not None:
                    return self._validate_receipt(completed, expected_channel_id)
                if retries >= 3:
                    raise YouTubePublisherError("YouTube upload remains incomplete after bounded session reconciliation")
                if offset >= size:
                    raise YouTubePublisherError("YouTube reported an invalid resumable upload offset")
                current_stat = os.fstat(source.fileno())
                current_signature = (current_stat.st_size, current_stat.st_mtime_ns, current_stat.st_ino)
                if current_signature != file_signature:
                    raise YouTubePublisherError("Video artifact changed during resumable upload; outcome requires reconciliation")
                source.seek(offset)
                data = source.read(min(CHUNK_SIZE, size - offset))
                if not data:
                    raise YouTubePublisherError("Video artifact ended before the bound upload size", confirmed=True)
                current_stat = os.fstat(source.fileno())
                if (current_stat.st_size, current_stat.st_mtime_ns, current_stat.st_ino) != file_signature:
                    raise YouTubePublisherError("Video artifact changed while preparing a resumable chunk; outcome requires reconciliation")
                end = offset + len(data) - 1
                try:
                    response = self.http.put(session_url, data=data, headers={
                        "Authorization": f"Bearer {access_token}", "Content-Length": str(len(data)),
                        "Content-Type": "video/mp4", "Content-Range": f"bytes {offset}-{end}/{size}",
                    }, timeout=180, allow_redirects=False)
                except requests.RequestException:
                    retries += 1
                    _wait_before_retry(None)
                    continue  # Next iteration probes this same session before sending data again.
                if response.status_code in (200, 201):
                    return self._validate_receipt(response.json(), expected_channel_id)
                if response.status_code == 308:
                    next_offset = self._range_offset(response.headers.get("Range"))
                    if next_offset > end + 1:
                        raise YouTubePublisherError("YouTube reported a byte range beyond the sent chunk", confirmed=True)
                    location = response.headers.get("Location")
                    if location:
                        _validate_session_url(location)
                        session_url = location
                        if on_session:
                            on_session(location)
                    if response.headers.get("Retry-After"):
                        _wait_before_retry(response.headers["Retry-After"])
                    if next_offset > offset:
                        retries = 0
                        no_progress = 0
                    else:
                        no_progress += 1
                        if no_progress >= 3:
                            raise YouTubePublisherError("YouTube resumable session made no confirmed progress")
                    continue
                try:
                    self._raise_http(response, "YouTube resumable session upload")
                except YouTubePublisherError as exc:
                    if exc.reauthorization_required and not refreshed:
                        _, access_token = self.access_token(session_context["connection_id"])
                        refreshed = True
                        continue  # Reconcile before any further media bytes.
                    if _is_temporary(exc):
                        retries += 1
                        _wait_before_retry(exc)
                        continue
                    raise

    def _session_offset(self, session_url: str, size: int, access_token: str) -> tuple[int, dict | None, str | None, str | None]:
        _validate_session_url(session_url)
        response = self.http.put(session_url, data=b"", headers={
            "Authorization": f"Bearer {access_token}", "Content-Length": "0",
            "Content-Range": f"bytes */{size}",
        }, timeout=30, allow_redirects=False)
        if response.status_code in (200, 201):
            return 0, response.json(), None, None
        if response.status_code != 308:
            self._raise_http(response, "YouTube resumable session reconciliation")
        location = response.headers.get("Location")
        if location:
            _validate_session_url(location)
        return self._range_offset(response.headers.get("Range")), None, location, response.headers.get("Retry-After")

    @staticmethod
    def _validate_receipt(result: dict, expected_channel_id: str) -> dict:
        if not isinstance(result, dict) or not isinstance(result.get("snippet"), dict):
            raise YouTubePublisherError("Upload completed but YouTube returned a malformed receipt; publication is unknown")
        channel_id = result["snippet"].get("channelId")
        if not isinstance(result.get("id"), str) or not result["id"] or channel_id != expected_channel_id:
            raise YouTubePublisherError("Upload completed but YouTube returned an unexpected channel identity; publication is unknown")
        return result

    @staticmethod
    def _range_offset(value: str | None) -> int:
        if not value:
            return 0
        match = re.fullmatch(r"bytes=0-(\d{1,20})", value)
        if not match:
            raise YouTubePublisherError("YouTube returned a malformed resumable byte range", confirmed=True)
        return int(match.group(1)) + 1

    def videos_list(self, connection_id: str, video_id: str) -> dict:
        _, access = self.access_token(connection_id)
        response = self.http.get(f"{YOUTUBE_API}/videos", params={"part": "id,snippet,status,processingDetails", "id": video_id},
                                 headers={"Authorization": f"Bearer {access}"}, timeout=20,
                                 allow_redirects=False)
        self._raise_http(response, "YouTube video status lookup")
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise YouTubePublisherError("YouTube returned a malformed video status response", confirmed=True)
        items = payload["items"]
        if len(items) > 1 or any(not isinstance(item, dict) for item in items):
            raise YouTubePublisherError("YouTube returned an ambiguous video status response", confirmed=True)
        return items[0] if items else {}
