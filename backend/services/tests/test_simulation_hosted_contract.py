import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from backend.services.simulation.engine import SimulationEngine
from backend.services.simulation.services import simulation_hosted_scheduler as hosted


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy_id", ["12", "sys_topk"])
async def test_strategy_parameters_and_template_identity_are_preserved(strategy_id):
    storage = MagicMock()
    storage.get = AsyncMock(
        return_value={
            "parameters": {
                "topk": 50,
                "n_drop": 10,
                "rebalance_days": 3,
                "deterministic_buy_order": True,
            }
        }
    )
    with patch(
        "backend.services.simulation.engine.get_strategy_storage_service",
        return_value=storage,
    ):
        config = await SimulationEngine()._load_strategy_config(
            None, strategy_id, "10000001"
        )
    assert storage.get.await_args.kwargs["strategy_id"] == (
        12 if strategy_id == "12" else "sys_topk"
    )
    assert (config.topk, config.n_drop, config.rebalance_days) == (50, 10, 3)
    assert config.deterministic_buy_order is True


@pytest.mark.asyncio
async def test_runtime_overrides_replace_persisted_dropout_and_flags():
    storage = MagicMock()
    storage.get = AsyncMock(return_value={"parameters": {"n_drop": 10}})
    with patch(
        "backend.services.simulation.engine.get_strategy_storage_service",
        return_value=storage,
    ):
        config = await SimulationEngine()._load_strategy_config(
            None,
            "12",
            "10000001",
            params_override={
                "n_drop": 0,
                "rebalance_days": 5,
                "enable_min_score": True,
            },
        )
    assert (config.n_drop, config.rebalance_days, config.enable_min_score) == (
        0,
        5,
        True,
    )


@pytest.mark.asyncio
async def test_hosted_cycle_passes_runtime_parameters_to_engine():
    run_cycle = AsyncMock(return_value=SimpleNamespace(error=None, order_count=0))
    with (
        patch.object(
            hosted,
            "_resolve_hosted_signal_run_id",
            AsyncMock(return_value=("signal_run", None)),
        ),
        patch(
            "backend.services.simulation.engine.simulation_engine.run_cycle", run_cycle
        ),
    ):
        await hosted.run_simulation_cycle_for_active(
            tenant_id="default",
            user_id="10000001",
            strategy_id="12",
            live_trade_config={"topk": 50, "n_drop": 10, "rebalance_days": 3},
        )
    assert run_cycle.await_args.kwargs["params_override"] == {
        "topk": 50,
        "n_drop": 10,
        "rebalance_days": 3,
    }


@pytest.mark.asyncio
async def test_bootstrap_cycle_denies_observe_only_before_signal_lookup():
    resolver = AsyncMock()
    with patch.object(hosted, "_resolve_hosted_signal_run_id", resolver):
        result = await hosted.run_simulation_cycle_for_active(
            tenant_id="default",
            user_id="10000001",
            strategy_id="12",
            live_trade_config={"trading_permission": "observe_only"},
        )
    assert result["status"] == "skipped"
    resolver.assert_not_awaited()


def _scheduler(payload):
    redis = MagicMock()
    redis.client.get.return_value = json.dumps(
        {
            "mode": "SIMULATION",
            "strategy_id": "12",
            "runtime_tenant_id": "default",
            "runtime_user_id": "10000001",
            **payload,
        }
    )
    redis.client.set.return_value = True
    return hosted.SimulationHostedScheduler(redis)


