"""QuantDB Redis-DB-3 realtime snapshot client (server-side only)."""

from __future__ import annotations

import os
from typing import Any

import httpx

from backend.shared.stock_utils import StockCodeUtil


class QuantDBRealtimeUnavailable(RuntimeError):
    pass


async def get_snapshots(symbols: list[str]) -> dict[str, Any]:
    base_url = os.getenv("QUANTDB_REALTIME_BASE_URL", "").rstrip("/")
    secret = os.getenv("QUANTDB_INTERNAL_SERVICE_KEY", "")
    if not base_url or not secret:
        raise QuantDBRealtimeUnavailable("QuantDB realtime service is not configured")
    suffixes = list(dict.fromkeys(StockCodeUtil.to_suffix(symbol) for symbol in symbols if symbol))
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
            response = await client.get(
                f"{base_url}/api/v1/internal/realtime/snapshot",
                params={"symbols": ",".join(suffixes)},
                headers={"X-Internal-Service-Key": secret},
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise QuantDBRealtimeUnavailable("QuantDB realtime source unavailable") from exc

    items = []
    for quote in payload.get("items", []):
        row = dict(quote)
        row["symbol"] = StockCodeUtil.normalize(str(row.get("symbol") or ""))
        items.append(row)
    return {
        "items": items,
        "missing": [StockCodeUtil.normalize(str(s)) for s in payload.get("missing", [])],
        "source": "quantdb",
    }
