"""Tests for the keyless A-share news vendor (cn_news): gating, parsing, point-in-time."""

import ast
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from tradingagents.dataflows.config import run_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorUnavailableError,
)
from tradingagents.dataflows.vendors import cn_news

_ROOT = Path(__file__).resolve().parents[1]

_TICKER_ROWS = [
    {
        "关键词": "515070",
        "新闻标题": "AI 算力回暖",
        "新闻内容": "摘要一",
        "发布时间": "2026-11-11 13:55:15",
        "文章来源": "每日经济新闻",
        "新闻链接": "http://finance.eastmoney.com/a/1.html",
    },
    {
        "关键词": "515070",
        "新闻标题": "AI 算力回调",
        "新闻内容": "摘要二",
        "发布时间": "2026-11-10 09:05:00",
        "文章来源": "证券时报",
        "新闻链接": "http://finance.eastmoney.com/a/2.html",
    },
    {
        "关键词": "515070",
        "新闻标题": "更早的一篇",
        "新闻内容": "摘要三",
        "发布时间": "2026-11-09 08:00:00",
        "文章来源": "证券时报",
        "新闻链接": "http://finance.eastmoney.com/a/3.html",
    },
]

_GLOBAL_ROWS = [
    {"标题": "央行开展逆回购", "摘要": "摘要一", "发布时间": "2026-11-12 09:30:00", "链接": "http://x/1"},
    {"标题": "央行开展逆回购", "摘要": "重复", "发布时间": "2026-11-11 09:30:00", "链接": "http://x/dup"},
    {"标题": "制造业数据", "摘要": "摘要二", "发布时间": "2026-11-10 10:00:00", "链接": "http://x/2"},
    {"标题": "未来快讯", "摘要": "未来", "发布时间": "2026-11-20 10:00:00", "链接": "http://x/future"},
    # 02:00 China time the day after as_of is after the analysis date, even
    # though it is still before midnight UTC of that day.
    {"标题": "次日凌晨快讯", "摘要": "凌晨", "发布时间": "2026-11-13 02:00:00", "链接": "http://x/early"},
]


def _fake_akshare(monkeypatch, **functions) -> types.SimpleNamespace:
    """Serve a stand-in akshare module, so no endpoint is reached."""
    module = types.SimpleNamespace(__version__="test", **functions)
    monkeypatch.setattr(cn_news, "_import_akshare", lambda: module)
    return module


def _forbid_akshare(monkeypatch) -> None:
    """Fail the test if the vendor touches akshare at all."""

    def forbidden():
        raise AssertionError("cn_news imported akshare for a symbol it does not serve")

    monkeypatch.setattr(cn_news, "_import_akshare", forbidden)


@pytest.mark.unit
def test_akshare_is_imported_lazily():
    """An optional dependency must not arrive with the module import."""
    source = (_ROOT / "tradingagents/dataflows/vendors/cn_news.py").read_text(encoding="utf-8")
    module_level = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            module_level.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_level.append(node.module)
    assert not [name for name in module_level if name.split(".")[0] == "akshare"]
    # A fresh interpreter proves the import is lazy even when another test in
    # this process has already imported akshare.
    probe = (
        "import sys, tradingagents.dataflows.vendors.cn_news; "
        "print('AKSHARE=' + str('akshare' in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=_ROOT, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert "AKSHARE=False" in result.stdout


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["AAPL", "0700.HK", "BTC-USD", "GC=F", "ABC.SS"])
def test_get_news_gates_non_a_share_symbols(monkeypatch, ticker):
    _forbid_akshare(monkeypatch)
    with pytest.raises(VendorNotConfiguredError):
        cn_news.get_news(ticker, "2026-11-01", "2026-11-14")


@pytest.mark.unit
def test_get_news_queries_six_digit_code_and_resolves_shanghai(monkeypatch):
    calls = []

    def fake_news(symbol):
        calls.append(symbol)
        return pd.DataFrame(_TICKER_ROWS[:1])

    _fake_akshare(monkeypatch, stock_news_em=fake_news)
    out = cn_news.get_news("515070.SH", "2026-11-09", "2026-11-12")
    assert calls == ["515070"]
    assert out.startswith("## 515070.SH News, from 2026-11-09 to 2026-11-12:\n\n")


