"""Redis 序列行情解析单测（纯函数，不依赖网络）。"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.simulation.services import redis_series_quote as quote_mod
from backend.services.simulation.services.redis_series_quote import (
    candidate_series_keys,
    fetch_series_tick,
    fetch_snapshot_tick,
    fetch_snapshot_ticks,
    fetch_series_ticks,
    legacy_series_key_for,
    parse_series_member,
    series_key_for,
)


def test_series_key_uses_suffix_format():
    assert series_key_for("600036.SH") == "market:series:600036.SH"
    assert series_key_for("SH600036") == "market:series:600036.SH"
    assert series_key_for("0700.HK") == "market:series:0700.HK"
    assert series_key_for("HK00700") == "market:series:HK00700"
    assert series_key_for("AAPL") == "market:series:AAPL"
    assert series_key_for("not-a-code-!!!") is None


def test_legacy_series_key_for_old_prefix_compat():
    assert legacy_series_key_for("600036.SH") == "market:series:SH600036"
    assert legacy_series_key_for("SH600036") == "market:series:SH600036"
    # 非 A 股无新老之分
    assert legacy_series_key_for("AAPL") is None
    assert legacy_series_key_for("not-a-code-!!!") is None


def test_candidate_series_keys_new_first():
    assert candidate_series_keys("600036.SH") == [
        "market:series:600036.SH",
        "market:series:SH600036",
    ]
    assert candidate_series_keys("AAPL") == ["market:series:AAPL"]


def test_parse_fresh_tick():
    member = json.dumps(
        {"price": 40.9, "open": 40.5, "source": "remote_redis"},
        ensure_ascii=False,
    )
    tick = parse_series_member(member, 1_000_000.0, 1_000_100.0, 300)
    assert tick is not None
    assert tick["price"] == 40.9
    assert tick["age_s"] == 100.0
    assert tick["open"] == 40.5


def test_parse_stale_or_bad_tick_returns_none():
    member = json.dumps({"price": 40.9})
    # 超龄
    assert parse_series_member(member, 1_000_000.0, 1_000_400.0, 300) is None
    # 零价格
    assert (
        parse_series_member(json.dumps({"price": 0}), 1_000_000.0, 1_000_100.0, 300)
        is None
    )
    # 非法 JSON
    assert parse_series_member("not-json", 1_000_000.0, 1_000_100.0, 300) is None
    # 未来时间戳
    assert parse_series_member(member, 1_000_200.0, 1_000_100.0, 300) is None


@pytest.mark.asyncio
async def test_fetch_series_ticks_uses_latest_not_liquidity_window(monkeypatch):
    """最新价走 zrevrange+max_age；流动性窗口只影响 recent_volume。

    回归：曾用 60s zrangebyscore 兼作取价窗口，导致 >60s 无新 tick 的股票
    被当成缺失，权益结算回退到过期收盘价，总资产在成本价/市价间跳动。
    """
    now = 1_000_000.0
    monkeypatch.setattr(quote_mod.time, "time", lambda: now)

    latest_member = json.dumps({"price": 1.38, "volume": 100.0, "source": "t"})
    # 最新 tick 年龄 120s（超出默认流动性窗口 60s，但仍在 max_age 300s 内）
    latest_rows = [(latest_member, now - 120)]
    # 流动性窗口内无成交
    window_rows: list = []

    class FakePipe:
        def __init__(self) -> None:
            self.ops: list[str] = []

        def zrevrange(self, *_args, **_kwargs):
            self.ops.append("zrevrange")
            return self

        def zrangebyscore(self, *_args, **_kwargs):
            self.ops.append("zrangebyscore")
            return self

        async def execute(self):
            # 每个 symbol 两段：latest + window
            return [latest_rows, window_rows]

    pipe = FakePipe()
    client = MagicMock()
    client.pipeline.return_value = pipe
    monkeypatch.setattr(quote_mod, "_get_client", lambda: client)
    monkeypatch.setattr(
        "backend.shared.quote_redis_config.sim_redis_quote_max_age_sec",
        lambda: 300,
    )

    result = await fetch_series_ticks(["688121.SH"], volume_window_sec=60)
    assert pipe.ops == ["zrevrange", "zrangebyscore"]
    assert "688121.SH" in result
    assert result["688121.SH"]["price"] == 1.38
    assert result["688121.SH"]["age_s"] == 120.0
    assert result["688121.SH"]["recent_volume"] is None


@pytest.mark.asyncio
async def test_fetch_series_ticks_drops_stale_beyond_max_age(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(quote_mod.time, "time", lambda: now)
    stale_member = json.dumps({"price": 1.15, "volume": 1.0})
    latest_rows = [(stale_member, now - 400)]  # > max_age 300
    window_rows: list = []

    class FakePipe:
        def zrevrange(self, *_a, **_k):
            return self

        def zrangebyscore(self, *_a, **_k):
            return self

        async def execute(self):
            return [latest_rows, window_rows]

    client = MagicMock()
    client.pipeline.return_value = FakePipe()
    monkeypatch.setattr(quote_mod, "_get_client", lambda: client)
    monkeypatch.setattr(
        "backend.shared.quote_redis_config.sim_redis_quote_max_age_sec",
        lambda: 300,
    )

    result = await fetch_series_ticks(["688121.SH"])
    assert result == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_stale_suffix_falls_back_to_fresh_legacy(monkeypatch, batch):
    now = 1_000_000.0
    monkeypatch.setattr(quote_mod.time, "time", lambda: now)
    stale = [(json.dumps({"price": 10}), now - 400)]
    fresh = [(json.dumps({"price": 12}), now - 20)]
    client = MagicMock()
    client.zrevrange = AsyncMock(side_effect=[stale, fresh])
    first, second = MagicMock(), MagicMock()
    first.execute = AsyncMock(return_value=[stale, []])
    second.execute = AsyncMock(return_value=[fresh, []])
    client.pipeline.side_effect = [first, second]
    monkeypatch.setattr(quote_mod, "_get_client", lambda: client)

    if batch:
        tick = (await fetch_series_ticks(["603968.SH"], max_age_sec=300))["603968.SH"]
        second.zrevrange.assert_called_once_with(
            "market:series:SH603968", 0, 0, withscores=True
        )
    else:
        tick = await fetch_series_tick("603968.SH", max_age_sec=300)
        assert client.zrevrange.await_args_list[1].args[0] == "market:series:SH603968"
    assert tick["price"] == 12
    assert tick["age_s"] == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["stale", "future", "zero", "bad_timestamp"])
async def test_direct_snapshot_skips_invalid_suffix_and_checks_legacy(
    monkeypatch, invalid
):
    now = 1_000_000.0
    monkeypatch.setattr(quote_mod.time, "time", lambda: now)
    bad = {"Now": "10", "timestamp": str(now - 400)}
    if invalid == "future":
        bad["timestamp"] = str(now + 20)
    elif invalid == "zero":
        bad = {"Now": "0", "timestamp": str(now - 10)}
    elif invalid == "bad_timestamp":
        bad["timestamp"] = "invalid"
    fresh = {"Now": "12", "timestamp": str(now - 20), "Volume": "100"}
    pipe = MagicMock()
    pipe.execute = AsyncMock(return_value=[bad, fresh, {}, {}])
    client = MagicMock()
    client.pipeline.return_value = pipe
    monkeypatch.setattr(quote_mod, "_get_client", lambda: client)
    monkeypatch.setattr(
        "backend.shared.quote_redis_config.sim_redis_quote_max_age_sec", lambda: 300
    )
    tick = await fetch_snapshot_tick("603968.SH")
    assert tick["price"] == 12
    assert tick["age_s"] == 20
    assert tick["recent_volume"] is None
    assert tick["price_source"] == "redis_snapshot"


@pytest.mark.asyncio
async def test_direct_snapshot_rejects_missing_or_stale_data(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(quote_mod.time, "time", lambda: now)
    pipe = MagicMock()
    pipe.execute = AsyncMock(
        return_value=[{"Now": "12", "timestamp": str(now - 600)}, {}, {}, {}]
    )
    client = MagicMock()
    client.pipeline.return_value = pipe
    monkeypatch.setattr(quote_mod, "_get_client", lambda: client)
    monkeypatch.setattr(
        "backend.shared.quote_redis_config.sim_redis_quote_max_age_sec", lambda: 300
    )
    assert await fetch_snapshot_tick("603968.SH") is None


@pytest.mark.asyncio
async def test_batch_snapshots_keep_symbol_boundaries_and_freshness(monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(quote_mod.time, "time", lambda: now)
    monkeypatch.setattr(
        "backend.shared.quote_redis_config.sim_redis_quote_max_age_sec", lambda: 300
    )
    fresh = {"Now": "12", "timestamp": str(now - 20)}
    stale = {"Now": "40", "timestamp": str(now - 600)}
    pipe = MagicMock()
    # A 股每只 4 个候选键，美股 1 个；仅第一只与末尾美股有新鲜快照。
    pipe.execute = AsyncMock(return_value=[{}, fresh, {}, {}, stale, {}, {}, {}, fresh])
    client = MagicMock()
    client.pipeline.return_value = pipe
    monkeypatch.setattr(quote_mod, "_get_client", lambda: client)
    ticks = await fetch_snapshot_ticks(["603968.SH", "600036.SH", "AAPL", "603968.SH"])
    assert set(ticks) == {"603968.SH", "AAPL"}
    assert ticks["603968.SH"]["age_s"] == 20
    assert ticks["AAPL"]["price"] == 12
    client.pipeline.assert_called_once()
    assert pipe.hgetall.call_count == 9
