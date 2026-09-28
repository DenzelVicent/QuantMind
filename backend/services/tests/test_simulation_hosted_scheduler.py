from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from backend.services.live_trading.services import (
    manual_execution_service as mes_module,
)
from backend.services.simulation import engine as engine_module
from backend.services.simulation.services import (
    simulation_hosted_scheduler as scheduler,
)
from backend.services.simulation.services.signal_loader import SignalLoader
from backend.services.simulation.services.simulation_hosted_scheduler import (
    _next_scheduled_trigger,
    _normalize_live_trade_config,
    _should_trigger,
    hosted_cycle_ready,
)


def test_same_sell_buy_time_triggers_all_phase():
    cfg = _normalize_live_trade_config(
        {
            "schedule_type": "interval",
            "rebalance_days": 1,
            "enabled_sessions": ["PM"],
            "sell_time": "14:50",
            "buy_time": "14:50",
        }
    )

    decision = _should_trigger(
        now=datetime(2026, 6, 2, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai")),
        live_trade_config=cfg,
        started_day=None,
    )

    assert decision.should_trigger is True
    assert decision.phase == "ALL"
    assert decision.trade_date == "2026-06-02"


def test_same_sell_buy_time_only_triggers_on_configured_minute():
    cfg = _normalize_live_trade_config(
        {
            "schedule_type": "interval",
            "rebalance_days": 1,
            "enabled_sessions": ["AM"],
            "sell_time": "09:30",
            "buy_time": "09:30",
        }
    )

    decision = _should_trigger(
        now=datetime(2026, 6, 2, 10, 40, tzinfo=ZoneInfo("Asia/Shanghai")),
        live_trade_config=cfg,
        started_day=None,
    )

    assert decision.should_trigger is False
    assert decision.reason == "before_window"


def test_pm_config_does_not_trigger_at_am_open():
    cfg = _normalize_live_trade_config(
        {
            "schedule_type": "interval",
            "rebalance_days": 1,
            "enabled_sessions": ["PM"],
            "sell_time": "14:45",
            "buy_time": "14:50",
        }
    )

    decision = _should_trigger(
        now=datetime(2026, 6, 2, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        live_trade_config=cfg,
        started_day=None,
    )

    assert decision.should_trigger is False
    assert decision.reason == "outside_session"


def test_interval_schedule_uses_strategy_start_anchor():
    cfg = _normalize_live_trade_config(
        {
            "schedule_type": "interval",
            "rebalance_days": 3,
            "enabled_sessions": ["PM"],
            "sell_time": "14:45",
            "buy_time": "14:50",
        }
    )

    decision = _should_trigger(
        now=datetime(2026, 6, 3, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai")),
        live_trade_config=cfg,
        started_day=datetime(2026, 6, 2, tzinfo=ZoneInfo("Asia/Shanghai")).date(),
    )

    assert decision.should_trigger is False
    assert decision.reason == "interval_skip"


def test_weekly_schedule_respects_configured_weekday():
    cfg = _normalize_live_trade_config(
        {
            "schedule_type": "weekly",
            "trade_weekdays": ["TUE"],
            "enabled_sessions": ["PM"],
            "sell_time": "14:45",
            "buy_time": "14:50",
        }
    )

    decision = _should_trigger(
        now=datetime(2026, 6, 2, 14, 45, tzinfo=ZoneInfo("Asia/Shanghai")),
        live_trade_config=cfg,
        started_day=None,
    )

    assert decision.should_trigger is True
    assert decision.phase == "SELL"


def test_scheduler_skips_non_trading_day():
    cfg = _normalize_live_trade_config(
        {
            "schedule_type": "interval",
            "rebalance_days": 1,
            "enabled_sessions": ["PM"],
            "sell_time": "14:45",
            "buy_time": "14:50",
        }
    )

    decision = _should_trigger(
        now=datetime(2026, 6, 6, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai")),
        live_trade_config=cfg,
        started_day=None,
    )

    assert decision.should_trigger is False
    assert decision.reason == "non_trading_day"


def test_am_same_time_interval_follows_configured_trade_days():
    cfg = _normalize_live_trade_config(
        {
            "schedule_type": "interval",
            "rebalance_days": 3,
            "enabled_sessions": ["AM"],
            "sell_time": "09:30",
            "buy_time": "09:30",
            "sell_first": True,
            "order_type": "MARKET",
            "max_price_deviation": 0.02,
            "max_orders_per_cycle": 20,
        }
    )
    started_day = datetime(2026, 6, 3, tzinfo=ZoneInfo("Asia/Shanghai")).date()

    first_day = _should_trigger(
        now=datetime(2026, 6, 3, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        live_trade_config=cfg,
        started_day=started_day,
    )
    skipped_day = _should_trigger(
        now=datetime(2026, 6, 4, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        live_trade_config=cfg,
        started_day=started_day,
    )
    third_trade_day = _should_trigger(
        now=datetime(2026, 6, 8, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        live_trade_config=cfg,
        started_day=started_day,
    )

    assert first_day.should_trigger is True
    assert first_day.phase == "ALL"
    assert skipped_day.should_trigger is False
    assert skipped_day.reason == "interval_skip"
    assert third_trade_day.should_trigger is True
    assert third_trade_day.phase == "ALL"


def test_next_scheduled_trigger_skips_missed_window_and_returns_next_interval_day():
    cfg = _normalize_live_trade_config(
        {
            "schedule_type": "interval",
            "rebalance_days": 3,
            "enabled_sessions": ["AM"],
            "sell_time": "09:30",
            "buy_time": "09:30",
        }
    )

    next_trigger = _next_scheduled_trigger(
        now=datetime(2026, 6, 3, 13, 9, 46, tzinfo=ZoneInfo("Asia/Shanghai")),
        live_trade_config=cfg,
        started_day=datetime(2026, 6, 3, tzinfo=ZoneInfo("Asia/Shanghai")).date(),
    )

    assert next_trigger is not None
    assert next_trigger.phase == "ALL"
    assert next_trigger.trade_date == "2026-06-08"
    assert next_trigger.target_at.isoformat() == "2026-06-08T09:30:00+08:00"
    assert next_trigger.window_end_at.isoformat() == "2026-06-08T09:31:30+08:00"


def test_hosted_cycle_ready_skips_sell_only_window():
    assert hosted_cycle_ready("SELL") is False
    assert hosted_cycle_ready("BUY") is True
    assert hosted_cycle_ready("ALL") is True


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeDB:
    """最小 AsyncSession 替身：只记录 SQL 与参数，供批次解析断言使用。

    ``row`` 为 dict 时按 SQL 片段匹配返回不同结果，用于验证「默认模型批次
    优先、缺失时退化」的两段查询顺序。
    """

    def __init__(self, row):
        self.row = row
        self.sql = ""
        self.params = None
        self.sqls: list[str] = []

    async def execute(self, query, params=None):
        sql = str(query)
        self.sql = sql
        self.sqls.append(sql)
        self.params = params
        if isinstance(self.row, dict):
            for marker, value in self.row.items():
                if marker in sql:
                    return _FakeResult(value)
            return _FakeResult(None)
        return _FakeResult(self.row)


@pytest.mark.asyncio
async def test_resolve_effective_batch_only_picks_effective_trade_date():
    """trade_date 是生效日(T+1)：不得取到未来批次。"""
    db = _FakeDB(("run_effective",))

    got = await SignalLoader()._resolve_effective_batch(db, "default", "10000001")

    assert got == "run_effective"
    assert "trade_date <= :today" in db.sql
    assert db.params["today"] == datetime.now(ZoneInfo("Asia/Shanghai")).date()


@pytest.mark.asyncio
async def test_resolve_effective_batch_returns_none_when_all_batches_future():
    db = _FakeDB(None)

    assert await SignalLoader()._resolve_effective_batch(db, "default", "1") is None


@pytest.mark.asyncio
async def test_resolve_effective_batch_prefers_default_model_batch():
    """同一生效日多模型并存时，必须取默认模型的批次而非最近写入的那批。"""
    db = _FakeDB(
        {
            "qm_model_inference_runs": ("run_default_model",),
            "GROUP BY run_id": ("run_written_last",),
        }
    )

    got = await SignalLoader()._resolve_effective_batch(db, "default", "1")

    assert got == "run_default_model"
    assert len(db.sqls) == 1  # 首次查询即命中，不再退化


@pytest.mark.asyncio
async def test_resolve_effective_batch_falls_back_without_default_model_batch():
    db = _FakeDB(
        {
            "qm_model_inference_runs": None,
            "GROUP BY run_id": ("run_written_last",),
        }
    )

    got = await SignalLoader()._resolve_effective_batch(db, "default", "1")

    assert got == "run_written_last"
    assert len(db.sqls) == 2


@pytest.mark.asyncio
async def test_hosted_gate_rejects_unavailable_batch(monkeypatch):
    async def _unavailable(*, tenant_id, user_id):
        return {
            "available": False,
            "reason_code": "window_expired",
            "message": "结果已超过可执行窗口",
        }

    monkeypatch.setattr(
        mes_module.manual_execution_service,
        "get_default_model_hosted_status",
        _unavailable,
    )

    run_id, err = await scheduler._resolve_hosted_signal_run_id("default", "1")

    assert run_id is None
    assert err is not None and "window_expired" in err


@pytest.mark.asyncio
async def test_hosted_gate_binds_default_model_batch(monkeypatch):
    async def _ready(*, tenant_id, user_id):
        return {"available": True, "latest_run_id": "run_default_model_1"}

    monkeypatch.setattr(
        mes_module.manual_execution_service,
        "get_default_model_hosted_status",
        _ready,
    )

    assert await scheduler._resolve_hosted_signal_run_id("default", "1") == (
        "run_default_model_1",
        None,
    )


@pytest.mark.asyncio
async def test_run_cycle_skips_without_placing_orders_when_batch_missing(monkeypatch):
    """无可用批次时必须跳过，绝不能退回「取最新交易日全部信号」。"""

    async def _unavailable(*, tenant_id, user_id):
        return {
            "available": False,
            "reason_code": "missing_default_model",
            "message": "未找到默认模型",
        }

    monkeypatch.setattr(
        mes_module.manual_execution_service,
        "get_default_model_hosted_status",
        _unavailable,
    )

    calls: dict[str, Any] = {}

    class _ExplodingEngine:
        async def run_cycle(self, **kwargs):
            calls["kwargs"] = kwargs
            raise AssertionError("无可用批次时不得下单")

    monkeypatch.setattr(engine_module, "simulation_engine", _ExplodingEngine())

    result = await scheduler.run_simulation_cycle_for_active(
        tenant_id="default",
        user_id="10000001",
        strategy_id="12",
    )

    assert result["status"] == "skipped"
    assert "missing_default_model" in str(result["error"])
    assert "kwargs" not in calls


@pytest.mark.asyncio
async def test_run_cycle_passes_signal_run_id_to_engine(monkeypatch):
    async def _ready(*, tenant_id, user_id):
        return {"available": True, "latest_run_id": "run_bound_1"}

    monkeypatch.setattr(
        mes_module.manual_execution_service,
        "get_default_model_hosted_status",
        _ready,
    )

    calls: dict[str, Any] = {}

    class _FakeReport:
        run_id = "task_1"
        error = None
        signal_count = 3
        order_count = 1
        filled_count = 1

    class _FakeEngine:
        async def run_cycle(self, **kwargs):
            calls["kwargs"] = kwargs
            return _FakeReport()

    monkeypatch.setattr(engine_module, "simulation_engine", _FakeEngine())

    result = await scheduler.run_simulation_cycle_for_active(
        tenant_id="default",
        user_id="10000001",
        strategy_id="12",
        run_id="task_1",
    )

    assert result["status"] == "succeeded"
    assert calls["kwargs"]["signal_run_id"] == "run_bound_1"

