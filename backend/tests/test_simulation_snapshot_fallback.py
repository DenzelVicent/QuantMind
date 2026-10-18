"""Snapshot-only quotes must be executable without accepting historical daily bars."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.services.simulation.services import redis_series_quote
from backend.services.simulation.services.execution_engine import (
    SimulationExecutionEngine,
)
from backend.services.stream.market_app.services.quote_service import QuoteService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "age_seconds,accepted", [(20, True), (600, False), (None, False)]
)
async def test_snapshot_fallback_requests_live_source(
    monkeypatch, age_seconds, accepted
):
    monkeypatch.setattr(
        redis_series_quote, "fetch_series_ticks", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        redis_series_quote, "fetch_snapshot_tick", AsyncMock(return_value=None)
    )
    monkeypatch.setenv("SIM_REDIS_QUOTE_MAX_AGE_SEC", "300")
    data = {"current_price": 18.69, "data_source": "remote_redis"}
    if age_seconds is not None:
        data["timestamp"] = (
            datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        ).isoformat()

    def respond(request):
        assert request.url.params["source"] == "remote_redis"
        assert request.url.params["use_cache"] == "false"
        assert request.headers["X-User-Id"] == "1001"
        return httpx.Response(200, json=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        engine = SimulationExecutionEngine(
            db=SimpleNamespace(), manager=SimpleNamespace()
        )
        monkeypatch.setattr(engine, "_http_client", AsyncMock(return_value=client))
        snapshot = await engine._latest_price("603211.SH", user_id=1001)

    assert snapshot.price == (18.69 if accepted else 0)
    if not accepted:
        assert snapshot.price_source == "realtime_quote_unavailable"


@pytest.mark.asyncio
async def test_explicit_snapshot_source_bypasses_daily_cache_and_recent_record(
    monkeypatch,
):
    service = QuoteService(db=SimpleNamespace(), redis=SimpleNamespace())
    cache = AsyncMock(
        side_effect=AssertionError("must not read a different source cache")
    )
    recent = AsyncMock(
        side_effect=AssertionError("must not read a daily database record")
    )
    monkeypatch.setattr(service, "_get_cached_quote", cache)
    monkeypatch.setattr(service, "_get_recent_quote", recent)
    quote = {
        "symbol": "603211.SH",
        "timestamp": datetime.now(timezone.utc),
        "current_price": 18.69,
        "data_source": "remote_redis",
    }
    adapter = SimpleNamespace(fetch_quote=AsyncMock(return_value=quote))
    service.data_sources["remote_redis"] = adapter
    saved = SimpleNamespace(current_price=18.69)
    monkeypatch.setattr(service, "create_quote", AsyncMock(return_value=saved))
    monkeypatch.setattr(service, "_cache_quote", AsyncMock())

    result = await service.get_quote("603211.SH", source="remote_redis")

    assert result is saved
    adapter.fetch_quote.assert_awaited_once_with("603211.SH")
    cache.assert_not_awaited()
    recent.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_snapshot_does_not_require_http(monkeypatch):
    monkeypatch.setattr(
        redis_series_quote, "fetch_series_ticks", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        redis_series_quote,
        "fetch_snapshot_tick",
        AsyncMock(
            return_value={
                "price": 18.69,
                "timestamp": 1_000_000,
                "age_s": 20,
                "price_source": "redis_snapshot",
                "recent_volume": None,
            }
        ),
    )
    engine = SimulationExecutionEngine(db=SimpleNamespace(), manager=SimpleNamespace())
    monkeypatch.setattr(
        engine, "_enrich_cn_limits", lambda *_: (False, False, False, None, None)
    )
    http = AsyncMock(side_effect=AssertionError("direct snapshot must not need HTTP"))
    monkeypatch.setattr(engine, "_http_client", http)
    snapshot = await engine._latest_price("603211.SH", user_id=1001)
    assert snapshot.price == 18.69
    assert snapshot.price_source == "redis_snapshot"
    assert snapshot.quote_age_seconds == 20
    assert snapshot.recent_volume is None
    http.assert_not_awaited()
