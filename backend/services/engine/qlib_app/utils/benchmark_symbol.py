"""Benchmark symbol normalization utilities for Qlib data lookup."""

from __future__ import annotations


# Qlib 实际数据中的指数 instrument 代码为小写（features/sh000300 等），
# 因此 canonical 直接映射到小写代码，候选列表再补充历史大写/IDX_ 命名空间以兼容。
_ALIAS_TO_CANONICAL = {
    "CSI300": "sh000300",
    "HS300": "sh000300",
    "000300": "sh000300",
    "000300.SH": "sh000300",
    "SH000300": "sh000300",
    "IDX_SH000300": "sh000300",
    "SZ399300": "sh000300",
    "399300.SZ": "sh000300",
    "CSI500": "sh000905",
    "ZZ500": "sh000905",
    "000905": "sh000905",
    "000905.SH": "sh000905",
    "SH000905": "sh000905",
    "IDX_SH000905": "sh000905",
    "CSI1000": "sh000852",
    "ZZ1000": "sh000852",
    "000852": "sh000852",
    "000852.SH": "sh000852",
    "SH000852": "sh000852",
    "IDX_SH000852": "sh000852",
    "SZ000852": "sh000852",
    "SZ000905": "sh000905",
}


def normalize_benchmark_symbol(symbol: str | None) -> str:
    raw = (symbol or "").strip().upper()
    if not raw:
        return "sh000300"
    if raw.startswith("IDX_SH") or raw.startswith("IDX_SZ"):
        raw = raw[4:]
    return _ALIAS_TO_CANONICAL.get(raw, raw.lower())


def benchmark_candidates(symbol: str | None) -> list[str]:
    """Return ordered symbol candidates for lookup.

    优先使用 Qlib 真实数据中的小写代码（如 sh000300），
    再补充 IDX_ 命名空间与历史大写代码，最大化兼容不同数据落地方式。
    """
    canonical = normalize_benchmark_symbol(symbol)
    candidates: list[str] = [canonical]
    upper = canonical.upper()
    if not upper.startswith("IDX_"):
        candidates.append("IDX_" + upper)
    candidates.append(upper)
    # 沪深300 历史别名
    if canonical == "sh000300":
        candidates.append("SZ399300")
    seen = set()
    ordered: list[str] = []
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered

