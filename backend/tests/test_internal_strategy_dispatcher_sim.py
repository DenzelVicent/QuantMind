"""手动/影子模拟下单应走统一 submit_and_fill（ledger + sim_orders/sim_trades）。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.live_trading.services.internal_strategy_dispatcher import (
    dispatch_internal_strategy_order,
)
from backend.services.simulation.services.order_submission_service import (
    SimulationSubmissionOutcome,
)


class _FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.mark.parametrize(
    "order_type,expected_type,expected_price",
    [(None, "limit", 10.5), ("MARKET", "market", None), ("LIMIT", "limit", 10.5)],
)
def test_simulation_dispatch_uses_submit_and_fill_not_hand_insert(
    order_type, expected_type, expected_price
):
    async def _run():
        db = MagicMock()
        db.execute = AsyncMock(return_value=_FakeResult(None))
        redis = MagicMock()
        outcome = SimulationSubmissionOutcome(
            success=True,
            order_id="ord-1",
            fill_price=10.5,
            filled_quantity=100,
            commission=1.2,
            price_source="quote",
            message="filled",
        )
        submit = AsyncMock(return_value=outcome)
        submission = MagicMock()
        submission.submit_and_fill = submit

        with (
            patch(
                "backend.services.simulation.services.order_submission_service."
                "SimulationOrderSubmissionService",
                return_value=submission,
            ),
            patch(
                "backend.services.live_trading.services.internal_strategy_dispatcher."
                "mirror_virtual_fill",
                new_callable=AsyncMock,
            ) as mirror,
        ):
            result = await dispatch_internal_strategy_order(
                order_data={
                    "trading_mode": "SIMULATION",
                    "symbol": "SH600036",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 10.5,
                    "order_type": order_type,
                    "client_order_id": "manual-abc",
                    "remarks": "manual task",
                },
                user_id="1",
                tenant_id="default",
                redis=redis,
                db=db,
            )

        assert result["status"] == "success"
        assert result["execution"] == "virtual"
        assert result["order_id"] == "ord-1"
        assert result["result"]["success"] is True
        submit.assert_awaited_once()
        kwargs = submit.await_args.kwargs
        assert kwargs["order_type"] == expected_type
        assert kwargs["price"] == expected_price
        assert kwargs["client_order_id"] == "manual-abc"
        assert kwargs["trigger_source"] == "manual"
        assert str(kwargs["remarks"]).startswith("client_order_id=manual-abc")
        mirror.assert_awaited_once()

    asyncio.run(_run())


def test_simulation_dispatch_skips_duplicate_remark():
    async def _run():
        db = MagicMock()
        db.execute = AsyncMock(return_value=_FakeResult("existing-order"))
        redis = MagicMock()
        with patch(
            "backend.services.simulation.services.order_submission_service."
            "SimulationOrderSubmissionService",
        ) as ctor:
            result = await dispatch_internal_strategy_order(
                order_data={
                    "trading_mode": "SIMULATION",
                    "symbol": "SH600036",
                    "side": "BUY",
                    "quantity": 100,
                    "price": 10.5,
                    "client_order_id": "manual-dup",
                },
                user_id="1",
                tenant_id="default",
                redis=redis,
                db=db,
            )
        assert result["execution"] == "duplicate_skipped"
        ctor.assert_not_called()

    asyncio.run(_run())
