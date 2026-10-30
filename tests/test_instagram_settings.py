from pathlib import Path

import pytest

from app.config import PROJECT_ROOT
from app.instagram.settings import InstagramSettings


def test_defaults() -> None:
    s = InstagramSettings(_env_file=None)
    assert s.username is None and s.password is None
    assert s.session_file == (PROJECT_ROOT / "data" / "instagram" / "session.json").resolve()
    assert s.headless is True
    assert s.login_headless is False
    assert s.request_delay_seconds == 1.5


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("INSTAGRAM_USERNAME", "someone")
    monkeypatch.setenv("INSTAGRAM_PASSWORD", "  ")  # blank -> None
    monkeypatch.setenv("INSTAGRAM_SESSION_FILE", str(tmp_path / "s.json"))
    monkeypatch.setenv("INSTAGRAM_HEADLESS", "false")
    monkeypatch.setenv("INSTAGRAM_LOGIN_HEADLESS", "true")
    monkeypatch.setenv("INSTAGRAM_REQUEST_DELAY_SECONDS", "0")
    s = InstagramSettings(_env_file=None)
    assert s.username == "someone"
    assert s.password is None
    assert s.session_file == tmp_path / "s.json"
    assert s.headless is False
    assert s.login_headless is True
    assert s.request_delay_seconds == 0


def test_relative_session_file_resolves_against_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSTAGRAM_SESSION_FILE", "tmp/ig.json")
    assert InstagramSettings(_env_file=None).session_file == (PROJECT_ROOT / "tmp" / "ig.json").resolve()


def test_rejects_zero_wait() -> None:
    with pytest.raises(ValueError):
        InstagramSettings(_env_file=None, login_wait_seconds=0)
