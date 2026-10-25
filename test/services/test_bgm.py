"""Optional background music must not block generation when no tracks exist."""

from app.services import video


def test_random_background_music_falls_back_to_silence_when_library_is_empty(monkeypatch):
    monkeypatch.setattr(video.bgm_service, "list_bgm_files", lambda: [])

    assert video.get_bgm_file("random") == ""
