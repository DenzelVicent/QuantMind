from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade_shared.models.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TradeAction,
    TradingMode,
)
from backend.services.trade_shared.models.order import Order
from backend.services.trade_shared.portfolio.models import Portfolio
from backend.services.trade_shared.redis_client import RedisClient
from backend.services.trade_shared.schemas.order import OrderCreate
from backend.services.trade_shared.services.order_service import OrderService
from backend.services.trade_shared.simulation_manager import SimulationAccountManager
from backend.services.live_trading.services.trading_engine import TradingEngine
from backend.services.live_trading.routers.real_trading_utils import (
    _fetch_active_portfolio_snapshot,
)
from backend.services.live_trading.services.real_mirror_service import (
    mirror_virtual_fill,
)

logger = logging.getLogger(__name__)

_TRADE_ACTION_ALIAS = {
    "open": "buy_to_open",
    "buy_open": "buy_to_open",
    "buy_to_open": "buy_to_open",
    "close": "sell_to_close",
    "sell_close": "sell_to_close",
    "sell_to_close": "sell_to_close",
    "short": "sell_to_open",
    "sell_open": "sell_to_open",
    "sell_to_open": "sell_to_open",
    "cover": "buy_to_close",
    "buy_close": "buy_to_close",
    "buy_to_close": "buy_to_close",
}


def _normalize_trade_action(raw: Any) -> str | None:
    value = str(getattr(raw, "value", raw) or "").strip().lower()
    return _TRADE_ACTION_ALIAS.get(value, value) or None


