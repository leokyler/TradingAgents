"""The shared dotenv file: where it lives, what it may fill, who wins."""

from __future__ import annotations

import os

import pytest

from tradingagents import env


@pytest.mark.unit
def test_shared_file_lives_beside_the_packages_other_user_state(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADINGAGENTS_ENV_FILE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert env.user_env_file() == tmp_path / ".tradingagents" / ".env"


@pytest.mark.unit
def test_shared_file_can_be_pointed_elsewhere(monkeypatch, tmp_path):
    target = tmp_path / "rendered-by-a-secrets-manager.env"
    monkeypatch.setenv("TRADINGAGENTS_ENV_FILE", str(target))

    assert env.user_env_file() == target


@pytest.mark.unit
def test_missing_shared_file_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_ENV_FILE", str(tmp_path / "absent.env"))

    assert env.load_user_env() is None


@pytest.mark.unit
def test_shared_file_only_fills_values_that_are_unset_or_blank(monkeypatch, tmp_path):
    shared = tmp_path / "shared.env"
    shared.write_text("KEPT_FROM_SHELL=shared\nFILLED=shared\nBLANK_FILLED=shared\n")
    monkeypatch.setenv("TRADINGAGENTS_ENV_FILE", str(shared))
    monkeypatch.setenv("KEPT_FROM_SHELL", "shell")
    monkeypatch.delenv("FILLED", raising=False)
    monkeypatch.setenv("BLANK_FILLED", "")

    assert env.load_user_env() == shared
    assert os.environ["KEPT_FROM_SHELL"] == "shell"
    assert os.environ["FILLED"] == "shared"
    # A blank key in a copied .env.example means "not configured", not "blank".
    assert os.environ["BLANK_FILLED"] == "shared"


@pytest.mark.unit
def test_project_file_wins_over_the_shared_file(monkeypatch, tmp_path):
    project = tmp_path / "checkout"
    project.mkdir()
    (project / ".env").write_text("TA_DEEP_MODEL=project\n")
    shared = tmp_path / "shared.env"
    shared.write_text("TA_DEEP_MODEL=shared\n")
    monkeypatch.setenv("TRADINGAGENTS_ENV_FILE", str(shared))
    monkeypatch.delenv("TA_DEEP_MODEL", raising=False)
    monkeypatch.chdir(project)

    # The package reads the project's file first, then the shared one.
    from dotenv import load_dotenv

    assert env.project_env_file() == project / ".env"
    load_dotenv(env.project_env_file())
    env.load_user_env()

    assert os.environ["TA_DEEP_MODEL"] == "project"


@pytest.mark.unit
def test_looks_like_setting_skips_ordinary_environment_variables():
    assert env.looks_like_setting("TRADINGAGENTS_LLM_PROVIDER")
    assert env.looks_like_setting("OPENAI_API_KEY")
    assert env.looks_like_setting("TUSHARE_TOKEN")
    assert env.looks_like_setting("OLLAMA_BASE_URL")
    assert not env.looks_like_setting("PATH")
    assert not env.looks_like_setting("HOME")
    # The pointer to the shared file is not one of the settings inside it.
    assert not env.looks_like_setting("TRADINGAGENTS_ENV_FILE")
