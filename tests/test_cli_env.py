"""`tradingagents env`: which files are read, which keys they set, values hidden."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cli import main as cli_main


@pytest.mark.unit
def test_env_lists_the_shared_file_without_printing_values(monkeypatch, tmp_path):
    shared = tmp_path / "shared.env"
    shared.write_text(
        "OPENAI_API_KEY=sk-must-not-print\nTRADINGAGENTS_LLM_PROVIDER=openai\n"
    )
    monkeypatch.setenv("TRADINGAGENTS_ENV_FILE", str(shared))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-print")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli_main.app, ["env"])

    assert result.exit_code == 0, result.output
    assert str(shared) in result.output
    assert "OPENAI_API_KEY" in result.output
    assert "sk-must-not-print" not in result.output


@pytest.mark.unit
def test_env_reports_a_missing_shared_file(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli_main.app, ["env"])

    assert result.exit_code == 0, result.output
    assert "missing" in result.output
