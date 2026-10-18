"""用户私有股票池（favorites/research）单测。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.shared.stock_pool.user_pools import (
    CODE_FAVORITES,
    CODE_RESEARCH,
    _to_suffix,
    members_as_watchlist_items,
)


def test_to_suffix_normalizes_cn_codes():
    assert _to_suffix("600036.SH") == "600036.SH"
    assert _to_suffix("SH600036") == "600036.SH"
    assert _to_suffix("bad") is None


def test_members_as_watchlist_items_shape():
    items = members_as_watchlist_items(["SH600036", "SZ000001"])
    assert items[0]["symbol"] == "600036.SH"
    assert "stockName" in items[0]


@pytest.mark.asyncio
async def test_ensure_user_pool_creates_and_migrates_once(tmp_path, monkeypatch):
    from backend.shared.stock_pool import user_pools as up
    from backend.shared.stock_pool.schemas import StockPool

    created = StockPool(
        pool_id="sp_favorites_test",
        code=CODE_FAVORITES,
        name="我的自选",
        scope="user",
        owner_user_id="10000001",
        tenant_id="default",
        source_ref=None,
        file_path=str(tmp_path / "favorites.txt"),
    )

    session = MagicMock()
    session.execute = AsyncMock(
        return_value=SimpleNamespace(fetchall=lambda: [("SH600036",), ("600519.SH",)])
    )
    session.commit = AsyncMock()

    with (
        patch.object(up.repo, "ensure_tables", new=AsyncMock()),
        patch.object(up.repo, "get_pool_by_code", new=AsyncMock(return_value=None)),
        patch.object(up.repo, "create_pool", new=AsyncMock(return_value=created)) as create_mock,
        patch.object(up.repo, "read_members", return_value=[]),
        patch.object(up.repo, "save_members", new=AsyncMock(return_value={"symbol_count": 2})) as save_mock,
        patch.object(up.repo, "get_pool", new=AsyncMock(return_value=created)),
    ):
        pool = await up.ensure_user_pool(
            session,
            code=CODE_FAVORITES,
            user_id="10000001",
            tenant_id="default",
        )
        assert pool.code == CODE_FAVORITES
        create_mock.assert_awaited_once()
        save_mock.assert_awaited()

    # 已迁移则不再读旧表灌入
    migrated = created.model_copy(update={"source_ref": "migrated:qm_user_watchlist"})
    with (
        patch.object(up.repo, "ensure_tables", new=AsyncMock()),
        patch.object(up.repo, "get_pool_by_code", new=AsyncMock(return_value=migrated)),
        patch.object(up.repo, "save_members", new=AsyncMock()) as save_mock2,
        patch.object(up, "_load_legacy_symbols", new=AsyncMock()) as load_mock,
    ):
        await up.ensure_user_pool(
            session,
            code=CODE_FAVORITES,
            user_id="10000001",
            tenant_id="default",
        )
        load_mock.assert_not_awaited()
        save_mock2.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_symbol_idempotent():
    from backend.shared.stock_pool import user_pools as up
    from backend.shared.stock_pool.schemas import StockPool

    pool = StockPool(
        pool_id="sp_x",
        code=CODE_FAVORITES,
        name="我的自选",
        scope="user",
        owner_user_id="1",
        tenant_id="default",
        source_ref="migrated:qm_user_watchlist",
    )
    session = MagicMock()
    with (
        patch.object(up, "ensure_user_pool", new=AsyncMock(return_value=pool)),
        patch.object(up.repo, "read_members", return_value=["SH600036"]),
        patch.object(up.repo, "save_members", new=AsyncMock()) as save_mock,
    ):
        result = await up.add_symbol_to_user_pool(
            session,
            code=CODE_FAVORITES,
            symbol="600036.SH",
            user_id="1",
            tenant_id="default",
        )
        assert result["added"] is False
        save_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_reject_unknown_user_pool_code():
    from backend.shared.stock_pool import user_pools as up

    session = MagicMock()
    with pytest.raises(ValueError, match="仅支持"):
        await up.ensure_user_pool(
            session, code="custom", user_id="1", tenant_id="default"
        )


def test_research_code_constant():
    assert CODE_RESEARCH == "research"
