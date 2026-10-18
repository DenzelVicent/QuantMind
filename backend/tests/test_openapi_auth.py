"""OpenAPI 令牌签发/验签往返测试（不碰 DB）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.api.user_app.middleware.openapi_auth import (
    TOKEN_TYPE,
    issue_openapi_token,
    resolve_effective_scopes,
    verify_openapi_token,
)


def test_issue_verify_roundtrip():
    token, ttl = issue_openapi_token(
        user_id="10000001",
        tenant_id="default",
        access_key="qm_live_test",
        scopes=["news.read"],
    )
    assert ttl == 12 * 3600
    payload = verify_openapi_token(token)
    assert payload is not None
    assert payload["typ"] == TOKEN_TYPE
    assert payload["sub"] == "10000001"
    assert payload["ak"] == "qm_live_test"
    assert payload["scopes"] == ["news.read"]


def test_tampered_token_rejected():
    token, _ = issue_openapi_token(
        user_id="u", tenant_id="t", access_key="qm_live_x", scopes=[]
    )
    assert verify_openapi_token(token + "tamper") is None
    assert verify_openapi_token("not-a-token") is None
    assert verify_openapi_token("") is None


def test_effective_scopes():
    # 老 Key（permissions 为空）全开；显式填写则取交集
    assert resolve_effective_scopes(None) == {"news.read", "inference.read", "paper.read"}
    assert resolve_effective_scopes([]) == {"news.read", "inference.read", "paper.read"}
    assert resolve_effective_scopes(["news.read"]) == {"news.read"}
    assert resolve_effective_scopes(["news.read", "bogus"]) == {"news.read"}
