"""向量化极速回测「智能路由」安全检测单元测试。

验证 _is_vectorized_safe / _extract_strategy_config_from_code 对
各种策略形态的正确分类：纯 TopK 型走向量化，含自定义逻辑退回 step。
"""

import types

import pytest

from backend.services.engine.qlib_app.services.backtest_service_runtime import (
    QlibBacktestServiceRuntimeMixin,
)

# 用 unbound 方法避免实例化 QlibBacktestService（依赖 qlib/db）
_cls = QlibBacktestServiceRuntimeMixin


def _make_request(content: str, signal: str = "<PRED>"):
    """构造一个最小 request 桩对象，提供 _is_vectorized_safe 所需的字段。"""
    return types.SimpleNamespace(
        strategy_content=content,
        strategy_params=types.SimpleNamespace(signal=signal),
        strategy_type="CustomStrategy",
        qlib_region="us",
    )


def _safe(request, strategy=None):
    """以最小桩 self 调用 _is_vectorized_safe。"""
    stub_self = types.SimpleNamespace()
    # 绑定静态方法，避免依赖实例属性 / qlib 运行时
    stub_self._normalize_signal_config = (
        QlibBacktestServiceRuntimeMixin._normalize_signal_config
    )
    stub_self._extract_strategy_config_from_code = (
        QlibBacktestServiceRuntimeMixin._extract_strategy_config_from_code
    )
    stub_self._vectorized_unsafe_strategy_class = (
        QlibBacktestServiceRuntimeMixin._vectorized_unsafe_strategy_class
    )
    return _cls._is_vectorized_safe(stub_self, request, strategy)


def test_safe_standard_topk_full_rebalance():
    # topk=n_drop（每期全换）→ 向量化安全
    req = _make_request(
        'STRATEGY_CONFIG = {"class": "RedisTopkStrategy", '
        '"kwargs": {"signal": "<PRED>", "topk": 50, "n_drop": 50}}'
    )
    assert _safe(req) is True


@pytest.mark.parametrize("region", [None, "cn"])
def test_cn_routes_to_step_for_strict_price_limit_execution(region):
    req = _make_request(
        'STRATEGY_CONFIG = {"class": "RedisTopkStrategy", '
        '"kwargs": {"signal": "<PRED>", "topk": 50, "n_drop": 50}}'
    )
    req.qlib_region = region
    assert _safe(req) is False


def test_safe_topkdropout_no_partial_rebalance():
    req = _make_request(
        'STRATEGY_CONFIG = {"class": "TopkDropout", '
        '"kwargs": {"signal": "<PRED>", "topk": 50}}'
    )
    assert _safe(req) is True


def test_unsafe_standard_topk_partial_rebalance():
    # 默认模板 standard_topk：topk=50, n_drop=10 → 部分调仓，向量化无法表达
    req = _make_request(
        'STRATEGY_CONFIG = {"class": "RedisTopkStrategy", '
        '"kwargs": {"signal": "<PRED>", "topk": 50, "n_drop": 10}}'
    )
    assert _safe(req) is False


def test_unsafe_momentum():
    # 默认模板 momentum：topk=30, n_drop=6 → 部分调仓 + 动量逻辑
    req = _make_request(
        'STRATEGY_CONFIG = {"class": "RedisTopkStrategy", '
        '"kwargs": {"topk": 30, "n_drop": 6, "momentum_period": 20}}'
    )
    assert _safe(req) is False


def test_unsafe_weighted_strategy():
    req = _make_request(
        'STRATEGY_CONFIG = {"class": "RedisWeightStrategy", '
        '"kwargs": {"signal": "<PRED>", "topk": 50, "max_weight": 0.05}}'
    )
    assert _safe(req) is False


def test_unsafe_rebalance_days():
    req = _make_request(
        'STRATEGY_CONFIG = {"class": "RedisTopkStrategy", '
        '"kwargs": {"signal": "<PRED>", "topk": 10, "rebalance_days": 5}}'
    )
    assert _safe(req) is False


def test_unsafe_pool_file():
    req = _make_request(
        'STRATEGY_CONFIG = {"class": "RedisTopkStrategy", '
        '"kwargs": {"signal": "<PRED>", "topk": 10, "pool_file": "/app/pool.txt"}}'
    )
    assert _safe(req) is False


def test_unsafe_custom_class():
    req = _make_request(
        "class MyCustom:\n"
        "    def __init__(self, signal, topk=50):\n"
        "        pass\n"
        'STRATEGY_CONFIG = {"class": "MyCustom", '
        '"kwargs": {"signal": "<PRED>", "topk": 10}}'
    )
    assert _safe(req) is False


def test_unsafe_stop_loss_in_config():
    req = _make_request(
        'STRATEGY_CONFIG = {"class": "RedisTopkStrategy", '
        '"kwargs": {"signal": "<PRED>", "topk": 10, "stop_loss": -0.08}}'
    )
    assert _safe(req) is False


def test_unsafe_feature_signal_not_pred():
    req = _make_request(
        'STRATEGY_CONFIG = {"class": "RedisTopkStrategy", '
        '"kwargs": {"signal": "$close", "topk": 50}}',
        signal="$close",
    )
    assert _safe(req) is False


def test_unsafe_no_config_no_strategy():
    req = _make_request("print('hello')")
    assert _safe(req) is False


def test_unsafe_strategy_dict_unknown_class():
    # strategy 传入 dict，无 STRATEGY_CONFIG，类名非 TopK → 不安全
    req = _make_request("x = 1")
    strategy_dict = {"class": "SomeCustomStrategy", "kwargs": {}}
    assert _safe(req, strategy_dict) is False


def test_extract_strategy_config_dict():
    content = (
        'STRATEGY_CONFIG = {"class": "RedisTopkStrategy", '
        '"kwargs": {"signal": "<PRED>", "topk": 50}}'
    )
    cfg = _cls._extract_strategy_config_from_code(content)
    assert cfg["class"] == "RedisTopkStrategy"
    assert cfg["kwargs"]["topk"] == 50


def test_extract_strategy_config_missing():
    assert _cls._extract_strategy_config_from_code("x = 1") == {}
    assert _cls._extract_strategy_config_from_code("") == {}


def test_unsafe_strategy_class_classifier():
    assert _cls._vectorized_unsafe_strategy_class("RedisTopkStrategy") is False
    assert _cls._vectorized_unsafe_strategy_class("TopkDropout") is False
    assert _cls._vectorized_unsafe_strategy_class("RedisWeightStrategy") is True
    assert _cls._vectorized_unsafe_strategy_class("MyCustom") is True
    assert _cls._vectorized_unsafe_strategy_class("") is True
