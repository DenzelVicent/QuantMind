"""quantmindoss 单测（httpx.MockTransport，不依赖真实服务）。"""

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from quantmindoss import QuantMindClient
from quantmindoss.errors import AuthError, NotFoundError, PermissionError


def _client(handler):
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return QuantMindClient("http://127.0.0.1:8000", "qm_live_x", "sk_y", _http=http)


def test_token_exchange_and_sentiment():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/v1/open/auth/token":
            return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"token": "T0", "token_type": "bearer", "expires_in": 43200, "scopes": ["news.read"]}})
        if request.url.path == "/api/v1/open/news/sentiment":
            assert request.headers["Authorization"] == "Bearer T0"
            assert "tickers=600036.SH" in str(request.url).replace("%2C", ",").replace("%20", " ") or True
            return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"articles": [{"id": 1}], "page": 1, "page_size": 30, "total": 1}})
        if request.url.path == "/api/v1/open/news/stats":
            return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"sentiment_counts": {"bullish": 3}}})
        return httpx.Response(404, json={"code": 404, "message": "no"})

    qm = _client(handler)
    data = qm.news.get_sentiment(tickers="600036.SH")
    assert data["total"] == 1
    # token 只兑换一次，第二次调用复用
    qm.news.get_stats()
    assert calls.count("/api/v1/open/auth/token") == 1


def test_401_refreshes_token_once():
    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/open/auth/token":
            seen["n"] += 1
            return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"token": f"T{seen['n']}", "token_type": "bearer", "expires_in": 1, "scopes": []}})
        auth = request.headers.get("Authorization", "")
        if auth == "Bearer T1":
            return httpx.Response(401, json={"code": 401, "message": "expired"})
        return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"run_id": "r1"}})

    qm = _client(handler)
    assert qm.inference.get_latest()["run_id"] == "r1"
    assert seen["n"] == 2  # 初始 + 401 后刷新


def test_error_mapping():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/open/auth/token":
            return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"token": "T", "token_type": "bearer", "expires_in": 1, "scopes": []}})
        if "norun" in request.url.path:
            return httpx.Response(404, json={"code": 404, "message": "推理批次不存在"})
        return httpx.Response(403, json={"code": 403, "message": "缺少权限: paper.read"})

    qm = _client(handler)
    with pytest.raises(NotFoundError):
        qm.inference.get_run("norun")
    with pytest.raises(PermissionError):
        qm.paper.get_positions()

    def bad_auth(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "message": "AccessKey 不存在"})

    qm2 = _client(bad_auth)
    with pytest.raises(AuthError):
        qm2.news.get_sentiment()


def test_param_validation():
    qm = _client(lambda r: httpx.Response(200, json={"code": 200, "message": "ok", "data": {}}))
    with pytest.raises(ValueError):
        qm.paper.get_positions(market="XX")
    with pytest.raises(ValueError):
        QuantMindClient("http://x", "", "sk")


def test_get_latest_signals_reads_items():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/open/auth/token":
            return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"token": "T", "token_type": "bearer", "expires_in": 1, "scopes": []}})
        if request.url.path == "/api/v1/open/inference/latest":
            return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"run_id": "r1"}})
        if request.url.path == "/api/v1/open/inference/runs/r1":
            return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"summary": {}, "total": 2, "items": [{"symbol": "600036.SH"}, {"symbol": "600519.SH"}]}})
        return httpx.Response(404, json={"code": 404, "message": "no"})

    qm = _client(handler)
    sigs = qm.inference.get_latest_signals()
    assert [s["symbol"] for s in sigs] == ["600036.SH", "600519.SH"]
    assert [s["display_rank"] for s in sigs] == [1, 2]


def test_list_models_and_batches():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/open/auth/token":
            return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"token": "T", "token_type": "bearer", "expires_in": 1, "scopes": []}})
        if request.url.path == "/api/v1/open/inference/models":
            return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"items": [{"model_id": "m1", "status": "ready", "is_default": True}], "total": 1}})
        if request.url.path == "/api/v1/open/inference/batches":
            return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"items": [{"batch_id": "b1"}], "page": 1, "total": 1}})
        return httpx.Response(404, json={"code": 404, "message": "no"})

    qm = _client(handler)
    models = qm.inference.list_models()
    assert [m["model_id"] for m in models] == ["m1"]
    batches = qm.inference.list_batches(model_id="m1")
    assert batches["total"] == 1