@pytest.mark.unit
def test_get_news_serves_beijing_listing(monkeypatch):
    _fake_akshare(monkeypatch, stock_news_em=lambda symbol: pd.DataFrame(_TICKER_ROWS[:1]))
    out = cn_news.get_news("830799.BJ", "2026-11-09", "2026-11-12")
    assert "## 830799.BJ News, from 2026-11-09 to 2026-11-12:" in out


@pytest.mark.unit
def test_get_news_without_akshare_is_not_configured(monkeypatch):
    # A real lazy import of a missing module: the sentinel makes ``import akshare``
    # raise ImportError, so the vendor's own handler is what answers.
    monkeypatch.setitem(sys.modules, "akshare", None)
    with pytest.raises(VendorNotConfiguredError) as excinfo:
        cn_news.get_news("515070.SS", "2026-11-09", "2026-11-12")
    message = str(excinfo.value)
    assert "not configured" in message
    assert 'pip install "tradingagents[cn]"' in message


@pytest.mark.unit
def test_get_news_formats_articles_like_yahoo(monkeypatch):
    _fake_akshare(monkeypatch, stock_news_em=lambda symbol: pd.DataFrame(_TICKER_ROWS))
    with run_config({"news_article_limit": 2}):
        out = cn_news.get_news("515070.SS", "2026-11-09", "2026-11-12")
    assert "### AI 算力回暖 (source: 每日经济新闻)" in out
    assert "Published: 2026-11-11 13:55" in out
    assert "摘要一" in out
    assert "Link: http://finance.eastmoney.com/a/1.html" in out
    assert "AI 算力回调" in out
    # Newest first, capped at news_article_limit.
    assert "更早的一篇" not in out


@pytest.mark.unit
def test_publish_time_is_read_as_shanghai_local(monkeypatch):
    published = cn_news._published({"发布时间": "2026-11-11 08:30:00"})
    assert published == datetime(2026, 11, 11, 0, 30, tzinfo=UTC)


@pytest.mark.unit
def test_get_news_drops_articles_outside_the_window(monkeypatch):
    rows = [
        {"新闻标题": "窗口内", "新闻内容": "在窗口", "发布时间": "2026-11-11 10:00:00",
         "文章来源": "A", "新闻链接": "http://x/in"},
        {"新闻标题": "未来", "新闻内容": "未来", "发布时间": "2026-11-12 10:00:00",
         "文章来源": "A", "新闻链接": "http://x/future"},
        {"新闻标题": "过去", "新闻内容": "过去", "发布时间": "2026-11-01 10:00:00",
         "文章来源": "A", "新闻链接": "http://x/past"},
        # 03:00 China time on the day after end is after the analysis date,
        # even though it is still 19:00 UTC of the end day.
        {"新闻标题": "次日凌晨", "新闻内容": "凌晨", "发布时间": "2026-11-12 03:00:00",
         "文章来源": "A", "新闻链接": "http://x/early"},
    ]
    _fake_akshare(monkeypatch, stock_news_em=lambda symbol: pd.DataFrame(rows))
    out = cn_news.get_news("515070.SS", "2026-11-09", "2026-11-11")
    assert "窗口内" in out
    assert "未来" not in out
    assert "过去" not in out
    assert "次日凌晨" not in out
    assert "Published: 2026-11-11 10:00" in out


@pytest.mark.unit
def test_get_news_drops_undated_articles(monkeypatch):
    rows = [
        {"新闻标题": "没有时间", "新闻内容": "不该被当作今天", "发布时间": "",
         "文章来源": "A", "新闻链接": "http://x/undated"},
    ]
    _fake_akshare(monkeypatch, stock_news_em=lambda symbol: pd.DataFrame(rows))
    with pytest.raises(NoMarketDataError):
        cn_news.get_news("515070.SS", "2026-11-09", "2026-11-11")


@pytest.mark.unit
def test_get_news_does_not_leak_next_china_day(monkeypatch):
    rows = [
        {"新闻标题": "当日收盘后", "新闻内容": "保留", "发布时间": "2026-11-13 15:30:00",
         "文章来源": "A", "新闻链接": "http://x/kept"},
        {"新闻标题": "次日早盘前", "新闻内容": "丢弃", "发布时间": "2026-11-14 07:59:00",
         "文章来源": "A", "新闻链接": "http://x/leak"},
    ]
    _fake_akshare(monkeypatch, stock_news_em=lambda symbol: pd.DataFrame(rows))
    out = cn_news.get_news("515070.SS", "2026-11-11", "2026-11-13")
    assert "当日收盘后" in out
    assert "Published: 2026-11-13 15:30" in out
    assert "次日早盘前" not in out


