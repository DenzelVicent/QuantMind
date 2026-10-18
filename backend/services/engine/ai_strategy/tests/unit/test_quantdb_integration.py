"""Unit tests for QuantDB integration in step1 and step2.

Tests cover:
1. _extract_quantdb_filters: extracting QuantDB conditions from condition trees
2. parse_conditions: verifying quantdb_filters appear in ParseResponse.mapping
3. _to_suffix_symbol: PG format to QuantDB suffix format conversion
4. _enrich_with_quantdb_data: best-effort enrichment with mocked QuantDBDataHub
"""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helper: import step1 functions without triggering heavy module-level deps
# ---------------------------------------------------------------------------
def _import_step1():
    from backend.services.engine.ai_strategy.steps.step1_stock_selection import (
        _extract_quantdb_filters,
        is_quantdb_factor,
        parse_conditions,
    )

    return _extract_quantdb_filters, is_quantdb_factor, parse_conditions


def _import_step2():
    from backend.services.engine.ai_strategy.steps.step2_pool_confirmation import (
        _enrich_with_quantdb_data,
        _to_suffix_symbol,
    )

    return _enrich_with_quantdb_data, _to_suffix_symbol


# ===========================================================================
# Test: is_quantdb_factor
# ===========================================================================
class TestIsQuantdbFactor:
    """Verify that is_quantdb_factor correctly identifies QuantDB-mapped factors."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.is_quantdb_factor = _import_step1()[1]

    def test_quantdb_factor_returns_true(self):
        assert self.is_quantdb_factor("chip_profit_ratio_20") is True
        assert self.is_quantdb_factor("dividend_rate") is True
        assert self.is_quantdb_factor("liquidity_score") is True
        assert self.is_quantdb_factor("finance_balance") is True
        assert self.is_quantdb_factor("goodwill") is True

    def test_pg_factor_returns_false(self):
        assert self.is_quantdb_factor("pe") is False
        assert self.is_quantdb_factor("market_cap") is False
        assert self.is_quantdb_factor("close") is False
        assert self.is_quantdb_factor("rsi_14") is False

    def test_unknown_factor_returns_false(self):
        assert self.is_quantdb_factor("nonexistent_factor_xyz") is False


# ===========================================================================
# Test: _extract_quantdb_filters
# ===========================================================================
class TestExtractQuantdbFilters:
    """Verify extraction of QuantDB filters from condition trees."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.extract = _import_step1()[0]

    def test_single_numeric_quantdb_condition(self):
        conditions = {
            "type": "numeric",
            "factor": "chip_profit_ratio_20",
            "operator": ">",
            "threshold": 60,
        }
        result = self.extract(conditions)
        assert len(result) == 1
        assert result[0] == {
            "field": "chip_profit_ratio_20",
            "operator": ">",
            "value": 60,
            "table": "quantdb_factors",
        }

    def test_single_numeric_pg_condition_yields_empty(self):
        conditions = {
            "type": "numeric",
            "factor": "pe",
            "operator": "<",
            "threshold": 20,
        }
        result = self.extract(conditions)
        assert result == []

    def test_trend_quantdb_condition(self):
        conditions = {
            "type": "trend",
            "factor": "liquidity_score",
            "window": 5,
            "direction": "up",
        }
        result = self.extract(conditions)
        assert len(result) == 1
        assert result[0]["field"] == "liquidity_score"
        assert result[0]["operator"] == ">"
        assert result[0]["value"] == 0
        assert result[0]["table"] == "quantdb_sentiment"

    def test_trend_quantdb_condition_down(self):
        conditions = {
            "type": "trend",
            "factor": "ind_crowding_20",
            "window": 10,
            "direction": "down",
        }
        result = self.extract(conditions)
        assert len(result) == 1
        assert result[0]["operator"] == "<"

    def test_composite_mixed_conditions(self):
        conditions = {
            "type": "composite",
            "op": "AND",
            "children": [
                {
                    "type": "numeric",
                    "factor": "pe",
                    "operator": "<",
                    "threshold": 20,
                },
                {
                    "type": "numeric",
                    "factor": "chip_profit_ratio_20",
                    "operator": ">",
                    "threshold": 60,
                },
                {
                    "type": "numeric",
                    "factor": "dividend_rate",
                    "operator": ">=",
                    "threshold": 3.0,
                },
            ],
        }
        result = self.extract(conditions)
        assert len(result) == 2
        fields = {r["field"] for r in result}
        assert fields == {"chip_profit_ratio_20", "dividend_rate"}

    def test_composite_all_pg_conditions(self):
        conditions = {
            "type": "composite",
            "op": "AND",
            "children": [
                {"type": "numeric", "factor": "pe", "operator": "<", "threshold": 20},
                {"type": "numeric", "factor": "market_cap", "operator": ">", "threshold": 100},
            ],
        }
        result = self.extract(conditions)
        assert result == []

    def test_nested_composite(self):
        conditions = {
            "type": "composite",
            "op": "AND",
            "children": [
                {
                    "type": "composite",
                    "op": "OR",
                    "children": [
                        {
                            "type": "numeric",
                            "factor": "chip_profit_ratio_20",
                            "operator": ">",
                            "threshold": 60,
                        },
                        {
                            "type": "numeric",
                            "factor": "style_beta_20",
                            "operator": ">",
                            "threshold": 0.8,
                        },
                    ],
                },
                {"type": "numeric", "factor": "pe", "operator": "<", "threshold": 30},
            ],
        }
        result = self.extract(conditions)
        assert len(result) == 2
        tables = {r["table"] for r in result}
        assert tables == {"quantdb_factors"}

    def test_valuation_table_mapping(self):
        conditions = {
            "type": "numeric",
            "factor": "ps_ttm",
            "operator": "<",
            "threshold": 5.0,
        }
        result = self.extract(conditions)
        assert len(result) == 1
        assert result[0]["table"] == "quantdb_valuation"
        assert result[0]["field"] == "ps_ttm"

    def test_margin_table_mapping(self):
        conditions = {
            "type": "numeric",
            "factor": "finance_balance",
            "operator": ">",
            "threshold": 0,
        }
        result = self.extract(conditions)
        assert len(result) == 1
        assert result[0]["table"] == "quantdb_margin"

    def test_financial_table_mapping(self):
        conditions = {
            "type": "numeric",
            "factor": "goodwill",
            "operator": "<",
            "threshold": 1e9,
        }
        result = self.extract(conditions)
        assert len(result) == 1
        assert result[0]["table"] == "quantdb_financial"


