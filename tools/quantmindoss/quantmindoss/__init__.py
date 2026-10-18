"""QuantMind OpenAPI SDK v1。

快速开始::

    from quantmindoss import QuantMindClient

    qm = QuantMindClient(
        "http://<服务器IP>:8000",
        access_key="qm_live_xxx",
        secret_key="sk_xxx",  # 仅创建时显示一次，请妥善保存
    )
    qm.news.get_sentiment(tickers="600036.SH")
    qm.inference.get_latest()
    qm.paper.get_positions(market="CN")
"""

from .client import QuantMindClient, __version__
from .errors import (
    AuthError,
    NotFoundError,
    PermissionError,
    QuantMindError,
    RateLimitError,
    ServerError,
)
from .models import Page

__all__ = [
    "QuantMindClient",
    "QuantMindError",
    "AuthError",
    "PermissionError",
    "NotFoundError",
    "RateLimitError",
    "ServerError",
    "Page",
    "__version__",
]
