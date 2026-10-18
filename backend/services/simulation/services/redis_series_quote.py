"""Redis 实时行情直读（market:series ZSET），供模拟撮合使用。

默认直连全市场行情库 quantmindai.cn:6379 db3（密码见 quote_redis_config），
可用 REMOTE_QUOTE_REDIS_* 覆盖。键格式遵循 AGENTS.md：序列键用后缀正典
`market:series:600036.SH`，成员为 JSON（含 price/open/high/low/volume/amount/
timestamp/source），score 即时间戳。读端兼容老前缀键
`market:series:SH600036`（新键未命中时回退试读）。

撮合取价时优先用本模块（Level 0）：盘中 tick 新鲜时直接按 Redis 现价成交；
陈旧或缺失时返回 None，由调用方走既有兜底链路。延时约 1–2 分钟。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

SERIES_KEY_PREFIX = "market:series:"


def _env() -> tuple[str | None, int, str | None, int]:
    from backend.shared.quote_redis_config import (
        remote_quote_redis_db,
        remote_quote_redis_host,
        remote_quote_redis_password,
        remote_quote_redis_port,
    )

    return (
        remote_quote_redis_host(),
        remote_quote_redis_port(),
        remote_quote_redis_password(),
        remote_quote_redis_db(),
    )


def series_key_for(symbol: str) -> str | None:
    """Convert a symbol to its canonical suffix series key.

    后缀正典：600036.SH -> market:series:600036.SH。港股/美股等非 A 股
    沿用原样大写透传。
    """
    import re as _re

    from backend.shared.stock_utils import StockCodeUtil

    raw = str(symbol or "").strip().upper()
    normalized = StockCodeUtil.normalize(raw)
    if _re.fullmatch(r"^\d{6}\.(SH|SZ|BJ)$", normalized):
        pass
    elif _re.fullmatch(r"(?:\d{4,5}\.HK|HK\d{5})", raw):
        normalized = raw
    elif _re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", raw):
        normalized = raw
    else:
        return None
    return f"{SERIES_KEY_PREFIX}{normalized}"


def legacy_series_key_for(symbol: str) -> str | None:
    """老前缀键（过渡期读兼容用）：600036.SH -> market:series:SH600036。

    与新键相同时返回 None（非 A 股透传无新老之分），调用方去重。
    迁移脚本全量执行后删除。
    """
    import re as _re

    from backend.shared.stock_utils import StockCodeUtil

    new_key = series_key_for(symbol)
    if not new_key:
        return None
    suffix = new_key.removeprefix(SERIES_KEY_PREFIX)
    if not _re.fullmatch(r"^\d{6}\.(SH|SZ|BJ)$", suffix):
        return None
    old_key = f"{SERIES_KEY_PREFIX}{StockCodeUtil.to_prefix(suffix)}"
    return old_key if old_key != new_key else None


def candidate_series_keys(symbol: str) -> list[str]:
    """读键候选（新后缀优先，老前缀兼容）。"""
    new_key = series_key_for(symbol)
    if not new_key:
        return []
    old_key = legacy_series_key_for(symbol)
    return [new_key] if not old_key else [new_key, old_key]


def parse_series_member(
    member: str | bytes, score: float, now_ts: float, max_age_sec: int
) -> dict[str, Any] | None:
    """解析单个 ZSET 成员；价格无效或超龄返回 None（纯函数，可单测）。"""
    try:
        data = json.loads(member)
    except (TypeError, ValueError):
        return None
    try:
        price = float(data.get("price") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    try:
        ts = int(float(score))
    except (TypeError, ValueError):
        return None
    age = now_ts - ts
    if age < 0 or age > max_age_sec:
        return None
    out: dict[str, Any] = {
        "price": price,
        "timestamp": ts,
        "age_s": age,
        "source": data.get("source") or "redis_series",
    }
    for key in ("open", "high", "low", "volume", "amount"):
        try:
            val = data.get(key)
            out[key] = float(val) if val is not None else None
        except (TypeError, ValueError):
            out[key] = None
    return out


_client = None


def _get_client():
    """远端行情 Redis 客户端（单例复用，与 stream 侧同实例）。"""
    global _client
    if _client is None:
        import redis.asyncio as aioredis

        host, port, password, db = _env()
        _client = aioredis.Redis(
            host=host,
            port=port,
            password=password,
            db=db,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
    return _client


async def fetch_snapshot_tick(symbol: str) -> dict[str, Any] | None:
    """直读同一远端 Redis 的快照；不依赖 HTTP、数据库或 WS 订阅落序列。"""
    return (await fetch_snapshot_ticks([symbol])).get(symbol)


async def fetch_snapshot_ticks(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """批量直读实时快照，共用一次 pipeline，供托管全市场选股使用。"""
    from backend.shared.quote_redis_config import sim_redis_quote_max_age_sec
    from backend.shared.stock_utils import StockCodeUtil

    if not symbols:
        return {}
    candidates: list[tuple[str, list[str]]] = []
    for symbol in dict.fromkeys(symbols):
        normalized = StockCodeUtil.normalize(symbol)
        keys = [f"market:snapshot:{normalized}"]
        _, exchange = StockCodeUtil.split_suffix(normalized)
        if exchange in {"SH", "SZ", "BJ"}:
            prefix = StockCodeUtil.to_prefix(normalized)
            keys.extend(
                [
                    f"market:snapshot:{prefix}",
                    f"market:snapshot:{prefix.lower()}",
                    f"stock:{normalized}",
                ]
            )
        candidates.append((symbol, keys))
    try:
        client = _get_client()
        pipe = client.pipeline(transaction=False)
        for _, keys in candidates:
            for key in keys:
                pipe.hgetall(key)
        rows = await pipe.execute()
        now_ts = time.time()
        max_age_sec = sim_redis_quote_max_age_sec()
        result: dict[str, dict[str, Any]] = {}
        offset = 0
        for symbol, keys in candidates:
            for data in rows[offset : offset + len(keys)]:
                if not data:
                    continue
                try:
                    ts = float(data.get("timestamp") or 0)
                except (TypeError, ValueError):
                    continue
                payload = {
                    "price": data.get("Now") or data.get("price"),
                    "open": data.get("Open"),
                    "high": data.get("High"),
                    "low": data.get("Low"),
                    "volume": data.get("Volume"),
                    "amount": data.get("Amount"),
                    "source": data.get("source") or "remote_redis",
                }
                tick = parse_series_member(json.dumps(payload), ts, now_ts, max_age_sec)
                if tick is not None:
                    tick["price_source"] = "redis_snapshot"
                    # 单个累计量快照不能推算窗口内成交量。
                    tick["recent_volume"] = None
                    result[symbol] = tick
                    break
            offset += len(keys)
        return result
    except Exception as exc:
        logger.warning("[RedisSnapshotQuote] 批量读取失败: %s", exc)
    return {}


async def fetch_series_tick(
    symbol: str, max_age_sec: int | None = None
) -> dict[str, Any] | None:
    """取 symbol 最新 tick；新鲜才返回，否则 None。

    max_age_sec 默认取 SIM_REDIS_QUOTE_MAX_AGE_SEC（默认 300），与 stream 侧
    快照“>300s 视为不可用”口径一致。
    """
    from backend.shared.quote_redis_config import sim_redis_quote_max_age_sec

    if max_age_sec is None:
        max_age_sec = sim_redis_quote_max_age_sec()
    else:
        try:
            max_age_sec = int(max_age_sec)
        except (TypeError, ValueError):
            max_age_sec = sim_redis_quote_max_age_sec()
    key = series_key_for(symbol)
    if not key:
        return None
    try:
        client = _get_client()
        if client is None:
            return None
        now_ts = time.time()
        for candidate in candidate_series_keys(symbol):
            rows = await client.zrevrange(candidate, 0, 0, withscores=True)
            if rows:
                member, score = rows[0]
                tick = parse_series_member(member, float(score), now_ts, max_age_sec)
                if tick is not None:
                    return tick
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RedisSeriesQuote] 读取 %s 失败: %s", key, exc)
        return None
    logger.debug("[RedisSeriesQuote] %s 无新鲜 tick", key)
    return None


async def fetch_series_ticks(
    symbols: list[str],
    *,
    max_age_sec: int | None = None,
    volume_window_sec: int = 60,
) -> dict[str, dict[str, Any]]:
    """批量取最新 tick，并附带流动性窗口内的增量成交量。

    最新价路径与 ``fetch_series_tick`` 一致：``zrevrange`` + ``max_age_sec``
    （默认 300s）。``volume_window_sec``（默认 60s，可由
    ``SIM_LIQUIDITY_WINDOW_SEC`` 覆盖）**只**用于 ``recent_volume``，不得再当作
    取价窗口——否则盘中超过 60s 无新 tick 的股票会被误判为缺失，权益结算
    回退到过期收盘价（≈成本价），总资产在「初始价 / 市价」之间跳动。
    """
    from backend.shared.quote_redis_config import sim_redis_quote_max_age_sec

    if max_age_sec is None:
        max_age_sec = sim_redis_quote_max_age_sec()
    else:
        try:
            max_age_sec = int(max_age_sec)
        except (TypeError, ValueError):
            max_age_sec = sim_redis_quote_max_age_sec()
    try:
        volume_window_sec = int(
            os.getenv("SIM_LIQUIDITY_WINDOW_SEC") or volume_window_sec
        )
    except (TypeError, ValueError):
        pass
    keyed = [(symbol, series_key_for(symbol)) for symbol in dict.fromkeys(symbols)]
    keyed = [(symbol, key) for symbol, key in keyed if key]
    client = _get_client()
    if client is None or not keyed:
        return {}
    now_ts = time.time()
    try:
        pipe = client.pipeline(transaction=False)
        for _, key in keyed:
            # 最新价：不受流动性窗口限制
            pipe.zrevrange(key, 0, 0, withscores=True)
            # 流动性：仅统计窗口内成交量增量
            pipe.zrangebyscore(
                key,
                now_ts - max(1, volume_window_sec),
                now_ts,
                withscores=True,
            )
        pipe_result = await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RedisSeriesQuote] 批量读取失败: %s", exc)
        return {}

    result: dict[str, dict[str, Any]] = {}
    missing: list[tuple[str, str]] = []
    for idx, (symbol, _) in enumerate(keyed):
        latest_rows = pipe_result[idx * 2]
        window_rows = pipe_result[idx * 2 + 1]
        tick = _build_tick(
            latest_rows, window_rows, now_ts, max_age_sec, volume_window_sec
        )
        if tick is None:
            # 新键缺失、过期或价格无效时，继续尝试迁移窗口内的老键。
            legacy_key = legacy_series_key_for(symbol)
            if legacy_key:
                missing.append((symbol, legacy_key))
            continue
        result[symbol] = tick
    if missing:
        # 第二轮：老前缀键回退（仅缺失 symbol，避免常态双倍 pipeline）
        try:
            pipe2 = client.pipeline(transaction=False)
            for _, legacy_key in missing:
                pipe2.zrevrange(legacy_key, 0, 0, withscores=True)
                pipe2.zrangebyscore(
                    legacy_key,
                    now_ts - max(1, volume_window_sec),
                    now_ts,
                    withscores=True,
                )
            pipe2_result = await pipe2.execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[RedisSeriesQuote] 老键回退读取失败: %s", exc)
            pipe2_result = []
        for idx, (symbol, _) in enumerate(missing):
            if idx * 2 + 1 >= len(pipe2_result):
                break
            tick = _build_tick(
                pipe2_result[idx * 2],
                pipe2_result[idx * 2 + 1],
                now_ts,
                max_age_sec,
                volume_window_sec,
            )
            if tick is not None:
                result[symbol] = tick
    return result


def _build_tick(
    latest_rows: Any,
    window_rows: Any,
    now_ts: float,
    max_age_sec: int,
    volume_window_sec: int,
) -> dict[str, Any] | None:
    """由最新行 + 流动性窗口行组装 tick（新键/老键两轮复用）。"""
    if not latest_rows:
        return None
    member, score = latest_rows[0]
    tick = parse_series_member(member, float(score), now_ts, max_age_sec)
    if tick is None:
        return None
    volumes: list[float] = []
    for raw_member, _raw_score in window_rows or ():
        try:
            payload = json.loads(raw_member)
            volume = float(payload.get("volume"))
            if volume >= 0:
                volumes.append(volume)
        except (TypeError, ValueError, KeyError):
            continue
    tick["recent_volume"] = (
        max(0.0, volumes[-1] - volumes[0]) if len(volumes) >= 2 else None
    )
    return tick