# ===========================================================================
# Test: parse_conditions returns quantdb_filters in mapping
# ===========================================================================
class TestParseConditionsQuantdbFilters:
    """Verify that parse_conditions includes quantdb_filters in the mapping."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.parse_conditions = _import_step1()[2]

    def test_no_quantdb_conditions_no_key(self):
        conditions = {
            "type": "numeric",
            "factor": "pe",
            "operator": "<",
            "threshold": 20,
        }
        result = self.parse_conditions(conditions)
        assert "quantdb_filters" not in result.mapping

    def test_with_quantdb_conditions_has_key(self):
        conditions = {
            "type": "numeric",
            "factor": "chip_profit_ratio_20",
            "operator": ">",
            "threshold": 60,
        }
        result = self.parse_conditions(conditions)
        assert "quantdb_filters" in result.mapping
        assert len(result.mapping["quantdb_filters"]) == 1
        assert result.mapping["quantdb_filters"][0]["field"] == "chip_profit_ratio_20"

    def test_dsl_still_generated_correctly(self):
        conditions = {
            "type": "numeric",
            "factor": "chip_profit_ratio_20",
            "operator": ">",
            "threshold": 60,
        }
        result = self.parse_conditions(conditions)
        assert result.dsl.startswith("SELECT symbol WHERE")
        assert "chip_profit_ratio_20" in result.dsl

    def test_mixed_conditions_both_dsl_and_filters(self):
        conditions = {
            "type": "composite",
            "op": "AND",
            "children": [
                {"type": "numeric", "factor": "pe", "operator": "<", "threshold": 20},
                {"type": "numeric", "factor": "dividend_rate", "operator": ">", "threshold": 3.0},
            ],
        }
        result = self.parse_conditions(conditions)
        assert "quantdb_filters" in result.mapping
        assert len(result.mapping["quantdb_filters"]) == 1
        assert result.mapping["quantdb_filters"][0]["field"] == "dividend_rate"
        # DSL should still contain both factors
        assert "pe" in result.dsl
        assert "dividend_rate" in result.dsl


# ===========================================================================
# Test: _to_suffix_symbol
# ===========================================================================
class TestToSuffixSymbol:
    """Verify PG internal format to QuantDB suffix format conversion."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.convert = _import_step2()[1]

    def test_sh_prefix(self):
        assert self.convert("SH600036") == "600036.SH"

    def test_sz_prefix(self):
        assert self.convert("SZ000001") == "000001.SZ"

    def test_bj_prefix(self):
        assert self.convert("BJ430047") == "430047.BJ"

    def test_already_suffix_format(self):
        assert self.convert("600036.SH") == "600036.SH"

    def test_lowercase_sh_prefix(self):
        assert self.convert("sh600036") == "600036.SH"

    def test_no_prefix_passthrough(self):
        # 中枢收敛后：纯 6 位数字按号段自动识别（600036 -> 600036.SH），不再原样透传
        assert self.convert("600036") == "600036.SH"


