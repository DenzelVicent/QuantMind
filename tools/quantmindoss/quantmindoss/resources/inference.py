"""模型推理结果资源。"""

from __future__ import annotations

from typing import Any


class InferenceResource:
    """模型推理结果查询（scope: inference.read）。"""

    def __init__(self, client):
        self._c = client

    def get_latest(self, model_id: str | None = None) -> dict[str, Any]:
        """当前生效推理批次。返回 {run_id, model_id, target_date, status, ...}。"""
        return self._c._request(
            "GET", "/api/v1/open/inference/latest", params={"model_id": model_id}
        )

    def list_models(self) -> list[dict[str, Any]]:
        """当前用户的训练模型列表。每项 {model_id, status, is_default,
        created_at, updated_at}，拿到 model_id 后再查推理结果。"""
        data = self._c._request("GET", "/api/v1/open/inference/models")
        return list((data or {}).get("items") or [])

    def list_batches(
        self,
        model_id: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """批量推理历史（分页原样返回）。"""
        return self._c._request(
            "GET",
            "/api/v1/open/inference/batches",
            params={"model_id": model_id, "status": status, "page": page, "page_size": page_size},
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        """推理批次明细。返回 {summary, total, items[]}，
        每行 {symbol, fusion_score, signal_side, score_rank, ...}。"""
        return self._c._request("GET", f"/api/v1/open/inference/runs/{run_id}")

    def get_stock_history(
        self,
        symbol: str,
        days: int = 180,
        model_id: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """单只股票历史推理分数。symbol 建议后缀式（如 600036.SH，
        纯数字/SH前缀亦可）。end_date 缺省为今日，格式 YYYY-MM-DD。"""
        return self._c._request(
            "GET",
            f"/api/v1/open/inference/stock/{symbol}/history",
            params={"days": days, "model_id": model_id, "end_date": end_date},
        )

    def get_latest_signals(self, model_id: str | None = None) -> list[dict[str, Any]]:
        """便捷方法：当前生效批次的信号列表（latest → run 明细一次取全）。

        返回已按 fusion_score 降序排列；每项附带 display_rank（1 起，
        因服务端 score_rank 恒为 NULL，排名以列表顺序为准）。"""
        latest = self.get_latest(model_id)
        run_id = (latest or {}).get("run_id") or ""
        if not run_id:
            return []
        detail = self.get_run(run_id)
        items = (detail or {}).get("items")
        if items is None:  # 兼容 signals 键
            items = (detail or {}).get("signals") or []
        ranked = []
        for i, item in enumerate(items or [], start=1):
            row = dict(item or {})
            row.setdefault("display_rank", i)
            ranked.append(row)
        return ranked
