"""QuantMind OpenAPI SDK v1 数据模型（轻量 dataclass，原样透传服务端字段）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Page:
    items: list = field(default_factory=list)
    page: int = 1
    page_size: int = 30
    total: int = 0

    @classmethod
    def from_dict(cls, data: dict | None, items_key: str = "articles") -> Page:
        data = data or {}
        return cls(
            items=list(data.get(items_key) or []),
            page=int(data.get("page") or 1),
            page_size=int(data.get("page_size") or 0),
            total=int(data.get("total") or 0),
        )