@pytest.mark.unit
def test_global_news_does_not_leak_next_china_day(monkeypatch):
    rows = [
        {"标题": "当日收盘后", "摘要": "保留", "发布时间": "2026-11-13 15:30:00", "链接": "http://x/kept"},
        {"标题": "次日早盘前", "摘要": "丢弃", "发布时间": "2026-11-14 07:59:00", "链接": "http://x/leak"},
    ]
    _fake_akshare(monkeypatch, stock_info_global_em=lambda: pd.DataFrame(rows))
    out = cn_news.get_global_news("2026-11-13", 3, 5, "515070.SS")
    assert "当日收盘后" in out
    assert "Published: 2026-11-13 15:30" in out
    assert "次日早盘前" not in out


@pytest.mark.unit
def test_get_news_empty_window_raises_no_market_data(monkeypatch):
    empty = pd.DataFrame(columns=["新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"])
    _fake_akshare(monkeypatch, stock_news_em=lambda symbol: empty)
    with pytest.raises(NoMarketDataError):
        cn_news.get_news("515070.SS", "2026-11-09", "2026-11-11")


@pytest.mark.unit
def test_get_news_request_failure_is_vendor_unavailable(monkeypatch):
    def broken(symbol):
        raise RuntimeError("connection reset by peer")

    _fake_akshare(monkeypatch, stock_news_em=broken)
    with pytest.raises(VendorUnavailableError) as excinfo:
        cn_news.get_news("515070.SS", "2026-11-09", "2026-11-11")
    assert "connection reset by peer" in str(excinfo.value)


@pytest.mark.unit
def test_get_news_unreadable_table_is_vendor_unavailable(monkeypatch):
    class BrokenFrame:
        def to_dict(self, orient):
            raise ValueError("column layout changed")

    _fake_akshare(monkeypatch, stock_news_em=lambda symbol: BrokenFrame())
    with pytest.raises(VendorUnavailableError) as excinfo:
        cn_news.get_news("515070.SS", "2026-11-09", "2026-11-11")
    assert "unreadable" in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.parametrize("symbol", [None, "AAPL", "0700.HK", "BTC-USD"])
def test_global_news_gates_non_a_share_runs(monkeypatch, symbol):
    _forbid_akshare(monkeypatch)
    with pytest.raises(VendorNotConfiguredError) as excinfo:
        cn_news.get_global_news("2026-11-12", 7, 5, symbol)
    assert "A-share" in str(excinfo.value)


@pytest.mark.unit
def test_global_news_formats_flashes_in_window(monkeypatch):
    calls = []

    def fake_global():
        calls.append(True)
        return pd.DataFrame(_GLOBAL_ROWS)

    _fake_akshare(monkeypatch, stock_info_global_em=fake_global)
    out = cn_news.get_global_news("2026-11-12", 7, 5, "515070.SS")
    assert calls == [True]
    assert out.startswith("## Global Market News, from 2026-11-05 to 2026-11-12:\n\n")
    assert "### 央行开展逆回购 (source: Eastmoney)" in out
    assert "Published: 2026-11-12 09:30" in out
    assert "摘要一" in out
    assert "Link: http://x/1" in out
    assert "摘要二" in out
    # The duplicate headline is served once; the future flash is not served.
    assert out.count("### 央行开展逆回购") == 1
    assert "重复" not in out
    assert "未来快讯" not in out
    assert "次日凌晨快讯" not in out


@pytest.mark.unit
def test_global_news_respects_limit(monkeypatch):
    _fake_akshare(
        monkeypatch,
        stock_info_global_em=lambda: pd.DataFrame(_GLOBAL_ROWS),
    )
    out = cn_news.get_global_news("2026-11-12", 7, 1, "515070.SS")
    assert "央行开展逆回购" in out
    assert "制造业数据" not in out


@pytest.mark.unit
def test_global_news_zero_limit_raises_no_market_data(monkeypatch):
    _fake_akshare(monkeypatch, stock_info_global_em=lambda: pd.DataFrame(_GLOBAL_ROWS))
    with pytest.raises(NoMarketDataError):
        cn_news.get_global_news("2026-11-12", 7, 0, "515070.SS")


