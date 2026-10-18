"""模拟盘持仓资源。"""

from __future__ import annotations

from typing import Any


class PaperResource:
    """模拟盘持仓查询（scope: paper.read）。"""

    VALID_MARKETS = ("CN", "HK", "US", "FUTURES", "CRYPTO")

    def __init__(self, client):
        self._c = client

    def get_positions(self, market: str = "CN") -> dict[str, Any]:
        """模拟盘账户（含持仓）。返回 {cash, total_asset, market_value,
        positions, position_count, total_pnl, today_pnl, ...}。
        未初始化时 positions={} 且 account_not_initialized=True。"""
        market = (market or "CN").upper()
        if market not in self.VALID_MARKETS:
            raise ValueError(f"market 须为 {self.VALID_MARKETS} 之一，当前: {market}")
        data = self._c._request(
            "GET", "/api/v1/open/paper/positions", params={"market": market}
        )
        # 服务端包两层：open 包络 data = trade 侧 {success, data}，拍平一层方便使用
        if isinstance(data, dict) and "data" in data and "success" in data:
            inner = data.get("data") or {}
            inner["_market"] = data.get("market", market)
            return inner
        return data
