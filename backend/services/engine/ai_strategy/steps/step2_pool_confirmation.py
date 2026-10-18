"""Step 2: 股票池确认 - 执行查询并展示结果"""

import logging
import os
import re
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import HTTPException, Request, status
from sqlalchemy import text

from backend.shared.stock_utils import StockCodeUtil

from ..api.schemas.stock_pool import PoolItem, QueryPoolResponse

try:
    from backend.shared.database_pool import get_database_pool, get_db
except ImportError:
    try:
        from shared.database_pool import get_database_pool, get_db
    except ImportError:
        try:
            from backend.shared.strategy_storage import get_db

            def get_database_pool():
                raise ImportError("database_pool not available")

        except ImportError:
            try:
                from shared.strategy_storage import get_db

                def get_database_pool():
                    raise ImportError("database_pool not available")

            except ImportError:

                def get_database_pool():
                    raise ImportError("Cannot find database_pool module")

                def get_db():
                    raise ImportError("Cannot find database_pool module")


from ..services.validators.sql_validator import (
    SQLValidationError,
    safe_table_replace,
    validate_and_sanitize,
)
from .step1_stock_selection import (
    DSL_PREFIX,
    LATEST_TABLE,
    _map_factor,
    _parse_dsl,
    is_quantdb_factor,
    split_conditions_by_source,
)

logger = logging.getLogger(__name__)
TOTAL_MV_PER_YI = float(os.getenv("AI_STRATEGY_TOTAL_MV_PER_YI", "100000000.0"))
TOTAL_MV_TO_YI = 1.0 / 100000000.0  # 对外统一返回亿元口径，与投研接口一致

COMPATIBLE_COLUMN_CANDIDATES = {
    "symbol": ["symbol", "code"],
    "name": ["name", "stock_name"],
    "amount": ["amount", "turnover"],
    "idx_hs300": ["idx_hs300", "is_hs300", "is_csi300"],
    "idx_zz1000": ["idx_zz1000", "is_csi1000"],
}

_QUANTDB_NAMES_CACHE: dict[str, dict[str, str]] = {}
_QUANTDB_NAMES_TTL_SECONDS = 600


def _market_from_table(table_name: str | None) -> str:
    """从市场专属表名反推市场（stock_daily_latest_hk -> HK，缺省 CN）。"""
    name = str(table_name or "").lower()
    for m, suffix in (("FUTURES", "_futures"), ("CRYPTO", "_crypto"), ("US", "_us"), ("HK", "_hk")):
        if suffix in name:
            return m
    return "CN"


def _load_quantdb_stock_names(market: str | None = None) -> dict[str, str]:
    """按市场加载标的简称映射（suffix symbol -> name）。

    通过 market_hub 解析当前市场的 DataHub（A股/港股/美股/区块链/期货），
    避免硬编码 QuantDB（A 股）。结果带缓存，避免每次查询都读 parquet。
    """
    market_upper = str(market or "CN").upper()
    now = time.monotonic()
    cached = _QUANTDB_NAMES_CACHE.get(market_upper)
    if cached and cached[0] > now - _QUANTDB_NAMES_TTL_SECONDS:
        return cached[1]
    try:
        from backend.services.engine.data_platform.market_hub import fetch_stock_list_for_market

        mapping = fetch_stock_list_for_market(market_upper)
        _QUANTDB_NAMES_CACHE[market_upper] = (now, mapping)
        return mapping
    except Exception as exc:  # noqa: BLE001
        logger.debug("市场 %s 标的名称加载失败: %s", market_upper, exc)
        return {}


def _resolve_stock_name(
    row_dict: dict[str, Any] | None,
    raw_symbol: str,
    fallback: Any = None,
    market: str | None = None,
) -> Any:
    """解析股票简称：优先 SQL 查询出的 name，其次按市场 hub 简称，最后回退代码/None。"""
    name = row_dict.get("name") if row_dict else fallback
    if name:
        return name
    try:
        suffix = StockCodeUtil.to_suffix(raw_symbol)
    except Exception:  # noqa: BLE001
        suffix = raw_symbol
    quantdb_names = _load_quantdb_stock_names(market=market)
    return quantdb_names.get(suffix) or quantdb_names.get(raw_symbol) or name or raw_symbol


