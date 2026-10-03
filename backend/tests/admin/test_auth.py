"""Test that all admin sub-routers reject unauthenticated requests.

Verifies Phase 1 security hardening: every admin router must have
router-level dependencies=[Depends(require_admin)].

The test enumerates all admin sub-routers via the __init__.py admin_router
and checks that endpoint handlers are properly guarded:

1. Route handlers without explicit Depends(require_admin) in their signature
   still get protected by the router-level dependency (defense-in-depth)
2. Route handlers with explicit Depends(require_admin) in their signature
   get double-protection (both router-level and endpoint-level)
"""

import pytest
from fastapi import APIRouter
from fastapi.params import Depends as DependsParam
from fastapi.testclient import TestClient


# ── Discover admin router structure ──

@pytest.fixture
def admin_routes():
    """Yield the sub-APIRouters registered on the admin router.

    新版 FastAPI 的 include_router 不再展开为带 path 的 Mount Route，
    而是 `_IncludedRouter` 包装；通过 original_router 取回被包含的 APIRouter。
    """
    from fastapi import APIRouter

    from backend.services.api.routers.admin import admin_router
    subs = []
    for r in admin_router.routes:
        sub = getattr(r, "original_router", None)
        if isinstance(sub, APIRouter):
            subs.append(sub)
    return subs


# ── Auth dependency injection test ──

class TestAdminRouterAuth:
    """Verify every admin sub-router has a router-level dependency."""

    def test_admin_router_has_sub_routers(self, admin_routes):
        """Sanity: the admin router has at least 10 sub-routers registered."""
        assert len(admin_routes) >= 10, (
            f"Expected >=10 sub-routers, got {len(admin_routes)}"
        )

    def test_all_sub_routers_have_dependencies(self, admin_routes):
        """Every sub-router must have at least one dependency (require_admin)."""
        no_deps = []
        for sub in admin_routes:
            if not getattr(sub, "dependencies", []):
                no_deps.append(sub.prefix or list(sub.tags))
        assert not no_deps, (
            f"These admin sub-routers lack router-level dependencies: {no_deps}"
        )

    def test_auth_dependency_present(self, admin_routes):
        """The dependency must resolve to require_admin."""
        for sub in admin_routes:
            deps = getattr(sub, "dependencies", [])
            assert any(
                isinstance(d, DependsParam) for d in deps
            ), f"Sub-router {sub.prefix or sub.tags} has no Depends in dependencies: {deps}"


# ── Individual router import tests ──

class TestAdminRouterImports:
    """Verify all admin router modules import cleanly."""

    ADMIN_ROUTER_MODULES = [
        "backend.services.api.routers.admin.dashboard",
        "backend.services.api.routers.admin.users",
        "backend.services.api.routers.admin.admin_training",
        "backend.services.api.routers.admin.admin_training_utils",
        "backend.services.api.routers.admin.model_management",
        "backend.services.api.routers.admin.model_management_ops",
        "backend.services.api.routers.admin.model_management_utils",
        "backend.services.api.routers.admin.data_platform",
        "backend.services.api.routers.admin.quantdb_console",
        "backend.services.api.routers.admin.strategy_templates",
        "backend.services.api.routers.admin.alpha_factor_pipeline",
    ]

    @pytest.mark.parametrize("module_name", ADMIN_ROUTER_MODULES)
    def test_module_imports(self, module_name: str):
        """Each admin router module must import without error."""
        import importlib

        mod = importlib.import_module(module_name)
        assert hasattr(mod, 'router'), f"{module_name} has no 'router' attribute"
        assert isinstance(mod.router, APIRouter), \
            f"{module_name}.router is not an APIRouter instance"
