"""QuantMind OpenAPI SDK v1 异常定义。"""


class QuantMindError(Exception):
    """SDK 基础异常。"""

    def __init__(self, message: str, *, http_status: int | None = None, code=None):
        super().__init__(message)
        self.http_status = http_status
        self.code = code


class AuthError(QuantMindError):
    """401：AK/SK 无效、令牌过期且刷新失败。"""


class PermissionError(QuantMindError):
    """403：API Key 缺少对应 scope。"""


class NotFoundError(QuantMindError):
    """404：资源不存在（如推理批次号错误）。"""


class RateLimitError(QuantMindError):
    """429：请求超限。"""


class ServerError(QuantMindError):
    """5xx：服务端异常。"""


def raise_for_envelope(resp_json, http_status: int):
    """按统一包络 {code, message, data} 抛对应异常；成功时返回 data。"""
    if isinstance(resp_json, dict) and "code" in resp_json:
        code = resp_json.get("code")
        message = resp_json.get("message") or "请求失败"
        if code == 200:
            return resp_json.get("data")
        if http_status == 401 or code == 401:
            raise AuthError(message, http_status=http_status, code=code)
        if http_status == 403 or code == 403:
            raise PermissionError(message, http_status=http_status, code=code)
        if http_status == 404 or code == 404:
            raise NotFoundError(message, http_status=http_status, code=code)
        if http_status == 429 or code == 429:
            raise RateLimitError(message, http_status=http_status, code=code)
        if (http_status is not None and http_status >= 500) or (isinstance(code, int) and code >= 500):
            raise ServerError(message, http_status=http_status, code=code)
        raise QuantMindError(message, http_status=http_status, code=code)
    # 非包络响应（兼容）：2xx 直接返回
    if http_status is not None and 200 <= http_status < 300:
        return resp_json
    raise QuantMindError(f"请求失败: HTTP {http_status}", http_status=http_status)
