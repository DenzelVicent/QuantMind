"""模拟盘宇宙过滤回归：ST 剔除 + f_ 基本面约束透传与执行。"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.services.simulation.engine import (
    SimulationEngine,
    _extract_strategy_config_kwargs,
)
from backend.services.simulation.services.rebalance_calculator import StrategyConfig


def _sig(symbol: str, score: float = 1.0) -> SimpleNamespace:
    return SimpleNamespace(symbol=symbol, score=score)


def _market(value: str = "CN") -> SimpleNamespace:
    return SimpleNamespace(value=value)


def test_extract_passes_f_constraints_and_exclude_st():
    code = (
        "STRATEGY_CONFIG = {"
        '"kwargs": {"topk": 50, "f_total_mv_min": 3000000000.0,'
        ' "f_pe_ttm_max": 80, "exclude_st": False}}'
    )
    kwargs = _extract_strategy_config_kwargs(code)
    assert kwargs["topk"] == 50
    assert kwargs["f_total_mv_min"] == 3000000000.0
    assert kwargs["f_pe_ttm_max"] == 80
    assert kwargs["exclude_st"] is False


def test_extract_ignores_unrelated_keys():
    code = 'STRATEGY_CONFIG = {"kwargs": {"topk": 10, "signal": "<PRED>", "foo": 1}}'
    kwargs = _extract_strategy_config_kwargs(code)
    assert kwargs == {"topk": 10}


@pytest.mark.asyncio
async def test_universe_filter_drops_st_and_applies_constraints():
    engine = SimulationEngine()
    strategy = StrategyConfig(
        fundamental_constraints={"total_mv_min": 3_000_000_000.0},
        exclude_st=True,
    )
    signals = [_sig("600036.SH"), _sig("000001.SZ"), _sig("688001.SH")]

    fake_market_data = SimpleNamespace(st_symbols=lambda: frozenset({"000001.SZ"}))
    with (
        patch(
            "backend.services.simulation.services.local_market_data.get_local_market_data",
            return_value=fake_market_data,
        ),
        patch(
            "backend.shared.fundamental_aligner.fundamental_aligner.filter_instruments",
            side_effect=lambda _date, symbols, constraints: [
                s for s in symbols if s != "688001.SH"
            ],
        ),
    ):
        kept, st_dropped, f_dropped = await engine._apply_universe_filters(
            signals, strategy, market=_market()
        )

    assert [s.symbol for s in kept] == ["600036.SH"]
    assert st_dropped == 1
    assert f_dropped == 1


@pytest.mark.asyncio
async def test_universe_filter_fail_open_on_errors():
    engine = SimulationEngine()
    strategy = StrategyConfig(
        fundamental_constraints={"pe_ttm_max": 80}, exclude_st=True
    )
    signals = [_sig("600036.SH")]

    with (
        patch(
            "backend.services.simulation.services.local_market_data.get_local_market_data",
            side_effect=RuntimeError("boom"),
        ),
        patch(
            "backend.shared.fundamental_aligner.fundamental_aligner.filter_instruments",
            side_effect=RuntimeError("boom"),
        ),
    ):
        kept, st_dropped, f_dropped = await engine._apply_universe_filters(
            signals, strategy, market=_market()
        )

    assert [s.symbol for s in kept] == ["600036.SH"]
    assert (st_dropped, f_dropped) == (0, 0)


@pytest.mark.asyncio
async def test_universe_filter_noop_without_constraints():
    engine = SimulationEngine()
    strategy = StrategyConfig(fundamental_constraints={}, exclude_st=False)
    signals = [_sig("600036.SH")]
    kept, st_dropped, f_dropped = await engine._apply_universe_filters(
        signals, strategy, market=_market()
    )
    assert kept is signals
    assert (st_dropped, f_dropped) == (0, 0)
