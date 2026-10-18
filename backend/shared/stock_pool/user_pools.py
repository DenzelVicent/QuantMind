"""用户私有股票池（favorites / research）—— 自选与研究池对齐全局池的唯一写入口。

约定：
- code=favorites：终端 / 仪表盘 / 投研「加入自选」
- code=research：旧 qm_user_research_pool 迁移目标
- scope=user，owner_user_id=当前用户；仅本人可写
- 首次 ensure 时惰性把旧表成员并入 TXT（source_ref 打迁移标记，只跑一次）
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.shared.stock_pool import repository as repo
from backend.shared.stock_pool.constants import SCOPE_USER, STATUS_ACTIVE
from backend.shared.stock_pool.schemas import PoolMembersSave, StockPool, StockPoolCreate
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

CODE_FAVORITES = "favorites"
CODE_RESEARCH = "research"

_ALLOWED_CODES = frozenset({CODE_FAVORITES, CODE_RESEARCH})

_DEFAULT_NAMES = {
    CODE_FAVORITES: "我的自选",
    CODE_RESEARCH: "我的研究池",
}

_MIGRATE_REF = {
    CODE_FAVORITES: "migrated:qm_user_watchlist",
    CODE_RESEARCH: "migrated:qm_user_research_pool",
}

_SUFFIX_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.I)


class UserPoolEnsureRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    market: str = "CN"


def _normalize_code(code: str) -> str:
    return str(code or "").strip().lower()


def _require_allowed_code(code: str) -> str:
    c = _normalize_code(code)
    if c not in _ALLOWED_CODES:
        raise ValueError(
            f"用户池 code 仅支持 {sorted(_ALLOWED_CODES)}，收到: {code}"
        )
    return c


def _to_suffix(raw: str) -> str | None:
    """任意口径 → 后缀正典（600036.SH），非法返回 None。"""
    try:
        suffix = StockCodeUtil.normalize(str(raw or "").strip())
    except Exception:
        return None
    if not suffix or not _SUFFIX_RE.match(suffix):
        return None
    return suffix.upper()


# 旧名兼容（调用方统一用 _to_suffix；保留别名防外部导入断裂）
_to_prefix = _to_suffix


async def _load_legacy_symbols(
    session, *, code: str, tenant_id: str, user_id: str
) -> list[str]:
    table = (
        "qm_user_watchlist"
        if code == CODE_FAVORITES
        else "qm_user_research_pool"
    )
    try:
        rows = (
            await session.execute(
                text(
                    f"SELECT symbol FROM {table} "
                    "WHERE tenant_id = :tid AND user_id = :uid"
                ),
                {"tid": tenant_id, "uid": str(user_id)},
            )
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 — 旧表可能不存在
        logger.debug("legacy pool migrate skip table=%s: %s", table, exc)
        return []
    out: list[str] = []
    seen: set[str] = set()
    for (sym,) in rows:
        suffix = _to_suffix(str(sym or ""))
        if not suffix or suffix in seen:
            continue
        seen.add(suffix)
        out.append(suffix)
    return out


async def _mark_migrated(session, pool: StockPool, *, code: str, actor: str) -> None:
    await session.execute(
        text(
            """
            UPDATE qm_stock_pool
               SET source_kind = 'legacy_migrate',
                   source_ref = :ref,
                   updated_at = NOW(),
                   updated_by = :actor
             WHERE pool_id = :pid
            """
        ),
        {"pid": pool.pool_id, "ref": _MIGRATE_REF[code], "actor": actor},
    )
    await session.commit()


async def ensure_user_pool(
    session,
    *,
    code: str,
    user_id: str,
    tenant_id: str,
    name: str | None = None,
    market: str = "CN",
    actor: str | None = None,
) -> StockPool:
    """幂等确保用户池存在，并在需要时做一次旧表迁移。"""
    code = _require_allowed_code(code)
    actor = actor or str(user_id)
    await repo.ensure_tables(session)

    pool = await repo.get_pool_by_code(
        session,
        code,
        scope=SCOPE_USER,
        tenant_id=tenant_id,
        owner_user_id=str(user_id),
    )
    if pool is None:
        pool = await repo.create_pool(
            session,
            StockPoolCreate(
                code=code,
                name=(name or _DEFAULT_NAMES[code]).strip() or _DEFAULT_NAMES[code],
                description=f"用户私有池 {code}",
                market=market or "CN",
                pool_type="static",
                scope=SCOPE_USER,
                tenant_id=tenant_id,
                owner_user_id=str(user_id),
                source_kind="user",
                source_ref=None,
            ),
            actor=actor,
        )

    migrate_ref = _MIGRATE_REF[code]
    if str(pool.source_ref or "") != migrate_ref:
        legacy = await _load_legacy_symbols(
            session, code=code, tenant_id=tenant_id, user_id=str(user_id)
        )
        if legacy:
            existing = repo.read_members(pool)
            merged = list(dict.fromkeys([*existing, *legacy]))
            await repo.save_members(session, pool, merged, actor=actor)
            refreshed = await repo.get_pool(session, pool.pool_id)
            if refreshed is not None:
                pool = refreshed
            logger.info(
                "user pool migrate code=%s user=%s legacy=%d total=%d",
                code,
                user_id,
                len(legacy),
                len(merged),
            )
        await _mark_migrated(session, pool, code=code, actor=actor)
        refreshed = await repo.get_pool(session, pool.pool_id)
        if refreshed is not None:
            pool = refreshed

    return pool


async def get_owned_user_pool(
    session, *, code: str, user_id: str, tenant_id: str
) -> StockPool | None:
    code = _require_allowed_code(code)
    await repo.ensure_tables(session)
    pool = await repo.get_pool_by_code(
        session,
        code,
        scope=SCOPE_USER,
        tenant_id=tenant_id,
        owner_user_id=str(user_id),
    )
    if pool is None:
        return None
    if pool.status != STATUS_ACTIVE:
        return None
    if str(pool.owner_user_id or "") != str(user_id):
        return None
    return pool


def pool_to_dict(pool: StockPool) -> dict[str, Any]:
    return pool.model_dump(mode="json")


async def add_symbol_to_user_pool(
    session,
    *,
    code: str,
    symbol: str,
    user_id: str,
    tenant_id: str,
    actor: str | None = None,
) -> dict[str, Any]:
    pool = await ensure_user_pool(
        session,
        code=code,
        user_id=user_id,
        tenant_id=tenant_id,
        actor=actor,
    )
    suffix = _to_suffix(symbol)
    if not suffix:
        raise ValueError(f"非法股票代码: {symbol}")
    members = repo.read_members(pool)
    # 成员 TXT 可能混有老 prefix 行，双口径归一后比对防重
    if suffix in {StockCodeUtil.normalize(m) for m in members}:
        return {
            "pool": pool_to_dict(pool),
            "symbol": suffix,
            "added": False,
            "symbol_count": len(members),
        }
    result = await repo.save_members(
        session, pool, [*members, suffix], actor=actor or str(user_id)
    )
    refreshed = await repo.get_pool(session, pool.pool_id)
    return {
        "pool": pool_to_dict(refreshed or pool),
        "symbol": suffix,
        "added": True,
        "symbol_count": int(result.get("symbol_count") or 0),
    }


async def remove_symbol_from_user_pool(
    session,
    *,
    code: str,
    symbol: str,
    user_id: str,
    tenant_id: str,
    actor: str | None = None,
) -> dict[str, Any]:
    pool = await get_owned_user_pool(
        session, code=code, user_id=user_id, tenant_id=tenant_id
    )
    if pool is None:
        pool = await ensure_user_pool(
            session,
            code=code,
            user_id=user_id,
            tenant_id=tenant_id,
            actor=actor,
        )
    suffix = _to_suffix(symbol)
    if not suffix:
        raise ValueError(f"非法股票代码: {symbol}")
    members = repo.read_members(pool)
    remaining = [m for m in members if StockCodeUtil.normalize(m) != suffix]
    if len(remaining) == len(members):
        return {
            "pool": pool_to_dict(pool),
            "symbol": suffix,
            "removed": False,
            "symbol_count": len(members),
        }
    result = await repo.save_members(
        session, pool, remaining, actor=actor or str(user_id)
    )
    refreshed = await repo.get_pool(session, pool.pool_id)
    return {
        "pool": pool_to_dict(refreshed or pool),
        "symbol": suffix,
        "removed": True,
        "symbol_count": int(result.get("symbol_count") or 0),
    }


async def replace_user_pool_members(
    session,
    *,
    code: str,
    symbols: list[str],
    user_id: str,
    tenant_id: str,
    actor: str | None = None,
) -> dict[str, Any]:
    pool = await ensure_user_pool(
        session,
        code=code,
        user_id=user_id,
        tenant_id=tenant_id,
        actor=actor,
    )
    payload = PoolMembersSave(symbols=list(symbols or []))
    # 归一到后缀正典再存（save_members 内部会再转存储口径）
    api_syms: list[str] = []
    for raw in payload.symbols:
        suffix = _to_suffix(raw)
        if suffix:
            api_syms.append(suffix)
    result = await repo.save_members(
        session, pool, api_syms, actor=actor or str(user_id)
    )
    refreshed = await repo.get_pool(session, pool.pool_id)
    return {
        "pool": pool_to_dict(refreshed or pool),
        **result,
    }


def members_as_watchlist_items(api_symbols: list[str]) -> list[dict[str, Any]]:
    """兼容旧 /research/watchlist 响应形态。"""
    items: list[dict[str, Any]] = []
    for sym in api_symbols:
        suffix = _to_suffix(sym) or str(sym)
        items.append(
            {
                "symbol": suffix,
                "stockName": None,
                "addedAt": None,
                "sourceRunId": None,
            }
        )
    return items