async def dispatch_internal_strategy_order(
    *,
    order_data: dict[str, Any],
    user_id: str,
    tenant_id: str,
    redis: RedisClient,
    db: AsyncSession,
) -> dict[str, Any]:
    """复用内部策略下单逻辑：实盘走真实风控/柜台，影子/模拟走虚拟成交。"""
    # user_id 口径与模拟盘接口（simulation.py _require_user_id）对齐：
    # 管理员族收口 10000001，避免 int("00000001")==1 读到空账。
    from backend.services.trade_shared.simulation_manager import canonical_sim_uid

    _uid_raw = str(user_id or "").strip()
    uid = canonical_sim_uid(_uid_raw)
    tenant = (tenant_id or "").strip() or "default"
    trading_mode_raw = str(order_data.get("trading_mode", "REAL")).upper()
    try:
        trading_mode = TradingMode(trading_mode_raw)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"invalid trading_mode: {trading_mode_raw}"
        )

    symbol = str(order_data.get("symbol") or "").strip().upper()
    side_raw = str(order_data.get("side") or "").strip().upper()
    quantity = float(order_data.get("quantity") or 0)
    price = float(order_data.get("price") or 0)
    order_type_raw = str(order_data.get("order_type") or "LIMIT").strip().upper()
    trade_action_raw = _normalize_trade_action(order_data.get("trade_action"))
    position_side_raw = str(order_data.get("position_side") or "long").strip().lower()
    is_margin_trade = bool(order_data.get("is_margin_trade", False))
    client_order_id = str(order_data.get("client_order_id") or "").strip() or None
    remarks = order_data.get("remarks")

    if client_order_id is None:
        client_order_id = f"auto-{uuid.uuid4().hex}"

    if not symbol:
        raise HTTPException(status_code=400, detail="missing symbol")
    if side_raw not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail=f"invalid side: {side_raw}")
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")

    logger.info(
        "[Order] 收到信号 | 租户=%s 模式=%s | %s %s @ %s | trade_action=%s position_side=%s margin=%s",
        tenant,
        trading_mode.value,
        side_raw,
        symbol,
        price,
        trade_action_raw,
        position_side_raw,
        is_margin_trade,
    )

    if trading_mode in {TradingMode.SHADOW, TradingMode.SIMULATION}:
        # 与自动托管同一条链：SimOrderService → execute_order → apply_filled（sim_trades + ledger）。
        # 禁止只改 Redis / 手插 sim_orders，否则交易记录与 EOD/资金台账会分裂。
        try:
            from backend.services.simulation.models.order import SimOrder
            from backend.services.simulation.services.order_submission_service import (
                SimulationOrderSubmissionService,
            )

            dup_marker = f"client_order_id={client_order_id}"
            dup_stmt = (
                select(SimOrder.order_id)
                .where(
                    and_(
                        SimOrder.tenant_id == tenant,
                        SimOrder.user_id == uid,
                        SimOrder.remarks.startswith(dup_marker),
                    )
                )
                .limit(1)
            )
            if (await db.execute(dup_stmt)).scalar_one_or_none() is not None:
                return {
                    "status": "success",
                    "execution": "duplicate_skipped",
                    "result": {
                        "success": True,
                        "message": f"duplicate client_order_id skipped: {client_order_id}",
                    },
                }

            strategy_id_raw = str(order_data.get("strategy_id") or "").strip()
            strategy_id_val = int(strategy_id_raw) if strategy_id_raw.isdigit() else None
            if strategy_id_val is not None and strategy_id_val <= 0:
                strategy_id_val = None
            original_remarks = str(remarks or "").strip()
            combined_remarks = dup_marker
            if original_remarks and original_remarks != dup_marker:
                combined_remarks = f"{dup_marker} {original_remarks}"[:500]
            # 显式市价单可能携带预案参考价，不能据此改成限价单。
            # 未传类型的旧调用方仍保留按价格推断的兼容行为。
            sim_order_type = (
                order_type_raw.lower()
                if order_data.get("order_type")
                else ("limit" if price > 0 else "market")
            )
            if sim_order_type not in {"market", "limit"}:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid simulation order_type: {order_type_raw}",
                )
            trigger_source = "manual"
            if str(client_order_id or "").startswith("manual-"):
                trigger_source = "manual"
            elif str(client_order_id or "").startswith("auto-"):
                trigger_source = "hosted"

            sim_manager = SimulationAccountManager(redis)
            submission = SimulationOrderSubmissionService(db, sim_manager)
            outcome = await submission.submit_and_fill(
                tenant_id=tenant,
                user_id=uid,
                symbol=symbol,
                side=side_raw.lower(),
                quantity=quantity,
                order_type=sim_order_type,
                price=price if sim_order_type == "limit" and price > 0 else None,
                portfolio_id=0,
                strategy_id=strategy_id_val,
                trade_action=trade_action_raw,
                position_side=position_side_raw,
                is_margin_trade=is_margin_trade,
                remarks=combined_remarks,
                client_order_id=client_order_id,
                trigger_source=trigger_source,
            )
            if not outcome.success:
                logger.warning(
                    "[Shadow/Sim] 虚拟成交失败: %s %s reason=%s",
                    symbol,
                    side_raw,
                    outcome.message,
                )
                return {
                    "status": "failed",
                    "execution": "virtual",
                    "order_id": outcome.order_id,
                    "detail": {"success": False, "reason": outcome.message},
                    "result": {"success": False, "message": outcome.message},
                }

            logger.info(
                "[Shadow/Sim] 虚拟成交完成: %s %s qty=%s fee=%.2f order=%s",
                symbol,
                side_raw,
                outcome.filled_quantity,
                outcome.commission,
                outcome.order_id,
            )
            await mirror_virtual_fill(
                db=db,
                redis=redis,
                tenant_id=tenant,
                user_id=str(uid),
                symbol=symbol,
                side=side_raw,
                quantity=outcome.filled_quantity or quantity,
                price=outcome.fill_price or price,
                client_order_id=client_order_id or "",
                strategy_id=strategy_id_raw,
                source=f"internal_dispatcher:{trading_mode.value}",
            )
            return {
                "status": "success",
                "execution": "virtual",
                "order_id": outcome.order_id,
                "result": {
                    "success": True,
                    "message": outcome.message,
                    "price_source": outcome.price_source,
                },
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("[Shadow/Sim] 虚拟成交失败: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    try:
        strategy_id = order_data.get("strategy_id")
        strategy_id_str = str(strategy_id or "").strip()
        strategy_id_val = int(strategy_id_str) if strategy_id_str.isdigit() else None
        portfolio_id = int(order_data.get("portfolio_id") or 0)
        if portfolio_id <= 0:
            snapshot = await _fetch_active_portfolio_snapshot(
                db,
                tenant_id=tenant,
                user_id=str(uid),
                strategy_id=str(strategy_id or ""),
            )
            if snapshot:
                portfolio_id = int(snapshot.get("portfolio_id") or 0)

        if portfolio_id <= 0:
            stmt = (
                select(Portfolio.id)
                .where(
                    and_(
                        Portfolio.tenant_id == tenant,
                        Portfolio.user_id == uid,
                        Portfolio.status == "active",
                    )
                )
                .order_by(Portfolio.updated_at.desc())
                .limit(1)
            )
            result = await db.execute(stmt)
            portfolio_id = int(result.scalar_one_or_none() or 0)

        if portfolio_id <= 0:
            raise HTTPException(status_code=400, detail="no active portfolio available")

        try:
            order_type = OrderType(order_type_raw)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"invalid order_type: {order_type_raw}"
            )
        try:
            position_side = PositionSide(position_side_raw)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"invalid position_side: {position_side_raw}"
            )
        trade_action = None
        if trade_action_raw:
            try:
                trade_action = TradeAction(trade_action_raw)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"invalid trade_action: {trade_action_raw}"
                )

        order_service = OrderService(db, redis)
        engine = TradingEngine(db, redis)

        if client_order_id:
            existed_stmt = (
                select(Order)
                .where(
                    and_(
                        Order.tenant_id == tenant,
                        Order.user_id == str(uid),
                        Order.client_order_id == client_order_id,
                    )
                )
                .limit(1)
            )
            existed_result = await db.execute(existed_stmt)
            existed_order = existed_result.scalar_one_or_none()
            if existed_order is not None:
                return {
                    "status": "success",
                    "execution": "duplicate_skipped",
                    "order_id": str(existed_order.order_id),
                    "result": {
                        "success": True,
                        "message": "duplicate client_order_id skipped",
                        "client_order_id": client_order_id,
                    },
                }

        order = await order_service.create_order(
            user_id=uid,
            tenant_id=tenant,
            order_data=OrderCreate(
                portfolio_id=portfolio_id,
                strategy_id=strategy_id_val,
                symbol=symbol,
                symbol_name=order_data.get("symbol_name"),
                side=OrderSide(side_raw),
                order_type=order_type,
                quantity=quantity,
                price=price if price > 0 else None,
                trade_action=trade_action,
                position_side=position_side,
                is_margin_trade=is_margin_trade,
                trading_mode=trading_mode,
                client_order_id=client_order_id,
                remarks=remarks,
            ),
        )
    except IntegrityError:
        if not client_order_id:
            raise
        dup_stmt = (
            select(Order)
            .where(
                and_(
                    Order.tenant_id == tenant,
                    Order.user_id == str(uid),
                    Order.client_order_id == client_order_id,
                )
            )
            .limit(1)
        )
        dup_result = await db.execute(dup_stmt)
        dup_order = dup_result.scalar_one_or_none()
        if dup_order is None:
            raise
        return {
            "status": "success",
            "execution": "duplicate_skipped",
            "order_id": str(dup_order.order_id),
            "result": {
                "success": True,
                "message": "duplicate client_order_id skipped",
                "client_order_id": client_order_id,
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Internal order dispatch failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))

    risk_result = await engine.check_order_risk(uid, order)
    if not risk_result.get("passed"):
        await order_service.transition_order_status(
            order,
            OrderStatus.REJECTED,
            remarks=f"Risk check failed: {risk_result.get('violations')}",
        )
        return {
            "status": "rejected",
            "execution": "risk_blocked",
            "order_id": str(order.order_id),
            "violations": risk_result.get("violations", []),
        }

    submit_result = await engine.submit_order(order, tenant_id=tenant)
    return {
        "status": "success" if submit_result.get("success") else "failed",
        "execution": "direct",
        "order_id": str(order.order_id),
        "result": submit_result,
    }
