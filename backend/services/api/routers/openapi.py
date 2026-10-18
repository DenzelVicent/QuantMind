"""
OpenAPI (SDK) 只读网关
======================

前缀 `/api/v1/open`，鉴权走 AK/SK 兑换的短期 Bearer 令牌
（`backend/services/api/user_app/middleware/openapi_auth.py`），
与浏览器 JWT 会话隔离。

v1 共 7 个接口：
  POST /auth/token                AK/SK 兑换访问令牌（无需鉴权）
  GET  /news/sentiment            系统新闻情绪（scope: news.read）
  GET  /news/stats                情绪标签统计（scope: news.read）
  GET  /inference/latest          当前生效推理批次（scope: inference.read）
  GET  /inference/runs/{run_id}   推理批次明细/信号（scope: inference.read）
  GET  /inference/stock/{symbol}/history  个股历史分数（scope: inference.read）
  GET  /paper/positions            模拟盘持仓（scope: paper.read）

实现原则：不复制业务逻辑。news/inference 直接调用既有路由函数；
paper 经 httpx 转发 trade 服务（与 trade_proxy 同模式，网关注入身份头）。
统一包络 {code, message, data}，SDK 侧拆包。
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from backend.services.api.routers.model_training import (
    get_model_inference_latest,
    get_model_inference_run_detail,
    get_stock_inference_history,
    list_batch_inferences,
)
from backend.services.api.routers.news import enrichment_stats, list_articles
from backend.services.api.user_app.middleware.openapi_auth import (
    SCOPE_INFERENCE_READ,
    SCOPE_NEWS_READ,
    SCOPE_PAPER_READ,
    exchange_token,
    require_openapi_scope,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/open", tags=["OpenAPI"])

TRADE_BASE_URL = os.getenv("TRADE_SERVICE_URL", "http://trade-core:8002").rstrip("/")


def _ok(data):
    return {"code": 200, "message": "ok", "data": data}


class TokenRequest(BaseModel):
    access_key: str
    secret_key: str


@router.post("/auth/token", summary="AK/SK 兑换访问令牌")
async def open_auth_token(body: TokenRequest):
    return _ok(await exchange_token(body.access_key, body.secret_key))


@router.get("/news/sentiment", summary="系统新闻情绪查询（SDK）")
async def open_news_sentiment(
    tickers: str | None = Query(None, description="股票代码后缀式，逗号分隔 e.g. 600036.SH,000858.SZ"),
    sentiment: str | None = Query(None, description="bullish/bearish/neutral"),
    strong_only: bool = Query(False, description="仅强信号 |score|>=0.5"),
    keyword: str | None = Query(None, description="标题关键词"),
    since: str | None = Query(None, description="起始时间 ISO8601"),
    until: str | None = Query(None, description="截止时间 ISO8601"),
    sort: str = Query("time_desc", description="time_desc/time_asc/sentiment_bullish/sentiment_bearish"),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=200),
    user: dict = Depends(require_openapi_scope(SCOPE_NEWS_READ)),
):
    result = await list_articles(
        source_id=None,
        source_ids=None,
        folder_id=None,
        keyword=keyword,
        only_financial_event=False,
        tickers=tickers,
        industries=None,
        sentiment=sentiment,
        event_tags=None,
        countries=None,
        regions=None,
        key_terms=None,
        date_entities=None,
        provinces=None,
        cities=None,
        politicians=None,
        visits=None,
        departments=None,
        starred=None,
        strong_only=strong_only,
        sort=sort,
        since=since,
        until=until,
        page=page,
        page_size=page_size,
    )
    return _ok(result)


@router.get("/news/stats", summary="新闻情绪标签统计（SDK）")
async def open_news_stats(
    tickers: str | None = Query(None),
    sentiment: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    user: dict = Depends(require_openapi_scope(SCOPE_NEWS_READ)),
):
    result = await enrichment_stats(
        tickers=tickers,
        industries=None,
        sentiment=sentiment,
        event_tags=None,
        countries=None,
        regions=None,
        key_terms=None,
        date_entities=None,
        provinces=None,
        cities=None,
        politicians=None,
        visits=None,
        departments=None,
        strong_only=False,
        keyword=None,
        since=since,
        until=until,
    )
    return _ok(result)


@router.get("/inference/latest", summary="当前生效推理批次（SDK）")
async def open_inference_latest(
    model_id: str | None = Query(None, description="模型ID，可选"),
    user: dict = Depends(require_openapi_scope(SCOPE_INFERENCE_READ)),
):
    return _ok(await get_model_inference_latest(model_id, user))


@router.get("/inference/runs/{run_id}", summary="推理批次明细/信号（SDK）")
async def open_inference_run_detail(
    run_id: str,
    user: dict = Depends(require_openapi_scope(SCOPE_INFERENCE_READ)),
):
    return _ok(await get_model_inference_run_detail(run_id, user))


@router.get("/inference/stock/{symbol}/history", summary="个股历史推理分数（SDK）")
async def open_stock_history(
    symbol: str,
    days: int = Query(180, ge=7, le=7300),
    model_id: str | None = Query(None),
    end_date: str | None = Query(None, description="窗口终点 YYYY-MM-DD，缺省今日"),
    user: dict = Depends(require_openapi_scope(SCOPE_INFERENCE_READ)),
):
    return _ok(await get_stock_inference_history(symbol, days, model_id, end_date, user))


@router.get("/inference/models", summary="用户训练模型列表（SDK）")
async def open_inference_models(
    user: dict = Depends(require_openapi_scope(SCOPE_INFERENCE_READ)),
):
    """SDK 可发现性入口：返回该 AK 用户的模型（model_id/status/is_default），
    拿到 model_id 后再调 latest/runs/history。只读 qm_user_models，不扫服务器目录。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    tenant_id = str(user.get("tenant_id") or "default")
    user_id = str(user.get("user_id") or "")
    try:
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT model_id, status, is_default, created_at, updated_at "
                        "FROM qm_user_models "
                        "WHERE tenant_id = :tid AND user_id = :uid "
                        "ORDER BY updated_at DESC"
                    ),
                    {"tid": tenant_id, "uid": user_id},
                )
            ).mappings().all()
    except Exception as exc:
        logger.warning("open models query failed: %s", exc)
        return _ok({"items": [], "total": 0})
    items = []
    for r in rows:
        d = dict(r or {})
        for k in ("created_at", "updated_at"):
            if d.get(k) is not None:
                try:
                    d[k] = d[k].isoformat()
                except Exception:
                    d[k] = str(d[k])
        items.append(d)
    return _ok({"items": items, "total": len(items)})


