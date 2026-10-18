"""全市场最新行情 Redis（模拟撮合 / stream 直读唯一默认源）。

写死为公网行情库；可用 REMOTE_QUOTE_REDIS_* 环境变量覆盖（部署调试用）。
延时约 1–2 分钟；键格式后缀正典 market:series:{600036.SH}（ZSET），
读端兼容老前缀键 market:series:{SH600036}。
"""

from __future__ import annotations

import os

# 全市场最新行情：quantmindai.cn db3
DEFAULT_REMOTE_QUOTE_REDIS_HOST = "quantmindai.cn"
DEFAULT_REMOTE_QUOTE_REDIS_PORT = 6379
DEFAULT_REMOTE_QUOTE_REDIS_PASSWORD = "quantmind2026"
DEFAULT_REMOTE_QUOTE_REDIS_DB = 3
DEFAULT_SIM_REDIS_QUOTE_MAX_AGE_SEC = 300


def remote_quote_redis_host() -> str:
    return (
        os.getenv("REMOTE_QUOTE_REDIS_HOST") or DEFAULT_REMOTE_QUOTE_REDIS_HOST
    ).strip() or DEFAULT_REMOTE_QUOTE_REDIS_HOST


def remote_quote_redis_port() -> int:
    raw = (os.getenv("REMOTE_QUOTE_REDIS_PORT") or "").strip()
    try:
        return int(raw) if raw else DEFAULT_REMOTE_QUOTE_REDIS_PORT
    except ValueError:
        return DEFAULT_REMOTE_QUOTE_REDIS_PORT


def remote_quote_redis_password() -> str | None:
    # 显式空字符串表示无密码；未设置则用写死默认密码
    if "REMOTE_QUOTE_REDIS_PASSWORD" in os.environ:
        value = os.environ.get("REMOTE_QUOTE_REDIS_PASSWORD") or ""
        return value.strip() or None
    return DEFAULT_REMOTE_QUOTE_REDIS_PASSWORD


def remote_quote_redis_db() -> int:
    raw = (os.getenv("REMOTE_QUOTE_REDIS_DB") or "").strip()
    try:
        return int(raw) if raw else DEFAULT_REMOTE_QUOTE_REDIS_DB
    except ValueError:
        return DEFAULT_REMOTE_QUOTE_REDIS_DB


def sim_redis_quote_max_age_sec() -> int:
    raw = (os.getenv("SIM_REDIS_QUOTE_MAX_AGE_SEC") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_SIM_REDIS_QUOTE_MAX_AGE_SEC
    except ValueError:
        value = DEFAULT_SIM_REDIS_QUOTE_MAX_AGE_SEC
    return max(30, value)
