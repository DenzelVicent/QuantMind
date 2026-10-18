"""QuantDB dividend_factors → simulation_corporate_actions 每日同步。

数据流:
  QuantDB 凌晨自动更新 data/quantdb/3_financial_data/dividend_factors/*.parquet
  → 本模块按日同步窗口过滤后写入 simulation_corporate_actions (status=pending)
  → SimulationCorporateActionService.apply_due_actions() 到期应用到持仓/现金流

口径说明 (实测 000333.SZ / 600519.SH parquet 确认):
  - interest / stockBonus / stockGift / allotment 均为【每 10 股】口径
    (如茅台 2006-05-19 stockBonus=10 即 10送10), 入库前统一 ÷10
  - time 字段为公告/登记日, 早于实际除权日 0~5 个交易日; 分红提前入账仅使
    总资产短暂虚高, 除权日价格自然修正, 模拟盘可接受
  - 一条记录可同时含 分红+送转+配股, 拆成多条 action 分别应用
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from backend.services.simulation.models.corporate_action import (
    SimulationCorporateAction,
)
from backend.shared.database_manager_v2 import get_db_manager
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

# 数据目录: 容器内 QM_QUANTDB_DATA_DIR=/data/quantdb, 本地回退项目根 data/quantdb
_DATA_DIR = Path(
    os.getenv("QM_QUANTDB_DATA_DIR")
    or str(Path(__file__).resolve().parents[5] / "data" / "quantdb")
)

# 同步窗口: 回看 30 天(补最近除权的漏账) / 前看 120 天(覆盖公告期)
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_FORWARD_DAYS = 120


def _to_float(v) -> float:
    try:
        f = float(v)
        return 0.0 if pd.isna(f) else f
    except (TypeError, ValueError):
        return 0.0


def _collect_window_events(
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    forward_days: int = DEFAULT_FORWARD_DAYS,
    now: datetime | None = None,
) -> list[dict]:
    """读取 dividend_factors parquet, 返回同步窗口内的事件列表。"""
    now = now or datetime.now()
    start = now - timedelta(days=lookback_days)
    end = now + timedelta(days=forward_days)
    dividend_dir = _DATA_DIR / "3_financial_data" / "dividend_factors"
    if not dividend_dir.exists():
        logger.warning("dividend_factors 目录不存在: %s", dividend_dir)
        return []

    events: list[dict] = []
    files = sorted(dividend_dir.glob("*.parquet"))
    for f in files:
        try:
            df = pd.read_parquet(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 %s 失败: %s", f.name, exc)
            continue
        if df.empty or "time" not in df.columns:
            continue
        df = df[(df["time"] >= pd.Timestamp(start)) & (df["time"] <= pd.Timestamp(end))]
        if df.empty:
            continue
        symbol = StockCodeUtil.normalize(f.stem)
        for _, row in df.iterrows():
            interest = _to_float(row.get("interest"))
            bonus = _to_float(row.get("stockBonus"))
            gift = _to_float(row.get("stockGift"))
            allot = _to_float(row.get("allotment"))
            allot_price = _to_float(row.get("allotPrice"))
            ex_date = pd.Timestamp(row["time"]).to_pydatetime()
            if interest > 0:
                events.append(
                    {
                        "symbol": symbol,
                        "action_type": "dividend",
                        "ex_date": ex_date,
                        "cash_dividend_per_share": round(interest / 10.0, 6),
                        "share_ratio": 0.0,
                        "rights_price": 0.0,
                        "note": f"quantdb interest_per_10={interest}",
                    }
                )
            bonus_ratio = bonus + gift
            if bonus_ratio > 0:
                events.append(
                    {
                        "symbol": symbol,
                        "action_type": "bonus_share",
                        "ex_date": ex_date,
                        "cash_dividend_per_share": 0.0,
                        "share_ratio": round(bonus_ratio / 10.0, 6),
                        "rights_price": 0.0,
                        "note": f"quantdb bonus_per_10={bonus} gift_per_10={gift}",
                    }
                )
            if allot > 0 and allot_price > 0:
                events.append(
                    {
                        "symbol": symbol,
                        "action_type": "rights_issue",
                        "ex_date": ex_date,
                        "cash_dividend_per_share": 0.0,
                        "share_ratio": round(allot / 10.0, 6),
                        "rights_price": round(allot_price, 6),
                        "note": f"quantdb allotment_per_10={allot} allot_price={allot_price}",
                    }
                )
    return events


async def sync_corporate_actions_from_quantdb(
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    forward_days: int = DEFAULT_FORWARD_DAYS,
) -> int:
    """同步 dividend_factors → simulation_corporate_actions, 返回新插入条数。

    幂等: 按 (symbol, action_type, ex_date) 去重(不限 source, 防止与手工 CSV 重复入账)。
    """
    events = await asyncio.to_thread(
        _collect_window_events, lookback_days=lookback_days, forward_days=forward_days
    )
    if not events:
        logger.info("公司行为同步: 窗口内无事件")
        return 0

    inserted = 0
    db_manager = get_db_manager()
    async with db_manager.get_master_session() as session:
        # 一次取出窗口内已有记录做去重
        existing = set()
        symbols = sorted({e["symbol"] for e in events})
        rows = (
            await session.execute(
                select(
                    SimulationCorporateAction.symbol,
                    SimulationCorporateAction.action_type,
                    SimulationCorporateAction.ex_date,
                ).where(SimulationCorporateAction.symbol.in_(symbols))
            )
        ).all()
        existing = {(r[0], r[1], r[2]) for r in rows}

        for e in events:
            key = (e["symbol"], e["action_type"], e["ex_date"])
            if key in existing:
                continue
            existing.add(key)
            session.add(
                SimulationCorporateAction(
                    symbol=e["symbol"],
                    action_type=e["action_type"],
                    ex_date=e["ex_date"],
                    effective_date=None,
                    cash_dividend_per_share=e["cash_dividend_per_share"],
                    share_ratio=e["share_ratio"],
                    rights_price=e["rights_price"],
                    source="quantdb",
                    note=e["note"],
                    status="pending",
                )
            )
            inserted += 1
        await session.commit()

    logger.info(
        "公司行为同步: 窗口事件 %d 条, 新插入 %d 条 (回看 %d 天/前看 %d 天)",
        len(events),
        inserted,
        lookback_days,
        forward_days,
    )
    return inserted
