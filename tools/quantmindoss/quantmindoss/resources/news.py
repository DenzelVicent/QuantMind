"""新闻情绪资源。"""

from __future__ import annotations

from typing import Any
from collections.abc import Iterator

from ..models import Page


class NewsResource:
    """系统新闻情绪查询（scope: news.read）。"""

    def __init__(self, client):
        self._c = client

    def get_sentiment(
        self,
        tickers: str | None = None,
        sentiment: str | None = None,
        strong_only: bool = False,
        keyword: str | None = None,
        since: str | None = None,
        until: str | None = None,
        sort: str = "time_desc",
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        """查新闻情绪列表。

        tickers: 后缀式代码，逗号分隔，如 "600036.SH,000858.SZ"。
        sentiment: bullish / bearish / neutral。
        返回服务端分页 dict（含 articles/page/page_size/total）。
        """
        return self._c._request(
            "GET",
            "/api/v1/open/news/sentiment",
            params={
                "tickers": tickers,
                "sentiment": sentiment,
                "strong_only": strong_only,
                "keyword": keyword,
                "since": since,
                "until": until,
                "sort": sort,
                "page": page,
                "page_size": page_size,
            },
        )

    def get_stats(
        self,
        tickers: str | None = None,
        sentiment: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """情绪标签频次统计（供筛选器/看板用）。"""
        return self._c._request(
            "GET",
            "/api/v1/open/news/stats",
            params={"tickers": tickers, "sentiment": sentiment, "since": since, "until": until},
        )

    def iter_sentiment(self, page_size: int = 100, **kwargs) -> Iterator[dict[str, Any]]:
        """自动翻页迭代全部文章（遇到空页停止）。"""
        page = 1
        while True:
            data = self.get_sentiment(page=page, page_size=page_size, **kwargs)
            items = (data or {}).get("articles") or []
            if not items:
                return
            yield from items
            total = (data or {}).get("total") or 0
            if page * page_size >= total:
                return
            page += 1

    def page(self, data: dict | None) -> Page:
        return Page.from_dict(data)
