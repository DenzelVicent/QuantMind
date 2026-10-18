"""
OpenAPI (SDK) 鉴权中间件
========================

背景：`api_keys` 表只做了发行（`qm_live_*` / `sk_*`），没有任何 HTTP 入口消费。
本模块补上 AK/SK 的服务端消费端，供 `POST /api/v1/open/auth/token` 兑换
短期访问令牌，SDK 后续请求携带该令牌访问 `/api/v1/open/*` 只读接口。

为什么用「兑换令牌」而不是每请求 HMAC 签名：
服务端只存 SK 的 bcrypt 哈希（不可逆），无法像 HMAC 那样用原文重算签名；
SK 只在兑换时传输一次，后续走短期 Bearer 令牌，安全属性等价、实现更简单。

令牌：JWT（HS256，复用 user_app 的 SECRET_KEY/ALGORITHM），claims：
  typ="qm_open"（与用户登录 JWT 隔离，互不承认）、sub=user_id、
  tenant_id、ak=access_key、scopes、exp/iat。默认 TTL 12 小时。
每次请求除验签外还会回查 api_keys 行（禁用/删除即时吊销）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from backend.services.api.user_app.config import settings
from backend.services.api.user_app.models.api_key import ApiKey
from backend.services.api.user_app.services.api_key_service import ApiKeyService
from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)

TOKEN_TYPE = "qm_open"
OPENAPI_TOKEN_TTL_SECONDS = 12 * 3600

# v1 只读 scope（存在 api_keys.permissions JSONB 中）
SCOPE_NEWS_READ = "news.read"
SCOPE_INFERENCE_READ = "inference.read"
SCOPE_PAPER_READ = "paper.read"
ALL_SCOPES = frozenset({SCOPE_NEWS_READ, SCOPE_INFERENCE_READ, SCOPE_PAPER_READ})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(expires_at: datetime | None) -> bool:
    if not expires_at:
        return False
    if expires_at.tzinfo is None:
        return expires_at < datetime.now()
    return expires_at < _utcnow()


def resolve_effective_scopes(permissions) -> frozenset:
    """permissions 为空（老 Key 未填）视为全开，保证存量 Key 可用；
    显式填写则按交集强制执行。"""
    if not permissions:
        return ALL_SCOPES
    return frozenset(str(p) for p in permissions) & ALL_SCOPES


def issue_openapi_token(*, user_id: str, tenant_id: str, access_key: str, scopes) -> tuple[str, int]:
    now = _utcnow()
    exp = now + timedelta(seconds=OPENAPI_TOKEN_TTL_SECONDS)
    payload = {
        "typ": TOKEN_TYPE,
        "sub": str(user_id),
        "tenant_id": str(tenant_id or "default"),
        "ak": access_key,
        "scopes": sorted(set(scopes)),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return token, OPENAPI_TOKEN_TTL_SECONDS


def verify_openapi_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("typ") != TOKEN_TYPE:
        return None
    if not payload.get("sub") or not payload.get("ak"):
        return None
    return payload


async def get_openapi_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """SDK 访问令牌依赖：返回与 get_current_user 同形的用户 dict。"""
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing OpenAPI token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = verify_openapi_token(credentials.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效或已过期的 OpenAPI 令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # 吊销检查：Key 被禁用/删除后，已发令牌立即失效
    async with get_session(read_only=True) as session:
        result = await session.execute(select(ApiKey).where(ApiKey.access_key == payload["ak"]))
        key = result.scalar_one_or_none()
    if key is None or not key.is_active or _is_expired(key.expires_at):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API Key 已禁用、过期或不存在",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {
        "user_id": str(payload["sub"]),
        "tenant_id": str(payload.get("tenant_id") or "default"),
        "username": "openapi",
        "email": "",
        "access_key": str(payload["ak"]),
        "scopes": list(payload.get("scopes") or []),
    }


def require_openapi_scope(scope: str):
    """scope 检查依赖工厂，用法：`user=Depends(require_openapi_scope("news.read"))`。"""

    async def _checker(user: dict = Depends(get_openapi_user)) -> dict:
        if scope not in (user.get("scopes") or []):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"该 API Key 缺少权限: {scope}",
            )
        return user

    return _checker


async def exchange_token(access_key: str, secret_key: str) -> dict:
    """AK/SK 兑换访问令牌（POST /api/v1/open/auth/token 的实现）。"""
    from datetime import datetime as _dt

    async with get_session(read_only=False) as session:
        service = ApiKeyService(session)
        result = await session.execute(select(ApiKey).where(ApiKey.access_key == (access_key or "").strip()))
        key = result.scalar_one_or_none()
        if key is None or not key.is_active or _is_expired(key.expires_at):
            raise HTTPException(status_code=401, detail="AccessKey 不存在、已禁用或已过期")
        if not secret_key or not service.verify_secret(secret_key, key.secret_hash):
            raise HTTPException(status_code=401, detail="SecretKey 无效")
        key.last_used_at = _dt.now()
        await session.commit()
        scopes = sorted(resolve_effective_scopes(key.permissions))
        token, ttl = issue_openapi_token(
            user_id=str(key.user_id),
            tenant_id=str(key.tenant_id or "default"),
            access_key=str(key.access_key),
            scopes=scopes,
        )
    return {
        "token": token,
        "token_type": "bearer",
        "expires_in": ttl,
        "scopes": scopes,
    }
