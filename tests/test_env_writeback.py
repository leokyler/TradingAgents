"""Where a pasted API key lands: project .env, else shared file, else cwd."""

from __future__ import annotations

import os
import stat
from unittest.mock import patch

import pytest

from cli import prompts
from tradingagents.env import api_key_target


@pytest.mark.unit
def test_project_env_file_wins(tmp_path, monkeypatch):
    project = tmp_path / "checkout"
    project.mkdir()
    (project / ".env").write_text("OTHER=1\n")
    shared = tmp_path / "shared.env"
    shared.write_text("OTHER=1\n")
    monkeypatch.setenv("TRADINGAGENTS_ENV_FILE", str(shared))

    assert api_key_target(str(project / ".env")) == project / ".env"


@pytest.mark.unit
def test_shared_file_is_used_once_it_exists(tmp_path, monkeypatch):
    shared = tmp_path / "shared.env"
    shared.write_text("OTHER=1\n")
    monkeypatch.setenv("TRADINGAGENTS_ENV_FILE", str(shared))

    assert api_key_target() == shared


@pytest.mark.unit
def test_upstream_fallback_kept_while_shared_file_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.chdir(tmp_path)

    assert api_key_target() == tmp_path / ".env"


def _prompt_key(key="sk-typed-in"):
    fake = type("P", (), {"ask": staticmethod(lambda: key)})()
    with patch.object(prompts.questionary, "password", return_value=fake):
        return prompts.ensure_api_key("openai")


@pytest.mark.unit
def test_prompted_key_is_saved_to_the_shared_file(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    shared = tmp_path / "shared.env"
    shared.write_text("OTHER=1\n")
    monkeypatch.setenv("TRADINGAGENTS_ENV_FILE", str(shared))
    monkeypatch.chdir(tmp_path)

    assert _prompt_key() == "sk-typed-in"
    content = shared.read_text()
    assert "OPENAI_API_KEY" in content and "sk-typed-in" in content
    assert "OTHER=1" in content
    # Not a new .env in whatever directory the shell happened to be in.
    assert not (tmp_path / ".env").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_shared_key_file_is_owner_only(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    shared = tmp_path / "shared.env"
    shared.write_text("OTHER=1\n")
    os.chmod(shared, 0o664)
    monkeypatch.setenv("TRADINGAGENTS_ENV_FILE", str(shared))
    monkeypatch.chdir(tmp_path)

    _prompt_key()

    assert stat.S_IMODE(shared.stat().st_mode) == 0o600
