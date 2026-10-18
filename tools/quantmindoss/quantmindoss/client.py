"""QuantMind OpenAPI SDK 客户端。"""

from __future__ import annotations

import time
from typing import Any

import httpx

from .errors import QuantMindError, ServerError, raise_for_envelope
from .resources.inference import InferenceResource
from .resources.news import NewsResource
from .resources.paper import PaperResource

__version__ = "0.1.3"

_TOKEN_PATH = "/api/v1/open/auth/token"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class QuantMindClient:
    """QuantMind OpenAPI v1 客户端。

    用法::

        from quantmindoss import QuantMindClient
        qm = QuantMindClient("http://192.168.1.10:8000",
                             access_key="qm_live_xxx", secret_key="sk_xxx")
        qm.news.get_sentiment(tickers="600036.SH")
        qm.inference.get_latest()
        qm.paper.get_positions(market="CN")

    AK/SK 获取：登录管理后台 → API Keys → 新建（permissions 建议
    ["news.read", "inference.read", "paper.read"]），secret 仅显示一次。
    """

    def __init__(
        self,
        base_url: str,
        access_key: str,
        secret_key: str,
        timeout: float = 10.0,
        max_retries: int = 3,
        _http: httpx.Client | None = None,
    ):
        if not base_url or not access_key or not secret_key:
            raise ValueError("base_url / access_key / secret_key 均不能为空")
        self.base_url = base_url.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self._http = _http or httpx.Client(timeout=timeout, trust_env=False)
        self._token: str | None = None

        self.news = NewsResource(self)
        self.inference = InferenceResource(self)
        self.paper = PaperResource(self)

    # -- 鉴权 ----------------------------------------------------------
    def _exchange_token(self) -> str:
        resp = self._http.post(
            self.base_url + _TOKEN_PATH,
            json={"access_key": self.access_key, "secret_key": self.secret_key},
        )
        try:
            body = resp.json()
        except Exception as exc:
            raise QuantMindError(
                f"令牌兑换失败: HTTP {resp.status_code} 非 JSON", http_status=resp.status_code
            ) from exc
        data = raise_for_envelope(body, resp.status_code)
        token = (data or {}).get("token") or ""
        if not token:
            raise QuantMindError("令牌兑换失败: 服务端未返回 token", http_status=resp.status_code)
        self._token = token
        return token

    def _ensure_token(self) -> str:
        return self._token or self._exchange_token()

    # -- 请求 ----------------------------------------------------------
    def _request(self, method: str, path: str, params: dict | None = None) -> Any:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        token = self._ensure_token()
        try:
            return self._do(method, path, query, token)
        except _Unauthorized:
            # 令牌过期：刷新一次后重试
            token = self._exchange_token()
            return self._do(method, path, query, token)

    def _do(self, method: str, path: str, query: dict, token: str) -> Any:
        url = self.base_url + path
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.request(
                    method, url, params=query,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                last_exc = QuantMindError(f"网络错误: {exc}")
                if attempt < self.max_retries:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise last_exc from exc
            if resp.status_code == 401:
                raise _Unauthorized()
            if resp.status_code in _RETRYABLE_STATUS and method.upper() == "GET" \
                    and attempt < self.max_retries:
                time.sleep(0.5 * (2 ** attempt))
                continue
            try:
                body = resp.json()
            except Exception as exc:
                raise ServerError(
                    f"服务端返回非 JSON: HTTP {resp.status_code}", http_status=resp.status_code
                ) from exc
            return raise_for_envelope(body, resp.status_code)
        raise last_exc or QuantMindError("请求失败")

    def close(self):
        try:
            self._http.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _Unauthorized(Exception):
    """内部：401 触发令牌刷新。"""