@pytest.mark.asyncio
async def test_repeat_poll_preserves_successful_job_and_does_not_execute_again():
    scheduler = _scheduler({})
    scheduler.redis.client.set.side_effect = [True, False]
    jobs = MagicMock()
    for name in (
        "ensure_job",
        "mark_ready",
        "mark_started",
        "mark_finished",
        "mark_skipped",
    ):
        setattr(jobs, name, AsyncMock())
    cycle = AsyncMock(return_value={"status": "succeeded", "filled_count": 1})
    now = datetime(2026, 6, 2, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))
    with (
        patch.object(hosted, "SimulationRebalanceJobService", jobs),
        patch.object(
            hosted,
            "_should_trigger",
            return_value=hosted.SimulationScheduleDecision(
                True, "ALL", "2026-06-02", "ready"
            ),
        ),
        patch.object(hosted, "run_simulation_cycle_for_active", cycle),
    ):
        assert await scheduler._process_key(
            "trade:active_strategy:default:10000001", now=now
        )
        assert not await scheduler._process_key(
            "trade:active_strategy:default:10000001", now=now
        )
    cycle.assert_awaited_once()
    for name in ("ensure_job", "mark_ready", "mark_started", "mark_finished"):
        getattr(jobs, name).assert_awaited_once()
    assert jobs.mark_finished.await_args.kwargs["status"] == "succeeded"
    jobs.mark_skipped.assert_not_awaited()
    scheduler.redis.client.delete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("lock_error", [False, True])
async def test_unacquired_lock_leaves_existing_job_untouched(lock_error):
    scheduler = _scheduler({})
    if lock_error:
        scheduler.redis.client.set.side_effect = RuntimeError("redis unavailable")
    else:
        scheduler.redis.client.set.return_value = False
    jobs = MagicMock()
    cycle = AsyncMock()
    with (
        patch.object(hosted, "SimulationRebalanceJobService", jobs),
        patch.object(
            hosted,
            "_should_trigger",
            return_value=hosted.SimulationScheduleDecision(
                True, "ALL", "2026-06-02", "ready"
            ),
        ),
        patch.object(hosted, "run_simulation_cycle_for_active", cycle),
    ):
        triggered = await scheduler._process_key(
            "trade:active_strategy:default:10000001",
            now=datetime.now(ZoneInfo("Asia/Shanghai")),
        )
    assert not triggered
    assert jobs.mock_calls == []
    cycle.assert_not_awaited()
    scheduler.redis.client.delete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"trading_permission": "observe_only"},
        {"trading_permission": "blocked"},
        {"execution_config": {"auto_trade_enabled": False}},
        {"live_trade_config": {"auto_trade_enabled": False}},
    ],
)
async def test_scheduler_denies_disabled_permissions_before_creating_jobs(payload):
    scheduler = _scheduler(payload)
    with patch.object(hosted, "_should_trigger") as schedule:
        triggered = await scheduler._process_key(
            "trade:active_strategy:default:10000001",
            now=datetime(2026, 6, 2, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    assert triggered is False
    schedule.assert_not_called()
    scheduler.redis.client.set.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"status": "failed", "error": "无可用信号"},
        {"status": "failed", "error": "账户不存在"},
        {"status": "failed"},
        {"status": "succeeded", "order_count": 0},
        {"status": "skipped", "error": "window_pending"},
    ],
)
async def test_job_status_matches_cycle_outcome(result):
    scheduler = _scheduler({})
    jobs = MagicMock()
    for name in (
        "ensure_job",
        "mark_ready",
        "mark_started",
        "mark_finished",
        "mark_skipped",
    ):
        setattr(jobs, name, AsyncMock())
    decision = hosted.SimulationScheduleDecision(True, "ALL", "2026-06-02", "ready")
    with (
        patch.object(hosted, "_should_trigger", return_value=decision),
        patch.object(hosted, "SimulationRebalanceJobService", jobs),
        patch.object(
            hosted, "run_simulation_cycle_for_active", AsyncMock(return_value=result)
        ),
    ):
        if result["status"] == "failed":
            with pytest.raises(RuntimeError):
                await scheduler._process_key(
                    "trade:active_strategy:default:10000001",
                    now=datetime.now(ZoneInfo("Asia/Shanghai")),
                )
            assert jobs.mark_finished.await_args.kwargs["status"] == "failed"
            scheduler.redis.client.delete.assert_called_once()
        else:
            triggered = await scheduler._process_key(
                "trade:active_strategy:default:10000001",
                now=datetime.now(ZoneInfo("Asia/Shanghai")),
            )
            assert triggered is (result["status"] == "succeeded")
            if result["status"] == "skipped":
                jobs.mark_skipped.assert_awaited_once()
                jobs.mark_finished.assert_not_awaited()
            else:
                assert jobs.mark_finished.await_args.kwargs["status"] == "succeeded"


