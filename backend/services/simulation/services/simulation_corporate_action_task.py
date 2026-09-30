"""模拟盘公司行为每日任务（QuantDB 数据源, 开盘前同步并应用）。

QuantDB 每日凌晨自动更新 dividend_factors；本任务在每交易日 08:30 后：
  1. 同步窗口内公司行为 → simulation_corporate_actions (status=pending)
  2. apply_due_actions() 把到期事件应用到持仓/现金流/账户投影

时序: 08:30 (数据已更新、开盘前) → 09:16 T+1 解锁 → 09:30 开盘,
除权事件在开盘前完成入账, 盘中调仓看到的就是除权后口径。
进程重启会补跑当日任务; 同步与 apply 均幂等。
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.services.simulation.services.corporate_action_quantdb_sync import (
    sync_corporate_actions_from_quantdb,
)
from backend.services.simulation.services.corporate_action_service import (
    SimulationCorporateActionService,
)

logger = logging.getLogger(__name__)

_SH_TZ = ZoneInfo("Asia/Shanghai")

_SYNC_HOUR = 8
_SYNC_MINUTE = 30
_CHECK_INTERVAL_SECONDS = 60


def _is_cn_trade_date(day) -> bool:
    """XSHG 交易日历判定，失败回退 weekday（与 T+1 解锁任务同口径）。"""
    try:
        import pandas as pd
        from exchange_calendars import get_calendar

        return bool(get_calendar("XSHG").is_session(pd.Timestamp(day)))
    except Exception:
        return day.weekday() < 5


async def run_simulation_corporate_action_task(
    interval_seconds: int = _CHECK_INTERVAL_SECONDS,
) -> None:
    """每交易日 08:30 后同步 QuantDB 公司行为并应用到模拟盘账户（幂等）。"""
    last_date = ""
    while True:
        try:
            # 上海墙钟：容器时区不确定时 naive now() 会让 08:30 误触发。
            now = datetime.now(_SH_TZ)
            today = now.strftime("%Y%m%d")
            if today != last_date and (now.hour, now.minute) >= (
                _SYNC_HOUR,
                _SYNC_MINUTE,
            ):
                last_date = today
                if not _is_cn_trade_date(now.date()):
                    continue  # 非交易日（周末/节假日）不同步不应用
                inserted = await sync_corporate_actions_from_quantdb()
                applied = await SimulationCorporateActionService.apply_due_actions()
                if inserted or applied:
                    logger.info(
                        "模拟盘公司行为: 同步 %d 条, 应用 %d 条", inserted, applied
                    )
        except Exception as exc:
            logger.warning("模拟盘公司行为任务异常: %s", exc, exc_info=True)
        await asyncio.sleep(interval_seconds)