# ===========================================================================
# Test: _enrich_with_quantdb_data
# ===========================================================================
class TestEnrichWithQuantdbData:
    """Verify QuantDB enrichment with mocked data hub."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.enrich = _import_step2()[0]

    def test_empty_symbols_returns_empty(self):
        result = self.enrich([])
        assert result == {}

    @patch("backend.services.engine.ai_strategy.steps.step2_pool_confirmation.QuantDBDataHub", create=True)
    def test_hub_unavailable_returns_empty(self, mock_hub_cls):
        """When QuantDBDataHub is unavailable, return empty dict without error."""
        mock_instance = MagicMock()
        mock_instance.available = False
        mock_hub_cls.get_instance.return_value = mock_instance

        # Patch the import inside the function
        with patch.dict("sys.modules", {"backend.services.engine.data_platform.quantdb_hub": MagicMock(QuantDBDataHub=mock_hub_cls)}):
            result = self.enrich(["SH600036"])
            assert result == {}

    def test_import_error_returns_empty(self):
        """When QuantDBDataHub cannot be imported, return empty dict."""
        with patch.dict("sys.modules", {"backend.services.engine.data_platform.quantdb_hub": None}):
            result = self.enrich(["SH600036"])
            assert result == {}

    def test_valuation_enrichment_with_mock(self):
        """Test valuation data enrichment with a mocked hub."""
        enrich_fn = self.enrich

        # Build mock DataFrames
        val_df = pd.DataFrame({
            "symbol": ["600036.SH", "000001.SZ"],
            "trade_date": pd.to_datetime(["2026-07-30", "2026-07-30"]),
            "dividend_rate": [3.5, 2.1],
            "ps_ttm": [4.2, 6.8],
            "pe_static": [12.0, 18.5],
        })
        sent_df = pd.DataFrame({
            "symbol": ["600036.SH"],
            "trade_date": pd.to_datetime(["2026-07-30"]),
            "liquidity_score": [0.85],
            "buy_pressure": [0.12],
            "momentum_1d": [0.03],
        })
        l1_df = pd.DataFrame({
            "symbol": ["600036.SH"],
            "trade_date": pd.to_datetime(["2026-07-30"]),
            "chip_profit_ratio_20": [72.5],
            "ind_strength_20": [0.45],
            "style_beta_20": [1.1],
        })

        mock_hub = MagicMock()
        mock_hub.available = True
        mock_hub.fetch_valuation.return_value = val_df
        mock_hub.fetch_market_sentiment.return_value = sent_df
        mock_hub.fetch_l1_factors.return_value = l1_df

        with patch(
            "backend.services.engine.ai_strategy.steps.step2_pool_confirmation.QuantDBDataHub",
            create=True,
        ) as mock_cls:
            mock_cls.get_instance.return_value = mock_hub

            # We need to patch the import inside the function
            with patch.dict(
                "sys.modules",
                {"backend.services.engine.data_platform.quantdb_hub": MagicMock(QuantDBDataHub=mock_cls)},
            ):
                result = enrich_fn(["SH600036", "SZ000001"])

        # SH600036 should have all enrichment fields
        assert "SH600036" in result
        assert result["SH600036"]["dividend_rate"] == 3.5
        assert result["SH600036"]["ps_ttm"] == 4.2
        assert result["SH600036"]["liquidity_score"] == 0.85
        assert result["SH600036"]["chip_profit_ratio_20"] == 72.5

        # SZ000001 should have valuation data
        assert "SZ000001" in result
        assert result["SZ000001"]["dividend_rate"] == 2.1

    def test_nan_values_excluded(self):
        """NaN values in QuantDB data should not appear in enrichment."""
        val_df = pd.DataFrame({
            "symbol": ["600036.SH"],
            "trade_date": pd.to_datetime(["2026-07-30"]),
            "dividend_rate": [float("nan")],
            "ps_ttm": [4.2],
        })

        mock_hub = MagicMock()
        mock_hub.available = True
        mock_hub.fetch_valuation.return_value = val_df
        mock_hub.fetch_market_sentiment.return_value = pd.DataFrame()
        mock_hub.fetch_l1_factors.return_value = pd.DataFrame()

        with patch.dict(
            "sys.modules",
            {"backend.services.engine.data_platform.quantdb_hub": MagicMock(QuantDBDataHub=MagicMock(get_instance=MagicMock(return_value=mock_hub)))},
        ):
            result = self.enrich(["SH600036"])

        if "SH600036" in result:
            assert "dividend_rate" not in result["SH600036"]
            assert result["SH600036"]["ps_ttm"] == 4.2

    def test_exception_in_fetch_does_not_propagate(self):
        """If a QuantDB fetch raises, enrichment should return empty without error."""
        mock_hub = MagicMock()
        mock_hub.available = True
        mock_hub.fetch_valuation.side_effect = RuntimeError("DuckDB error")

        with patch.dict(
            "sys.modules",
            {"backend.services.engine.data_platform.quantdb_hub": MagicMock(QuantDBDataHub=MagicMock(get_instance=MagicMock(return_value=mock_hub)))},
        ):
            result = self.enrich(["SH600036"])
            # Should not raise, and should return empty or partial result
            assert isinstance(result, dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
