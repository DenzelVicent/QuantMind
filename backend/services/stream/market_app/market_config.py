import json
from pathlib import Path
from typing import List

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.shared.quote_redis_config import (
    DEFAULT_REMOTE_QUOTE_REDIS_DB,
    DEFAULT_REMOTE_QUOTE_REDIS_HOST,
    DEFAULT_REMOTE_QUOTE_REDIS_PASSWORD,
    DEFAULT_REMOTE_QUOTE_REDIS_PORT,
)

ROOT_ENV = Path(__file__).resolve().parents[4] / ".env"

# ============================================================
# 行情 Redis 硬编码：全市场最新行情 quantmindai.cn db3
# ============================================================
MARKET_REDIS_HOST = DEFAULT_REMOTE_QUOTE_REDIS_HOST
MARKET_REDIS_PORT = DEFAULT_REMOTE_QUOTE_REDIS_PORT
MARKET_REDIS_PASSWORD = DEFAULT_REMOTE_QUOTE_REDIS_PASSWORD
MARKET_REDIS_DB = DEFAULT_REMOTE_QUOTE_REDIS_DB


class Settings(BaseSettings):
    """Application settings"""

    model_config = SettingsConfigDict(
        env_file=(str(ROOT_ENV), ".env"),
        case_sensitive=True,
        extra="ignore",
    )

    # Service
    SERVICE_NAME: str = "market-data-service"
    MARKET_DATA_HOST: str = "0.0.0.0"
    MARKET_DATA_PORT: int = 8003
    DEBUG: bool = False

    # 兼容旧代码引用的 HOST/PORT
    @property
    def HOST(self) -> str:
        return self.MARKET_DATA_HOST

    @property
    def PORT(self) -> int:
        return self.MARKET_DATA_PORT

    # Database (Unified Configuration)
    MARKET_DATA_DB_URL: str = Field(
        default="postgresql+psycopg2://postgres:@localhost:5432/quantmind",
        validation_alias=AliasChoices("MARKET_DATA_DB_URL", "DATABASE_URL"),
    )
    DB_DRIVER: str = "asyncpg"
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_NAME: str = "quantmind"
    DB_USER: str = "postgres"
    DB_PASSWORD: str = ""

    @property
    def DATABASE_URL(self) -> str:
        return self.MARKET_DATA_DB_URL

    @model_validator(mode="after")
    def _ensure_database_url(self):
        # 若 DATABASE_URL 未显式配置，使用 DB_* 拼接，确保与根 .env 一致。
        current = (self.MARKET_DATA_DB_URL or "").strip()
        if current and "localhost:5432/quantmind" not in current:
            return self

        driver = (self.DB_DRIVER or "asyncpg").strip()
        user = (self.DB_USER or "postgres").strip()
        password = self.DB_PASSWORD or ""
        host = (self.DB_HOST or "localhost").strip()
        port = int(self.DB_PORT or 5432)
        db = (self.DB_NAME or "quantmind").strip()
        self.MARKET_DATA_DB_URL = (
            f"postgresql+{driver}://{user}:{password}@{host}:{port}/{db}"
        )
        return self

    DB_POOL_SIZE: int = 20
    DB_MAX_OVERFLOW: int = 30
    DB_ECHO: bool = False

    # Redis (Unified - OSS Edition)
    # 行情 Redis 已硬编码为 quantmind-redis 访客只读用户
    REDIS_HOST: str = Field(default=MARKET_REDIS_HOST)
    REDIS_PORT: int = Field(default=MARKET_REDIS_PORT)
    REDIS_USE_SENTINEL: bool = Field(default=False)
    REDIS_SENTINELS_RAW: str = Field(default="localhost:26379")
    REDIS_MASTER_NAME: str = "quantmind-master"
    REDIS_PASSWORD: str = Field(default=MARKET_REDIS_PASSWORD)
    REDIS_DB: int = Field(
        default=MARKET_REDIS_DB, validation_alias=AliasChoices("REDIS_DB", "REDIS_DB_MARKET")
    )

    # 远程行情快照 Redis（默认全市场库 quantmindai.cn db3）
    REMOTE_QUOTE_REDIS_HOST: str = Field(default=MARKET_REDIS_HOST)
    REMOTE_QUOTE_REDIS_PORT: int = Field(default=MARKET_REDIS_PORT)
    REMOTE_QUOTE_REDIS_PASSWORD: str = Field(default=MARKET_REDIS_PASSWORD)
    REMOTE_QUOTE_REDIS_DB: int = Field(default=MARKET_REDIS_DB)

    # 无订阅时的保活拉取标的，确保落库/推送链路持续有数据（后缀正典）
    STREAM_WARMUP_SYMBOLS: str = "000001.SZ,600000.SH"

    # 远端行情 Redis 写入开关（OSS 默认只读）。
    # 行情快照/时序统一由上游提供，本容器只读、不发布：
    # - STREAM_WRITE_SERIES：是否把读到的快照回写 market:series:*（默认关）
    # - STREAM_PERSIST_STATS：是否回写 market:stream:persist_stats（默认关）
    STREAM_WRITE_SERIES: bool = False
    STREAM_PERSIST_STATS: bool = False

    # Cache TTL (seconds)
    CACHE_TTL_QUOTE: int = 1
    CACHE_TTL_KLINE: int = 60
    CACHE_TTL_SNAPSHOT: int = 5

    # WebSocket
    WS_HEARTBEAT_INTERVAL: int = 30
    WS_MAX_CONNECTIONS: int = 1000

    # Data Sources
    # A 股行情统一走 QuantDB 本地 parquet；remote_redis 保留给后续实时快照接入。
    DATA_SOURCES: list[str] = ["quantdb", "remote_redis"]
    DEFAULT_SOURCE: str = "quantdb"

    @field_validator("DEFAULT_SOURCE")
    @classmethod
    def _validate_default_source(cls, v: str, info):  # type: ignore[override]
        data_sources = (
            (info.data.get("DATA_SOURCES") or []) if hasattr(info, "data") else []
        )
        if v and data_sources and v not in data_sources:
            raise ValueError(f"DEFAULT_SOURCE={v} 不在 DATA_SOURCES={data_sources} 中")
        return v

    @property
    def REDIS_SENTINELS(self) -> list[tuple]:
        text = (self.REDIS_SENTINELS_RAW or "").strip()
        if not text:
            return [("localhost", 26379)]
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                out = []
                for item in parsed:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        out.append((str(item[0]), int(item[1])))
                if out:
                    return out
        except Exception:
            pass
        pairs = []
        for item in text.split(","):
            seg = item.strip()
            if not seg:
                continue
            if ":" in seg:
                host, port = seg.split(":", 1)
                pairs.append((host.strip(), int(port.strip())))
            else:
                pairs.append((seg, 26379))
        return pairs or [("localhost", 26379)]

    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 100

    # Monitoring
    METRICS_ENABLED: bool = True


settings = Settings()