def _get_table_columns(session, table_name: str) -> set[str]:
    try:
        rows = session.execute(
            text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = :table_name
                """),
            {"table_name": table_name},
        ).fetchall()
        return {str(r[0]).lower() for r in rows}
    except Exception:
        return set()


def _strip_invalid_columns(sql: str, valid_columns: set[str]) -> str:
    """Remove WHERE-clause conditions that reference columns not in the actual PG table.

    Strips ``col op value`` and ``col ILIKE ...`` fragments for columns absent from
    *valid_columns*.  Handles AND/OR connectors and nested parentheses conservatively:
    if a condition references an invalid column, the entire ``AND <condition>`` or
    ``OR <condition>`` fragment is removed so that the remaining SQL stays syntactically
    valid.
    """
    if not valid_columns:
        return sql

    # Identify all column-like identifiers in the SQL
    identifiers = set(re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', sql))
    # SQL keywords to ignore
    sql_keywords = {
        "select", "from", "where", "and", "or", "not", "in", "is", "null",
        "like", "between", "exists", "order", "by", "limit", "offset", "asc",
        "desc", "join", "left", "right", "inner", "outer", "on", "group",
        "having", "as", "case", "when", "then", "else", "end", "distinct",
        "cast", "coalesce", "max", "min", "sum", "avg", "count", "ilike",
        "true", "false", "nulls", "last", "first",
    }

    invalid_cols = identifiers - valid_columns - sql_keywords
    if not invalid_cols:
        return sql

    # For each invalid column, remove the condition referencing it
    for col in invalid_cols:
        # Pattern: AND/OR <col> <op> <value>  (possibly with parentheses)
        # Handle: AND ind_code_l1 ILIKE '%xxx%'
        # Handle: OR ind_code_l2 ILIKE '%xxx%'
        # Handle: AND ind_code_l1 = 'xxx'
        pattern = re.compile(
            r'\s+(?:AND|OR)\s+'
            r'(?:\(\s*)?'
            + re.escape(col)
            + r'\s+(?:ILIKE|LIKE|>=|<=|!=|<>|=|>|<)\s*(?:[^\s\)]+|\$\d+\s*\))',
            re.IGNORECASE,
        )
        sql = pattern.sub('', sql)

    # Clean up any trailing empty parentheses or double spaces
    sql = re.sub(r'\s+', ' ', sql).strip()
    return sql


def _resolve_compatible_column(columns: set[str], logical_name: str) -> str:
    for candidate in COMPATIBLE_COLUMN_CANDIDATES.get(logical_name, [logical_name]):
        if candidate in columns:
            return candidate
    return logical_name


def _build_compat_table_sql(table_name: str, columns: set[str]) -> str:
    if not columns:
        return table_name

    select_fields = [col for col in sorted(columns)]
    alias_targets = {
        "symbol": _resolve_compatible_column(columns, "symbol"),
        "code": _resolve_compatible_column(columns, "symbol"),
        "name": _resolve_compatible_column(columns, "name"),
        "stock_name": _resolve_compatible_column(columns, "name"),
        "amount": _resolve_compatible_column(columns, "amount"),
        "turnover": _resolve_compatible_column(columns, "amount"),
        "idx_hs300": _resolve_compatible_column(columns, "idx_hs300"),
        "is_hs300": _resolve_compatible_column(columns, "idx_hs300"),
        "is_csi300": _resolve_compatible_column(columns, "idx_hs300"),
        "idx_zz1000": _resolve_compatible_column(columns, "idx_zz1000"),
        "is_csi1000": _resolve_compatible_column(columns, "idx_zz1000"),
    }
    for alias, target in alias_targets.items():
        if alias not in columns and target in columns:
            select_fields.append(f"{target} AS {alias}")

    return f"(SELECT {', '.join(select_fields)} FROM {table_name})"


def _replace_table_with_compat_subquery(sql: str, table_name: str, compat_table_sql: str) -> str:
    if compat_table_sql == table_name:
        return sql

    pattern = re.compile(
        rf"\b(FROM|JOIN)\s+{re.escape(table_name)}\b"
        rf"(?:\s+(?:AS\s+)?(?P<alias>[a-zA-Z_][a-zA-Z0-9_]*)(?=\s+(?:WHERE|JOIN|ORDER|GROUP|LIMIT|ON|$)))?",
        re.IGNORECASE,
    )

    def _repl(match: re.Match[str]) -> str:
        alias = match.group("alias") or table_name
        keyword = match.group(1)
        return f"{keyword} {compat_table_sql} {alias}"

    return pattern.sub(_repl, sql)


def _inject_trade_date_filter(sql: str, as_of_date: date | None) -> str:
    normalized = sql.strip().rstrip(";")
    if not as_of_date:
        return normalized

    split_match = re.search(r"\b(order\s+by|limit)\b", normalized, re.IGNORECASE)
    if split_match:
        body = normalized[: split_match.start()].rstrip()
        tail = " " + normalized[split_match.start() :].lstrip()
    else:
        body = normalized
        tail = ""

    if re.search(r"\btrade_date\b", body, re.IGNORECASE):
        return normalized

    trade_clause = f"trade_date = '{as_of_date}'"
    if re.search(r"\bwhere\b", body, re.IGNORECASE):
        body = re.sub(r"\bWHERE\b", f"WHERE {trade_clause} AND ", body, count=1, flags=re.IGNORECASE)
    else:
        body = f"{body} WHERE {trade_clause}"
    return f"{body}{tail}"


def _query_pool_limit() -> int:
    """查询结果上限（防止超大结果拖垮接口），默认 10000。"""
    raw = (os.getenv("AI_STRATEGY_QUERY_POOL_LIMIT", "10000") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 10000
    return max(1, min(value, 50000))


def _require_user_id(request: Request) -> str:
    # 企业环境严格要求鉴权上下文，禁止回退默认用户。
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未认证：缺少用户上下文",
        )

    if isinstance(user, dict):
        user_id = user.get("user_id")
        tenant_id = user.get("tenant_id")
    else:
        user_id = getattr(user, "user_id", None)
        tenant_id = getattr(user, "tenant_id", None)

    if not user_id or not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="未授权：缺少 user_id 或 tenant_id",
        )

    return str(user_id)


def _get_universe_total(user_id: str, table_name: str | None = None) -> int:
    """
    获取覆盖率分母（候选全集大小）。

    规则（尽量"不会超过数据表中股票列表"）：
    1. 若存在 user_universe 且该用户有授权标的，则以其为全集（多租户隔离更严格）。
    2. 否则退化为指定表全量记录数。
    """
    tbl = table_name or LATEST_TABLE
    try:
        with get_db() as session:
            # 优先使用多租户白名单表（如果存在且有数据）
            try:
                n = session.execute(
                    text("select count(1) from user_universe where user_id = :uid"),
                    {"uid": user_id},
                ).scalar()
                n_int = int(n or 0)
                if n_int > 0:
                    return n_int
            except Exception:
                # 表不存在/无权限等，忽略并走 fallback
                try:
                    session.rollback()
                except Exception:
                    pass
                pass

            n2 = session.execute(text(f"select count(1) from {tbl}")).scalar()
            return int(n2 or 0)
    except Exception:
        return 0


def _execute_raw_selection_sql(sql: str, table_name: str | None = None) -> tuple[list[PoolItem], date | None]:
    """直接执行由 LLM 生成的 SQL 语句

    安全改进:
    1. 使用 SQL 验证器防止注入
    2. 使用安全的表名替换
    3. 强制添加 LIMIT 限制
    """
    market_table = table_name or LATEST_TABLE
    try:
        # === 安全验证：防止SQL注入 ===
        try:
            validated_sql = validate_and_sanitize(sql)
        except SQLValidationError as e:
            logger.error(f"SQL validation failed: {e}")
            raise HTTPException(status_code=400, detail=f"SQL验证失败: {str(e)}")

        sql_lower = validated_sql.lower()
        normalized_sql = validated_sql

        # 使用安全的表名替换函数
        # Replace known table names with the market-specific table
        for old_name in ("stock_selection", "stock_daily_latest", "stock_daily"):
            if f"from {old_name}" in sql_lower:
                try:
                    normalized_sql = safe_table_replace(normalized_sql, old_name, market_table)
                    sql_lower = normalized_sql.lower()
                except SQLValidationError:
                    pass

        target_table = market_table if f"from {market_table}" in normalized_sql.lower() else "stock_selection"

        # 强制清除 LLM 可能生成的 LIMIT 限制，确保返回足够多的股票
        normalized_sql = re.sub(r"limit\s+\d+", "", normalized_sql, flags=re.IGNORECASE).strip()
        max_rows = _query_pool_limit()
        if not normalized_sql.lower().endswith(f"limit {max_rows}"):
            normalized_sql += f" LIMIT {max_rows}"

        with get_db() as session:
            target_columns = _get_table_columns(session, target_table)
            as_of_date = session.execute(text(f"select max(trade_date) from {target_table}")).scalar()

            # --- Strip invalid column references from WHERE clause ---
            normalized_sql = _strip_invalid_columns(normalized_sql, target_columns)

            compat_table_sql = _build_compat_table_sql(target_table, target_columns)
            normalized_sql = _inject_trade_date_filter(normalized_sql, as_of_date)
            normalized_sql = _replace_table_with_compat_subquery(normalized_sql, target_table, compat_table_sql)

            result = session.execute(text(normalized_sql)).fetchall()

            items: list[PoolItem] = []
            for row in result:
                row_dict = row._asdict() if hasattr(row, "_asdict") else None

                if row_dict:
                    symbol = str(row_dict.get("symbol") or row_dict.get("code") or "")
                    name = _resolve_stock_name(row_dict, symbol, market=_market_from_table(market_table))

                    market_cap = row_dict.get("market_cap")
                    if market_cap is None:
                        market_cap = row_dict.get("total_mv")

                    pe = row_dict.get("pe_ratio")
                    if pe is None:
                        pe = row_dict.get("pe_ttm")

                    pb = row_dict.get("pb_ratio")
                    if pb is None:
                        pb = row_dict.get("pb")

                    metrics = {
                        "market_cap": float(market_cap or 0) * TOTAL_MV_TO_YI,
                        "pe": float(pe or 0),
                        "close": float(row_dict.get("close") or 0),
                        "pb": float(pb or 0),
                        "roe": float(row_dict.get("roe") or 0),
                    }
                else:
                    symbol = str(row[0])
                    name = row[1] if len(row) > 1 else None
                    metrics = {"close": float(row[2]) if len(row) > 2 else 0.0}

                items.append(PoolItem(symbol=symbol, name=name, metrics=metrics))

            # LLM 生成的 SQL 可能只选了 symbol/name，缺失市值/PE/收盘价等指标。
            # 若大部分行 metrics 全为 0，则用标准字段 SQL 对这批股票批量补全。
            _enrich_raw_selection_metrics(items, market_table)
            return items, as_of_date
    except Exception as e:
        logger.error(f"Error in _execute_raw_selection_sql: {e}")
        raise


def _enrich_raw_selection_metrics(items: list[PoolItem], market_table: str) -> None:
    """为 metrics 全为 0 的选股结果批量补全市值/PE/收盘价等核心字段。"""
    if not items:
        return
    missing = [it for it in items if not it.metrics.get("market_cap") and not it.metrics.get("pe") and not it.metrics.get("close")]
    if not missing or len(missing) < max(1, len(items) * 0.3):
        return
    symbols = list({str(it.symbol) for it in missing if it.symbol})
    if not symbols:
        return
    try:
        sym_list = ", ".join(f"('{s}')" for s in symbols)
        sql = f"""
            WITH sym_list(raw_symbol) AS (VALUES {sym_list}),
            joined AS (
                SELECT
                    sdl.trade_date,
                    sdl.symbol AS raw_symbol,
                    sdl.stock_name,
                    sdl.close,
                    sdl.total_mv,
                    sdl.pe_ttm,
                    sdl.pb,
                    sdl.roe
                FROM {market_table} sdl
                JOIN sym_list ON sdl.symbol = sym_list.raw_symbol
            ),
            latest_price AS (
                SELECT DISTINCT ON (raw_symbol) raw_symbol, close
                FROM joined
                WHERE close IS NOT NULL AND close > 0
                ORDER BY raw_symbol, trade_date DESC
            ),
            latest_name AS (
                SELECT DISTINCT ON (raw_symbol) raw_symbol, stock_name
                FROM joined
                WHERE stock_name IS NOT NULL AND stock_name <> ''
                ORDER BY raw_symbol, trade_date DESC
            ),
            latest_mv AS (
                SELECT DISTINCT ON (raw_symbol) raw_symbol, total_mv, pe_ttm, pb, roe
                FROM joined
                WHERE total_mv IS NOT NULL AND total_mv > 0
                ORDER BY raw_symbol, trade_date DESC
            )
            SELECT
                l.raw_symbol,
                COALESCE(n.stock_name, '') AS stock_name,
                COALESCE(p.close, 0) AS close,
                COALESCE(mv.total_mv, 0) AS total_mv,
                COALESCE(mv.pe_ttm, 0) AS pe_ttm,
                COALESCE(mv.pb, 0) AS pb,
                COALESCE(mv.roe, 0) AS roe
            FROM (SELECT DISTINCT raw_symbol FROM joined) l
            LEFT JOIN latest_name n ON n.raw_symbol = l.raw_symbol
            LEFT JOIN latest_mv mv ON mv.raw_symbol = l.raw_symbol
            LEFT JOIN latest_price p ON p.raw_symbol = l.raw_symbol
        """
        with get_db() as session:
            rows = session.execute(text(sql)).mappings().fetchall()
        enriched: dict[str, dict[str, Any]] = {}
        for r in rows:
            mv = float(r.get("total_mv") or 0)
            enriched[str(r.get("raw_symbol"))] = {
                "name": r.get("stock_name"),
                "market_cap": mv / 1e8 if mv else 0,
                "pe": float(r.get("pe_ttm") or 0),
                "pb": float(r.get("pb") or 0),
                "roe": float(r.get("roe") or 0),
                "close": float(r.get("close") or 0),
            }
        for it in missing:
            data = enriched.get(str(it.symbol))
            if not data:
                continue
            it.name = data["name"] or it.name
            it.metrics = {
                "market_cap": data["market_cap"],
                "pe": data["pe"],
                "pb": data["pb"],
                "roe": data["roe"],
                "close": data["close"],
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("Metrics enrichment for raw selection failed: %s", exc)


def _query_stock_pool(
    conditions: list[dict[str, Any]], combiners: list[str], user_id: str, table_name: str | None = None
) -> tuple[list[PoolItem], date | None]:
    market_table = table_name or LATEST_TABLE
    try:
        with get_db() as session:
            target_columns = _get_table_columns(session, market_table)
            compat_table_sql = _build_compat_table_sql(market_table, target_columns)
            # 1. 确定一个能覆盖绝大多数股票的有效"最新日期"
            # 优先选择有财务数据（total_mv 非空）的最新日期，确保市值/PE/PB 能正常显示
            date_res = session.execute(
                text(
                    f"SELECT trade_date, COUNT(*) as cnt, COUNT(total_mv) as mv_cnt "
                    f"FROM {market_table} "
                    f"GROUP BY trade_date "
                    f"HAVING COUNT(total_mv) > 0 "
                    f"ORDER BY trade_date DESC LIMIT 1"
                )
            ).fetchone()
            if not date_res:
                # 回退：没有任何日期有财务数据，用最多人数的日期
                date_res = session.execute(
                    text(
                        f"SELECT trade_date, COUNT(*) as cnt FROM {market_table} GROUP BY trade_date ORDER BY cnt DESC LIMIT 1"
                    )
                ).fetchone()

            if not date_res:
                return [], None

            as_of_date = date_res[0]
            logger.info(f"Targeting trade_date: {as_of_date} (covers {date_res[1]} stocks)")

            # 2. 组装参数和条件
            params = {"d": as_of_date}
            # 基础条件：日期匹配
            where_clauses = ["trade_date = :d"]

            # 3. 翻译 DSL 条件（跳过 QuantDB 因子，由 QuantDBQueryExecutor 单独处理）
            flag_cols = {"is_st", "idx_hs300", "idx_zz1000"}
            for idx, cond in enumerate(conditions):
                # Skip QuantDB factors — they are not in PG tables
                if is_quantdb_factor(cond.get("factor", "")):
                    continue
                col = _map_factor(cond["factor"])
                param_key = f"p{idx}"
                op = "=" if cond["op"] == "==" else cond["op"]

                # 处理数值和类型
                val = cond["value"]
                if col in flag_cols:
                    try:
                        val = int(float(val))
                    except:
                        val = 1 if str(val).lower() in ("true", "1", "yes") else 0

                params[param_key] = val
                where_clauses.append(f"{col} {op} :{param_key}")

            # 4. 组合最终 WHERE 语句 (稳健拼接)
            # 我们强制要求日期匹配 AND (其他条件)
            final_where = f"({where_clauses[0]})"
            if len(where_clauses) > 1:
                # 组合用户的业务条件
                user_conds = f"({where_clauses[1]})"
                for i, combiner in enumerate(combiners):
                    if i + 2 < len(where_clauses):
                        user_conds += f" {combiner} ({where_clauses[i + 2]})"
                final_where += f" AND ({user_conds})"

            # 5. 执行最终查询 (全量返回，最高支持 10000 只股票)
            sql = f"""
            SELECT
                symbol,
                name,
                total_mv as market_cap,
                pe_ttm as pe_ratio,
                pb as pb_ratio,
                close,
                amount,
                volume
            FROM {compat_table_sql} {market_table}
            WHERE {final_where}
            ORDER BY total_mv DESC NULLS LAST
            LIMIT 10000
            """

            logger.info(f"Generated Selection SQL: {sql}")
            logger.info(f"SQL Params: {params}")

            result = session.execute(text(sql), params).fetchall()
            logger.info(f"Query returned {len(result)} rows")

            items: list[PoolItem] = []
            for row in result:
                # 使用 row_dict 确保字段取值稳健，不受列顺序影响
                row_dict = row._asdict() if hasattr(row, "_asdict") else None

                if row_dict:
                    symbol = str(row_dict.get("symbol") or "")
                    name = _resolve_stock_name(row_dict, symbol, market=_market_from_table(market_table))

                    # 兼容不同可能的字段名
                    market_cap = row_dict.get("market_cap")
                    if market_cap is None:
                        market_cap = row_dict.get("total_mv")

                    pe = row_dict.get("pe_ratio")
                    if pe is None:
                        pe = row_dict.get("pe_ttm")

                    pb = row_dict.get("pb_ratio")
                    if pb is None:
                        pb = row_dict.get("pb")

                    metrics = {
                        "market_cap": float(market_cap or 0) * TOTAL_MV_TO_YI,
                        "pe": float(pe or 0),
                        "pb": float(pb or 0),
                        "close": float(row_dict.get("close") or 0),
                        "amount": float(row_dict.get("amount") or 0),
                        "volume": float(row_dict.get("volume") or 0),
                    }
                else:
                    # 极端回退方案
                    symbol = str(row[0])
                    name = row[1] if len(row) > 1 else None
                    metrics = {
                        "market_cap": float(row[2] or 0) * TOTAL_MV_TO_YI if len(row) > 2 else 0,
                        "pe": float(row[3] or 0) if len(row) > 3 else 0,
                        "pb": float(row[4] or 0) if len(row) > 4 else 0,
                        "close": float(row[5] or 0) if len(row) > 5 else 0,
                    }

                items.append(PoolItem(symbol=symbol, name=name, metrics=metrics))
            return items, as_of_date
    except Exception as e:
        logger.error(f"Critical error in _query_stock_pool: {e}", exc_info=True)
        raise


def _build_pool_summary(
    items: list[PoolItem],
    as_of_date: date | None,
    universe_total: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    total = len(items)
    caps = [x.metrics.get("market_cap", 0) for x in items]
    # market_cap 已经是"亿元"口径
    bucket_lt_100 = sum(1 for v in caps if v < 100)
    bucket_100_200 = sum(1 for v in caps if 100 <= v < 200)
    bucket_gte_200 = sum(1 for v in caps if v >= 200)
    denom = int(universe_total) if universe_total and universe_total > 0 else 0
    # 防止出现 >100% 的覆盖率（例如分母写死/分母小于实际候选数）
    match_rate = 0.0
    if total and denom:
        match_rate = min(100.0, round(100.0 * total / denom, 2))
    summary = {
        "matchRate": match_rate,
        "totalCandidates": total,
        "universeTotal": denom or None,
        "asOf": as_of_date.isoformat() if as_of_date else None,
    }
    charts = {
        "marketCap": [
            {"bucket": "<100亿", "value": bucket_lt_100},
            {"bucket": "100-200亿", "value": bucket_100_200},
            {"bucket": ">=200亿", "value": bucket_gte_200},
        ]
    }
    return summary, charts


def _is_full_market_query(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text or "")
    if not normalized:
        return False
    normalized = re.sub(r"[，。,\.!！?？:：;；、]", "", normalized)
    full_market_terms = [
        "全市场",
        "全市场股票",
        "全部市场",
        "全部市场股票",
        "全部股票",
        "全A股",
        "全A股股票",
        "全A股市场",
        "全A股市场股票",
        "全A股市场全部股票",
    ]
    return any(term in normalized for term in full_market_terms)


def _build_full_market_sql(market: str | None = None) -> str:
    from .step1_stock_selection import get_latest_table
    tbl = get_latest_table(market)
    return (
        "SELECT symbol, name, close, total_mv as market_cap, pe_ttm as pe_ratio\n"
        f"FROM {tbl}"
    )


def _is_full_market_sql(sql: str) -> bool:
    if not sql:
        return False
    s = sql.strip().rstrip(";")
    pattern = re.compile(
        r"from\s+stock_selection\s+where\s+trade_date\s*=\s*"
        r"\(\s*select\s+max\(trade_date\)\s+from\s+stock_selection\s*\)\s*$",
        re.IGNORECASE,
    )
    return pattern.search(s) is not None


async def _ensure_latest_table_data(session, table_name: str | None = None) -> bool:
    """确保最新数据表中有可用数据，否则尝试检查原始表。"""
    tbl = table_name or LATEST_TABLE
    try:
        # 1. 检查 latest 表
        res = session.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar()
        if int(res or 0) > 0:
            return True

        # 2. 如果 latest 为空，检查原始 stock_daily 表
        logger.warning(f"表 {tbl} 为空，尝试检查原始 stock_daily 表...")
        res_raw = session.execute(
            text("SELECT trade_date FROM stock_daily ORDER BY trade_date DESC LIMIT 1")
        ).fetchone()

        if res_raw:
            latest_date = res_raw[0]
            error_msg = (
                f"选股数据未就绪：{tbl} 表为空。 "
                f"检测到原始数据表中最新日期为 {latest_date}，请运行同步脚本: "
                "python scripts/sync_latest_stocks.py"
            )
            logger.error(error_msg)
            raise HTTPException(status_code=503, detail=error_msg)
        else:
            raise HTTPException(status_code=503, detail="数据库中未发现任何行情数据，请先执行数据导入。")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        logger.error(f"检查数据可用性失败: {e}")
        return False


def _to_suffix_symbol(sym: str) -> str:
    """Convert PG internal format (SH600036) to QuantDB suffix format (600036.SH)."""
    return StockCodeUtil.to_suffix(sym)


def _suffix_matches_exchange(symbol: str, exchange: str) -> bool:
    """判断后缀格式 symbol 是否属于指定交易所（如 .HK 后缀 / 代码前缀）。"""
    s = str(symbol or "").upper()
    exch = str(exchange or "").upper()
    if not exch:
        return True
    # 后缀格式：700.HK / 600036.SH
    if "." in s:
        suffix = s.rsplit(".", 1)[-1]
        if exch in ("SH", "SSE") and suffix in ("SH", "CN"):
            return True
        if exch == "SZ" and suffix in ("SZ", "CN"):
            return True
        if exch == "HK" and suffix == "HK":
            return True
        if exch == "US" and suffix == "US":
            return True
        return False
    # 前缀格式：SH600036 / HK00700
    for prefix in ("SH", "SZ", "BJ", "HK"):
        if s.startswith(prefix):
            return prefix == exch
    return True


def _enrich_hub_pool_metrics(market: str, items: list[PoolItem]) -> None:
    """为 parquet 标的池补充名称/市值/PE 等维度（best-effort，各市场数据源不同）。

    港股: akshare_profile(公司名称) + akshare_valuation(PE/PB/简称)
    美股: f10(股票代码/name/market_cap/pe_ratio)
    期货/区块链: 暂无数补充，仅用 symbol。
    """
    market_upper = str(market or "").upper()
    if not items:
        return

    try:
        from backend.services.engine.data_platform.market_hub import get_hub_for_market

        hub = get_hub_for_market(market_upper)
        if hub is None:
            return
        base = hub.data_dir
    except Exception as exc:  # noqa: BLE001
        logger.debug("market %s 补充数据 hub 获取失败: %s", market_upper, exc)
        return

    symbol_to_item = {str(it.symbol).upper(): it for it in items}

    try:
        if market_upper == "HK":
            # 名称: akshare_profile/公司名称
            profile_dir = base / "2_base_sector" / "akshare_profile"
            if profile_dir.is_dir():
                import glob as _g

                name_map: dict[str, str] = {}
                for pf in _g.glob(str(profile_dir / "*.parquet"))[:2000]:
                    try:
                        pdf = pd.read_parquet(pf, engine="pyarrow")
                        for _, r in pdf.iterrows():
                            sym = str(r.get("symbol", "")).upper().strip()
                            nm = str(r.get("公司名称", "")).strip()
                            if sym and nm:
                                name_map[sym] = nm
                    except Exception:
                        continue
                for sym, it in symbol_to_item.items():
                    if sym in name_map:
                        it.name = name_map[sym]

            # 估值: akshare_valuation/市盈率-TTM + 市净率-MRQ
            val_dir = base / "2_base_sector" / "akshare_valuation"
            if val_dir.is_dir():
                import glob as _g

                val_map: dict[str, dict[str, float]] = {}
                for vf in _g.glob(str(val_dir / "*.parquet"))[:2000]:
                    try:
                        vdf = pd.read_parquet(vf, engine="pyarrow")
                        for _, r in vdf.iterrows():
                            sym = str(r.get("symbol", "")).upper().strip()
                            if not sym:
                                continue
                            val_map[sym] = {}
                            for col, key in (("市盈率-TTM", "pe"), ("市净率-MRQ", "pb")):
                                try:
                                    v = float(r.get(col))
                                    if v == v and v > 0:
                                        val_map[sym][key] = round(v, 2)
                                except Exception:
                                    pass
                    except Exception:
                        continue
                for sym, it in symbol_to_item.items():
                    if sym in val_map:
                        it.metrics.update(val_map[sym])

        elif market_upper == "US":
            # 美股: f10/*.parquet 含 name/market_cap/pe_ratio
            f10_dir = base / "2_base_sector" / "f10"
            if f10_dir.is_dir():
                import glob as _g

                for pf in _g.glob(str(f10_dir / "*.parquet"))[:2000]:
                    try:
                        fdf = pd.read_parquet(pf, engine="pyarrow")
                        for _, r in fdf.iterrows():
                            sym = str(r.get("symbol", "")).upper().strip()
                            if sym not in symbol_to_item:
                                continue
                            it = symbol_to_item[sym]
                            nm = str(r.get("name", "")).strip()
                            if nm:
                                it.name = nm
                            try:
                                mc = float(r.get("market_cap"))
                                if mc and mc == mc:
                                    it.metrics["market_cap"] = round(mc, 2)
                            except Exception:
                                pass
                            try:
                                pe = float(r.get("pe_ratio"))
                                if pe and pe == pe and pe > 0:
                                    it.metrics["pe"] = round(pe, 2)
                            except Exception:
                                pass
                    except Exception:
                        continue
    except Exception as exc:  # noqa: BLE001
        logger.warning("market %s 补充数据失败: %s", market_upper, exc)


def _load_market_pool_from_hub(market: str, dsl: str = "") -> tuple[list[PoolItem], date | None]:
    """非 A 股市场：从本地 parquet hub 读取最新交易日标的池，替代不存在的 PG latest 表。

    返回 (items, as_of_date)。支持"全部市场"类 DSL，也支持简单条件（pe_ttm>0 等
    会退化为取全部，因为 parquet 无这些 PG 字段过滤）。
    """
    from backend.services.engine.data_platform.market_hub import get_hub_for_market

    market_upper = str(market or "").upper()
    if market_upper in ("", "CN", "A"):
        return [], None

    hub = get_hub_for_market(market_upper)
    if hub is None:
        logger.warning("market %s hub 不可用，无法加载标的池", market_upper)
        return [], None

    # 读取最新一天的 daily_forward 截面（symbol + close + volume + amount）
    try:
        end = date.today()
        start = end - timedelta(days=10)
        # 各 hub 的 fetch_daily_kline 需要单个 symbol；这里直接读 daily_forward 最新分区
        fwd_dir = hub.data_dir / "1_kline_data" / "daily_forward"
        if not fwd_dir.is_dir():
            logger.warning("market %s daily_forward 目录不存在: %s", market_upper, fwd_dir)
            return [], None

        import glob as _glob

        parts = sorted(_glob.glob(str(fwd_dir / "dt=*" / "data.parquet")))
        if not parts:
            return [], None
        latest_part = parts[-1]
        latest_dt = latest_part.split("dt=")[-1].split("/")[0]
        df = pd.read_parquet(latest_part, engine="pyarrow")
        if df is None or df.empty:
            return [], None

        # 统一列名：symbol / instrument
        sym_col = "symbol" if "symbol" in df.columns else ("instrument" if "instrument" in df.columns else None)
        if sym_col is None or "close" not in df.columns:
            logger.warning("market %s 最新分区缺 symbol/close 列", market_upper)
            return [], None

        items: list[PoolItem] = []
        for _, row in df.iterrows():
            sym = str(row[sym_col]).strip()
            if not sym:
                continue
            close = float(row.get("close") or 0)
            if close <= 0:
                continue
            metrics: dict[str, Any] = {"close": close}
            if "volume" in df.columns:
                metrics["volume"] = float(row.get("volume") or 0)
            if "amount" in df.columns:
                metrics["amount"] = float(row.get("amount") or 0)
            items.append(PoolItem(symbol=sym, name=sym, metrics=metrics))

        as_of_date = None
        try:
            as_of_date = date(int(latest_dt[:4]), int(latest_dt[4:6]), int(latest_dt[6:8]))
        except Exception:
            pass
        # 补充名称/市值/PE 等维度（best-effort）
        _enrich_hub_pool_metrics(market_upper, items)
        logger.info(
            "market %s parquet 标的池: %d 个标的 (date=%s)",
            market_upper, len(items), latest_dt,
        )
        return items, as_of_date
    except Exception as exc:  # noqa: BLE001
        logger.warning("market %s parquet 标的池加载失败: %s", market_upper, exc)
        return [], None


def _enrich_with_quantdb_data(symbols: list[str]) -> dict[str, dict]:
    """从 QuantDB 补充关键维度数据（best-effort，不阻塞主查询）。

    Returns:
        dict mapping PG-format symbol -> {field: value, ...} for key metrics.
    """
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        if not hub.available:
            return {}
    except ImportError:
        return {}

    if not symbols:
        return {}

    result: dict[str, dict] = {}

    # Build lookup: suffix_symbol -> original PG symbol
    suffix_to_orig: dict[str, str] = {}
    for sym in symbols:
        suffix_sym = _to_suffix_symbol(sym)
        suffix_to_orig[suffix_sym] = sym

    target_suffixes = set(suffix_to_orig.keys())

    # --- Valuation: dividend_rate, ps_ttm, pe_static ---
    try:
        df_val = hub.fetch_valuation(start=None, end=None)
        if df_val is not None and not df_val.empty:
            sym_col = "symbol" if "symbol" in df_val.columns else "Symbol"
            # Take only the latest row per symbol
            if "trade_date" in df_val.columns:
                df_val = df_val.sort_values("trade_date").groupby(sym_col).last().reset_index()
            elif "dt" in df_val.columns:
                df_val = df_val.sort_values("dt").groupby(sym_col).last().reset_index()

            matched = df_val[df_val[sym_col].isin(target_suffixes)]
            for _, row in matched.iterrows():
                suffix_sym = str(row[sym_col])
                orig_sym = suffix_to_orig.get(suffix_sym)
                if not orig_sym:
                    continue
                if orig_sym not in result:
                    result[orig_sym] = {}
                for field in ("dividend_rate", "ps_ttm", "pe_static"):
                    if field in row and pd.notna(row[field]):
                        result[orig_sym][field] = float(row[field])
    except Exception as exc:
        logger.debug("QuantDB valuation enrichment failed: %s", exc)

    # --- Market Sentiment: liquidity_score, buy_pressure, momentum_1d ---
    try:
        df_sent = hub.fetch_market_sentiment(start=None, end=None)
        if df_sent is not None and not df_sent.empty:
            sym_col = "symbol" if "symbol" in df_sent.columns else "Symbol"
            if "trade_date" in df_sent.columns:
                df_sent = df_sent.sort_values("trade_date").groupby(sym_col).last().reset_index()
            elif "dt" in df_sent.columns:
                df_sent = df_sent.sort_values("dt").groupby(sym_col).last().reset_index()

            matched = df_sent[df_sent[sym_col].isin(target_suffixes)]
            for _, row in matched.iterrows():
                suffix_sym = str(row[sym_col])
                orig_sym = suffix_to_orig.get(suffix_sym)
                if not orig_sym:
                    continue
                if orig_sym not in result:
                    result[orig_sym] = {}
                for field in ("liquidity_score", "buy_pressure", "momentum_1d"):
                    if field in row and pd.notna(row[field]):
                        result[orig_sym][field] = float(row[field])
    except Exception as exc:
        logger.debug("QuantDB sentiment enrichment failed: %s", exc)

    # --- L1 Factors: chip_profit_ratio_20, ind_strength_20, style_beta_20 ---
    try:
        df_l1 = hub.fetch_l1_factors(start=None, end=None)
        if df_l1 is not None and not df_l1.empty:
            sym_col = "symbol" if "symbol" in df_l1.columns else "Symbol"
            if "trade_date" in df_l1.columns:
                df_l1 = df_l1.sort_values("trade_date").groupby(sym_col).last().reset_index()
            elif "dt" in df_l1.columns:
                df_l1 = df_l1.sort_values("dt").groupby(sym_col).last().reset_index()

            matched = df_l1[df_l1[sym_col].isin(target_suffixes)]
            for _, row in matched.iterrows():
                suffix_sym = str(row[sym_col])
                orig_sym = suffix_to_orig.get(suffix_sym)
                if not orig_sym:
                    continue
                if orig_sym not in result:
                    result[orig_sym] = {}
                for field in ("chip_profit_ratio_20", "ind_strength_20", "style_beta_20"):
                    if field in row and pd.notna(row[field]):
                        result[orig_sym][field] = float(row[field])
    except Exception as exc:
        logger.debug("QuantDB l1_factors enrichment failed: %s", exc)

    return result


def _filter_items_by_quantdb(
    items: list[PoolItem],
    qdb_conditions: list[dict[str, Any]],
    target_date: date,
) -> list[PoolItem]:
    """用 QuantDBQueryExecutor 对 QuantDB 条件过滤，与 PG 结果取交集。

    PG 结果 symbol 为后缀式（600036.SH），QuantDB 执行器返回后缀式
    （600036.SH），两侧统一成后缀正典再求交集。
    """
    if not qdb_conditions or not items:
        return items

    try:
        from ..services.selection.generator import QuantDBQueryExecutor

        executor = QuantDBQueryExecutor()
        qdb_symbols_suffix = executor.execute(qdb_conditions, target_date=target_date)
        if not qdb_symbols_suffix:
            logger.info("QuantDB filter returned empty set — no stock passes the QuantDB conditions")
            return []

        # 后缀正典统一
        qdb_suffix = {
            StockCodeUtil.normalize(s)
            for s in qdb_symbols_suffix
            if s
        }
        if not qdb_suffix:
            return []

        logger.info(
            "QuantDB filter: %d symbols passed conditions (suffix-sample: %s)",
            len(qdb_suffix),
            list(qdb_suffix)[:3],
        )
        return [it for it in items if StockCodeUtil.normalize(it.symbol) in qdb_suffix]
    except Exception as exc:
        # 过滤失败时降级为不过滤（保持旧行为），但记录告警以便排查
        logger.warning("QuantDB filter failed, returning PG results unfiltered: %s", exc)
        return items


async def query_pool(dsl: str, user_id: str, market: str | None = None, exchange: str | None = None) -> QueryPoolResponse:
    """执行 DSL/SQL 查询并返回股票池"""
    from .step1_stock_selection import get_latest_table

    market_table = get_latest_table(market)
    market_upper = str(market or "").upper()

    # 非 A 股市场：优先从本地 parquet hub 加载标的池（PG latest 表可能不存在）
    if market_upper not in ("", "CN", "A"):
        # 检查 PG 表是否存在；不存在则走 parquet
        try:
            with get_db() as session:
                tbl_exists = session.execute(
                    text(
                        "SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=:t"
                    ),
                    {"t": market_table},
                ).scalar()
        except Exception:
            tbl_exists = None

        if not tbl_exists:
            items, as_of_date = _load_market_pool_from_hub(market_upper, dsl)
            if items:
                # 交易所过滤（港股等）
                if exchange:
                    exch = str(exchange).upper()
                    items = [it for it in items if _suffix_matches_exchange(str(it.symbol), exch)]
                universe_total = len(items)
                summary, charts = _build_pool_summary(items, as_of_date, universe_total=universe_total)
                return QueryPoolResponse(items=items, summary=summary, charts=charts)
            logger.warning("market %s parquet 标的池为空，回退 PG 表 %s", market_upper, market_table)

    # 增加数据就绪性检查
    with get_db() as session:
        await _ensure_latest_table_data(session, market_table)

    if dsl.startswith("SQL: "):
        raw_sql = dsl.replace("SQL: ", "").strip()
        items, as_of_date = _execute_raw_selection_sql(raw_sql, market_table)
    else:
        if not dsl.startswith(DSL_PREFIX):
            raise ValueError("DSL格式不正确，必须以 'SELECT symbol WHERE' 开头")

        conditions, combiners = _parse_dsl(dsl)
        # 拆分 PG 原生条件 与 QuantDB 因子条件
        pg_conditions, pg_combiners, qdb_conditions, qdb_combiners = split_conditions_by_source(
            conditions, combiners
        )
        logger.info(
            "query_pool: %d PG conditions, %d QuantDB conditions",
            len(pg_conditions),
            len(qdb_conditions),
        )

        items, as_of_date = _query_stock_pool(pg_conditions, pg_combiners, user_id, market_table)

        # QuantDB 因子条件：用 DuckDB 视图过滤，与 PG 结果取交集
        if qdb_conditions and as_of_date:
            items = _filter_items_by_quantdb(items, qdb_conditions, as_of_date)
        elif qdb_conditions:
            logger.warning("query_pool: QuantDB conditions present but no as_of_date to filter by")

    # 交易所过滤：A 股 symbol 为前缀格式（SH600000 / SZ000001 / BJ920000）
    if exchange and market in (None, "", "A", "CN"):
        exch = str(exchange).upper()
        if exch in ("SH", "SZ", "BJ"):
            prefix_map = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}
            wanted = prefix_map[exch]
            items = [it for it in items if str(it.symbol or "").upper().startswith(wanted)]

    universe_total = _get_universe_total(user_id, market_table)

    summary, charts = _build_pool_summary(items, as_of_date, universe_total=universe_total)
    return QueryPoolResponse(items=items, summary=summary, charts=charts)