@router.get("/inference/batches", summary="批量推理历史（SDK）")
async def open_inference_batches(
    model_id: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: dict = Depends(require_openapi_scope(SCOPE_INFERENCE_READ)),
):
    return _ok(
        await list_batch_inferences(
            model_id=model_id,
            status=status,
            page=page,
            page_size=page_size,
            current_user=user,
        )
    )


@router.get("/paper/positions", summary="模拟盘持仓（SDK）")
async def open_paper_positions(
    request: Request,
    market: str = Query("CN", description="CN/HK/US/FUTURES/CRYPTO"),
    user: dict = Depends(require_openapi_scope(SCOPE_PAPER_READ)),
):
    """转发 trade 服务的模拟盘账户接口（与 trade_proxy 同模式）。

    注意 trade 侧 get_auth_context 只认 Authorization Bearer JWT
    （X-User-Id 仅为兼容保留），因此必须把 openapi 令牌原样透传，
    否则 trade 直接 401。
    """
    url = f"{TRADE_BASE_URL}/api/v1/simulation/account"
    headers = {
        "X-User-Id": str(user.get("user_id") or ""),
        "X-Tenant-Id": str(user.get("tenant_id") or "default"),
    }
    incoming_auth = request.headers.get("authorization")
    if incoming_auth:
        headers["Authorization"] = incoming_auth
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0), trust_env=False
        ) as client:
            resp = await client.get(url, params={"market": (market or "CN").upper()}, headers=headers)
    except Exception as exc:
        logger.error("open paper positions: trade service unreachable: %s", exc)
        from fastapi import HTTPException

        raise HTTPException(status_code=502, detail=f"交易服务不可用: {exc}") from exc
    if resp.status_code != 200:
        from fastapi import HTTPException

        raise HTTPException(status_code=resp.status_code, detail=f"交易服务返回 {resp.status_code}")
    try:
        upstream = resp.json()
    except Exception as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=502, detail="交易服务返回非 JSON") from exc
    return _ok(upstream)