@pytest.mark.asyncio
async def test_oss_parameter_precedence_preserves_code_fallback():
    storage = MagicMock()
    storage.get = AsyncMock(
        return_value={
            "parameters": {"topk": 30},
            "code": "STRATEGY_CONFIG = {'kwargs': {'topk': 50, 'n_drop': 10, 'rebalance_days': 3}}",
        }
    )
    with patch(
        "backend.services.simulation.engine.get_strategy_storage_service",
        return_value=storage,
    ):
        engine = SimulationEngine()
        config = await engine._load_strategy_config(None, "12", "10000001")
        overridden = await engine._load_strategy_config(
            None, "12", "10000001", params_override={"topk": 20, "n_drop": 0}
        )
    assert (config.topk, config.n_drop, config.rebalance_days) == (30, 10, 3)
    assert (overridden.topk, overridden.n_drop, overridden.rebalance_days) == (20, 0, 3)


@pytest.mark.asyncio
async def test_sync_hosted_sim_task_registers_hosted_sim_and_writes_stream():
    """托管模拟调仓必须登记为 hosted_sim（非 queued）并写实盘日志流。

    回归 Bug 2：模拟任务此前只写 simulation_rebalance_jobs，任务表 0 行、
    日志流无写入，导致 StrategyConsole 的「查看任务日志」永远空白。
    """
    created: dict = {}
    updated: dict = {}
    logs: list = []

    class _Persistence:
        async def create_task(self, **kwargs):
            created.update(kwargs)

        async def update_task(self, **kwargs):
            updated.update(kwargs)

    class _Stream:
        def append_log(self, **kwargs):
            logs.append(kwargs)

    with (
        patch(
            "backend.services.live_trading.services.manual_execution_persistence.manual_execution_persistence",
            _Persistence(),
        ),
        patch(
            "backend.services.live_trading.services.manual_execution_log_stream.manual_execution_log_stream",
            _Stream(),
        ),
    ):
        await hosted._sync_hosted_sim_task(
            task_id="hosted_sim_x",
            tenant_id="default",
            user_id="10000001",
            strategy_id="12",
            strategy_name="测试策略",
            live_trade_config={},
        )
        # 非 queued：手动执行 worker 只认领 queued，避免误跑实盘撮合
        assert created["task_type"] == "hosted_sim"
        assert created["status"] == "running"
        assert created["trading_mode"] == "SIMULATION"
        assert created["strategy_name"] == "测试策略"

        await hosted._sync_hosted_sim_task(
            task_id="hosted_sim_x",
            tenant_id="default",
            user_id="10000001",
            strategy_id="12",
            strategy_name="测试策略",
            live_trade_config={},
            result={
                "status": "succeeded",
                "signal_count": 5,
                "order_count": 3,
                "filled_count": 2,
                "rejected_count": 1,
            },
        )

    assert updated["status"] == "succeeded"
    assert updated["order_count"] == 3
    assert updated["success_count"] == 2
    assert updated["failed_count"] == 1
    assert len(logs) == 2
    assert all(entry["task_id"] == "hosted_sim_x" for entry in logs)


@pytest.mark.asyncio
async def test_sync_hosted_sim_task_skips_empty_task_id():
    """没有 run_id 时不得落库（bootstrap 之外的无 task 调用）。"""
    with patch(
        "backend.services.live_trading.services.manual_execution_persistence.manual_execution_persistence",
    ) as persistence:
        await hosted._sync_hosted_sim_task(
            task_id="",
            tenant_id="default",
            user_id="10000001",
            strategy_id="12",
            strategy_name="12",
            live_trade_config={},
        )
    persistence.create_task.assert_not_called()
