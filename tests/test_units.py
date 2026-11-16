"""Unit wording follows the run's output language, once, for every block."""

import copy

import pytest

import tradingagents.dataflows.config as config_module
from tradingagents.dataflows import units
from tradingagents.dataflows.config import set_config


@pytest.fixture(autouse=True)
def _restore_config():
    original = copy.deepcopy(config_module._config)
    yield
    config_module._config = original


@pytest.mark.unit
def test_chinese_output_uses_the_chinese_units():
    set_config({"output_language": "中文"})
    assert units.lots(1e4) == "1.00 万手"
    assert units.shares(1e8) == "1.00 亿份"
    assert units.money(1e8) == "1.00 亿元"


@pytest.mark.unit
def test_english_output_uses_the_neutral_units():
    set_config({"output_language": "English"})
    assert units.lots(1e4) == "1.00 10k lots"
    assert units.shares(1e8) == "1.00 100m shares"
    assert units.money(1e8) == "CNY 1.00 100m"