@pytest.mark.unit
def test_global_news_falls_back_to_cls_feed(monkeypatch):
    rows = [
        {"标题": "财联社快讯", "内容": "内容一", "发布日期": "2026-11-12", "发布时间": "11:15:52"},
    ]
    _fake_akshare(monkeypatch, stock_news_global_cls=lambda: pd.DataFrame(rows))
    out = cn_news.get_global_news("2026-11-12", 7, 5, "515070.SS")
    assert "### 财联社快讯 (source: CLS)" in out
    assert "Published: 2026-11-12 11:15" in out
    assert "内容一" in out
    # The CLS feed links no article, so the block carries no Link line.
    assert "Link:" not in out


@pytest.mark.unit
def test_global_news_without_a_known_feed_is_vendor_unavailable(monkeypatch):
    _fake_akshare(monkeypatch)
    monkeypatch.setattr(cn_news, "_search_rows", lambda *a, **k: [])
    with pytest.raises(VendorUnavailableError):
        cn_news.get_global_news("2026-11-12", 7, 5, "515070.SS")


@pytest.mark.unit
def test_global_news_empty_window_raises_no_market_data(monkeypatch):
    empty = pd.DataFrame(columns=["标题", "摘要", "发布时间", "链接"])
    _fake_akshare(monkeypatch, stock_info_global_em=lambda: empty)
    monkeypatch.setattr(cn_news, "_search_rows", lambda *a, **k: [])
    with pytest.raises(NoMarketDataError):
        cn_news.get_global_news("2026-11-12", 7, 5, "515070.SS")


@pytest.mark.unit
def test_global_news_request_failure_is_vendor_unavailable(monkeypatch):
    def broken():
        raise RuntimeError("timed out")

    _fake_akshare(monkeypatch, stock_info_global_em=broken)
    monkeypatch.setattr(cn_news, "_search_rows", lambda *a, **k: [])
    with pytest.raises(VendorUnavailableError) as excinfo:
        cn_news.get_global_news("2026-11-12", 7, 5, "515070.SS")
    assert "timed out" in str(excinfo.value)


@pytest.mark.unit
def test_global_news_falls_back_to_the_dated_eastmoney_search(monkeypatch):
    """The 7x24 flash list keeps only the latest hours; a historical run reads the
    same search index the ticker feed uses, still filtered to the China-local window."""
    empty = pd.DataFrame(columns=["标题", "摘要", "发布时间", "链接"])
    _fake_akshare(monkeypatch, stock_info_global_em=lambda: empty)
    rows = [
        {"新闻标题": "旧闻", "新闻内容": "x", "发布时间": "2026-11-12 15:30:00",
         "文章来源": "每日经济新闻", "新闻链接": "http://example/old"},
        {"新闻标题": "未来", "新闻内容": "y", "发布时间": "2026-11-13 07:59:00",
         "文章来源": "每日经济新闻", "新闻链接": "http://example/future"},
    ]
    monkeypatch.setattr(cn_news, "_search_rows", lambda *a, **k: rows)

    out = cn_news.get_global_news("2026-11-12", 7, 5, "515070.SS")

    assert "旧闻" in out
    assert "未来" not in out
    assert "Published: 2026-11-12 15:30" in out


@pytest.mark.unit
def test_a_missing_akshare_warns_at_cli_startup(monkeypatch, capsys):
    from cli import run
    from tradingagents.dataflows import units
    from tradingagents.dataflows.vendors import cn_news

    monkeypatch.setattr(cn_news, "missing_dependency", lambda: "akshare is not installed")
    monkeypatch.setattr(units, "is_chinese", lambda language=None: True)
    config = {"data_vendors": {"news_data": "cn_news,yfinance"}, "output_language": "中文"}

    run._warn_if_cn_news_missing(
        {"asset_type": "etf", "output_language": "中文"}, config
    )
    out = capsys.readouterr().out
    flat = " ".join(out.split())
    assert "cn_news 不可用" in flat
    assert "yfinance" in flat
    assert 'pip install "tradingagents[cn]"' in flat   # rich must not eat [cn]

    run._warn_if_cn_news_missing(
        {"asset_type": "etf", "output_language": "中文"},
        {"data_vendors": {"news_data": "cn_news,alpha_vantage"}},
    )
    assert "alpha_vantage" in capsys.readouterr().out

    run._warn_if_cn_news_missing(
        {"asset_type": "etf", "output_language": "中文"},
        {"data_vendors": {"news_data": "yfinance"}},
    )
    run._warn_if_cn_news_missing({"asset_type": "stock", "output_language": "中文"}, config)
    assert capsys.readouterr().out == ""
