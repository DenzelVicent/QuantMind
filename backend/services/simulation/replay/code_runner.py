"""模拟盘策略代码执行器（回放 code 模式）。

同一套 Strategy Lab SDK 代码（``setup / on_universe / on_bar``），回测跑
``strategy_lab.engine.loop``，模拟盘跑这里。两种模式共用：

- 沙箱：``ast_checker.assert_safe`` + worker 同款受限 globals（进程内执行，
  与回放其它链路一致；回放本就跑在 API/engine 进程内，无独立 worker）。
- 股票池：``ctx.stock_pool`` 一行代码，经
  ``shared.stock_pool.strategy.apply_pool_to_universe`` 解析，回测/模拟同源。
- 行情：``ReplayDataProvider`` 把回放本地 parquet（``LocalMarketData``）
  适配成 SDK provider 口径（history/snapshot/feature）。

会话级编译产物缓存在进程内存（``_SESSIONS``，与 ``_PRED_FRAME_CACHE`` 同假设：
同一 engine 进程处理同一会话的连续 step）。
"""

from __future__ import annotations

import logging
import math
import traceback
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_LOT_SIZE = 100

# ---------------------------------------------------------------------------
# 会话级编译缓存
# ---------------------------------------------------------------------------


@dataclass
class _CompiledSession:
    ctx: Any
    user_globals: dict[str, Any]
    symbols: list[str]  # 前缀式
    on_universe: Any = None
    on_bar: Any = None
    pool_id: str | None = None
    pool_checksum: str | None = None
    tenant_id: str = "default"
    user_id: str = "0"


_SESSIONS: dict[str, _CompiledSession] = {}


def _build_user_globals() -> dict[str, Any]:
    """worker 同款受限 globals（复用逻辑，避免跨模块私函数依赖）。"""
    from backend.services.engine.strategy_lab.runner.ast_checker import (
        ALLOWED_MODULES,
    )

    _real_import = __import__

    def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        top = (name or "").split(".")[0]
        if top not in ALLOWED_MODULES:
            raise ImportError(
                f"module '{name}' is not in the strategy sandbox whitelist"
            )
        return _real_import(name, globals, locals, fromlist, level)

    safe_builtins: dict[str, Any] = {
        "abs": abs, "all": all, "any": any, "bool": bool, "bytes": bytes,
        "callable": callable, "chr": chr, "complex": complex, "dict": dict,
        "divmod": divmod, "enumerate": enumerate, "filter": filter,
        "float": float, "frozenset": frozenset, "hex": hex, "int": int,
        "isinstance": isinstance, "issubclass": issubclass, "iter": iter,
        "len": len, "list": list, "map": map, "max": max, "min": min,
        "next": next, "object": object, "oct": oct, "ord": ord, "pow": pow,
        "print": print, "range": range, "repr": repr, "reversed": reversed,
        "round": round, "set": set, "slice": slice, "sorted": sorted,
        "str": str, "sum": sum, "tuple": tuple, "type": type, "zip": zip,
        "hasattr": hasattr, "getattr": getattr, "setattr": setattr,
        "__import__": _safe_import,
        "Exception": Exception, "ValueError": ValueError, "KeyError": KeyError,
        "TypeError": TypeError, "RuntimeError": RuntimeError,
        "ZeroDivisionError": ZeroDivisionError, "IndexError": IndexError,
        "ImportError": ImportError, "ArithmeticError": ArithmeticError,
        "True": True, "False": False, "None": None,
        "__name__": "__strategy__",
    }
    return {"__builtins__": safe_builtins}


def _resolve_universe_symbols(
    universe: Any, market_data: Any
) -> list[str]:
    """解析 setup 的 universe 为后缀正典符号表。

    list 直接用；str 先走 strategy_lab 的 qlib instruments（与回测同源），
    读不到再回退回放本地行情全量标的。
    """
    from backend.shared.stock_utils import StockCodeUtil

    if isinstance(universe, str):
        try:
            from backend.services.engine.strategy_lab.engine.data_provider import (
                load_universe,
            )

            syms = load_universe(universe)
        except Exception:
            syms = []
        if syms:
            return [StockCodeUtil.normalize(s) for s in syms]
        # 回退：本地回放行情全量（统一后缀正典）
        try:
            bars = market_data.load_date(None, None)  # type: ignore[arg-type]
        except Exception:
            bars = {}
        if not bars:
            try:
                sessions = market_data._sessions()
                if sessions:
                    from datetime import date as _date

                    y, m, d = (
                        sessions[-1] // 10000,
                        (sessions[-1] // 100) % 100,
                        sessions[-1] % 100,
                    )
                    bars = market_data.load_date(_date(y, m, d), None)
            except Exception:
                bars = {}
        return [StockCodeUtil.normalize(s) for s in bars.keys()]
    return [StockCodeUtil.normalize(str(s)) for s in (universe or [])]


def prepare_session(
    session_id: uuid.UUID | str,
    strategy_code: str,
    *,
    market_data: Any,
    start: date,
    end: date,
    cash: float,
    pool_ref: str | None = None,
    tenant_id: str = "default",
    user_id: str = "0",
) -> _CompiledSession:
    """编译策略代码并执行 setup，缓存会话执行上下文。

    - ``pool_ref``（会话级）作为 ``ctx.stock_pool`` 的缺省值：代码里写了以
      代码为准，没写才用会话的。
    - 失败抛 ``ValueError``（router 转 400，不建会话）。
    """
    from backend.services.engine.strategy_lab.runner.ast_checker import (
        ASTCheckError,
        assert_safe,
    )
    from backend.services.engine.strategy_lab.sdk.context import Context
    from backend.shared.stock_pool.strategy import apply_pool_to_universe

    sid = str(session_id)
    if not strategy_code or not strategy_code.strip():
        raise ValueError("code 模式必须提供 strategy_code")

    try:
        assert_safe(strategy_code)
    except ASTCheckError as exc:
        raise ValueError(f"策略代码未通过安全检查: {exc}") from exc

    ctx = Context()
    user_globals = _build_user_globals()
    try:
        compiled = compile(strategy_code, f"<replay:{sid}>", "exec")
        exec(compiled, user_globals, user_globals)
    except Exception as exc:
        raise ValueError(f"策略代码加载失败: {exc}") from exc

    setup_fn = user_globals.get("setup")
    if not callable(setup_fn):
        raise ValueError("策略代码必须定义 setup(ctx)")
    try:
        setup_fn(ctx)
    except Exception as exc:
        raise ValueError(f"setup(ctx) 执行失败: {exc}") from exc

    # 回放窗口/资金以会话为准（与 loop 跑全程不同，这里逐日驱动）。
    object.__setattr__(ctx, "start", start.isoformat())
    object.__setattr__(ctx, "end", end.isoformat())
    try:
        ctx.assert_ready()
    except Exception as exc:
        raise ValueError(f"setup 缺必要配置: {exc}") from exc

    symbols = _resolve_universe_symbols(ctx.universe, market_data)

    code_pool = getattr(ctx, "stock_pool", None)
    effective_pool = (
        code_pool.strip()
        if isinstance(code_pool, str) and code_pool.strip()
        else (pool_ref or "").strip()
    )
    pool_id = None
    pool_checksum = None
    if effective_pool:
        try:
            out = apply_pool_to_universe(
                symbols, effective_pool,
                tenant_id=tenant_id, user_id=user_id, strict=True,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        symbols = out.symbols
        pool_id = out.pool_id
        pool_checksum = out.pool_checksum
        logger.info(
            "回放 code 模式股票池: session=%s pool=%s kept=%d dropped=%d",
            sid, pool_id, len(symbols), out.dropped,
        )

    compiled_session = _CompiledSession(
        ctx=ctx,
        user_globals=user_globals,
        symbols=symbols,
        on_universe=user_globals.get("on_universe"),
        on_bar=user_globals.get("on_bar"),
        pool_id=pool_id,
        pool_checksum=pool_checksum,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    if compiled_session.on_universe is None and compiled_session.on_bar is None:
        raise ValueError("策略代码必须定义 on_bar 或 on_universe 至少其一")
    _SESSIONS[sid] = compiled_session
    try:
        ctx._attach(data_provider=None, broker=None, cash=float(cash))
    except Exception:
        pass
    return compiled_session


def drop_session(session_id: uuid.UUID | str) -> None:
    _SESSIONS.pop(str(session_id), None)


def get_session(session_id: uuid.UUID | str) -> _CompiledSession | None:
    return _SESSIONS.get(str(session_id))


# ---------------------------------------------------------------------------
# 行情适配器：LocalMarketData → SDK provider 口径
# ---------------------------------------------------------------------------


class ReplayDataProvider:
    """把回放本地 parquet 适配成 ctx.history/snapshot/feature 口径。

    bars 按后缀式键入（``load_date`` 原样）；用户代码侧统一用前缀式，
    这里双向归一。history 走逐日 ``load_date``（命中 market_data 自带日缓存）。
    """

    _OHLCV = ("open", "high", "low", "close", "volume")

    def __init__(self, market_data: Any) -> None:
        self._md = market_data

    def _day_symbols(self, day: date) -> dict[str, Any]:
        try:
            return self._md.load_date(day, None) or {}
        except Exception:
            return {}

    def _bar_field(self, bar: Any, fld: str) -> float:
        try:
            v = float(getattr(bar, fld))
        except (TypeError, ValueError):
            return math.nan
        return v

    def _series(
        self, symbol: str, n: int, fld: str, today: pd.Timestamp | None
    ) -> pd.Series:
        from backend.shared.stock_utils import StockCodeUtil

        suffix = StockCodeUtil.to_suffix(symbol)
        try:
            sessions = self._md._sessions()
        except Exception:
            return pd.Series(dtype=float)
        end_int = int(pd.Timestamp(today).strftime("%Y%m%d")) if today is not None else 10**9
        days = [s for s in sessions if s <= end_int][-max(n * 2, 30):]
        vals: dict[pd.Timestamp, float] = {}
        for d_int in days:
            day = date(d_int // 10000, (d_int // 100) % 100, d_int % 100)
            bar = self._day_symbols(day).get(suffix)
            if bar is None:
                continue
            v = self._bar_field(bar, fld)
            if math.isfinite(v) and v > 0:
                vals[pd.Timestamp(day)] = v
            if len(vals) >= n * 2:
                break
        if not vals:
            return pd.Series(dtype=float)
        return pd.Series(vals).sort_index().tail(n)

    def history(
        self,
        symbol: str | None = None,
        n: int = 20,
        field: str = "close",
        fields: Sequence[str] | None = None,
        symbols: Sequence[str] | None = None,
        today: pd.Timestamp | None = None,
    ) -> pd.Series | pd.DataFrame:
        if symbols:
            cols = {}
            for s in symbols:
                cols[s] = self._series(s, n, field, today)
            return pd.DataFrame(cols)
        if symbol is None:
            return pd.Series(dtype=float)
        if fields:
            out = {f: self._series(symbol, n, f, today) for f in fields}
            return pd.DataFrame(out)
        return self._series(symbol, n, field, today)

    def feature(
        self, symbol: str, name: str, n: int = 1, today: pd.Timestamp | None = None
    ) -> float | pd.Series | None:
        # 回放只有 OHLCV；因子名映射 $close→close，其余返回 None。
        df_field = name.lstrip("$")
        if df_field not in (*self._OHLCV, "adj_close"):
            return None
        if df_field == "adj_close":
            df_field = "close"
        s = self._series(symbol, n, df_field, today)
        if s.empty:
            return None
        return float(s.iloc[-1]) if n == 1 else s

    def list_features(self) -> list[str]:
        return ["open", "high", "low", "close", "volume"]

    def snapshot(
        self, date: Any | None = None, symbols: Sequence[str] | None = None
    ) -> pd.DataFrame:
        from backend.shared.stock_utils import StockCodeUtil

        if date is None:
            return pd.DataFrame()
        day = pd.Timestamp(date).date()
        bars = self._day_symbols(day)
        rows = {}
        for s in symbols or []:
            bar = bars.get(StockCodeUtil.to_suffix(s))
            if bar is None:
                continue
            rows[s] = {f: self._bar_field(bar, f) for f in self._OHLCV}
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows).T

    def benchmark_history(
        self, symbol: str, n: int, today: pd.Timestamp | None = None
    ) -> pd.Series:
        return self._series(symbol or "SH000001", n, "close", today)

    def is_tradable(self, symbol: str, today: pd.Timestamp | None = None) -> bool:
        from backend.shared.stock_utils import StockCodeUtil

        if today is None:
            return True
        bar = self._day_symbols(pd.Timestamp(today).date()).get(
            StockCodeUtil.to_suffix(symbol)
        )
        if bar is None:
            return False
        try:
            return not bool(bar.suspended) and float(bar.volume or 0) > 0
        except (TypeError, ValueError):
            return False

    def is_st(self, symbol: str, today: pd.Timestamp | None = None) -> bool:
        return False

    def industry(self, symbol: str) -> str:
        return ""

    def market_cap(self, symbol: str, today: pd.Timestamp | None = None) -> float | None:
        return None


# ---------------------------------------------------------------------------
# 每日执行：hooks → OrderIntent → Order
# ---------------------------------------------------------------------------


def _equity_now(account_data: dict[str, Any], bars: dict[str, Any]) -> float:
    from backend.shared.stock_utils import StockCodeUtil

    cash = float(account_data.get("cash") or 0.0)
    mv = 0.0
    for sym, pos in ((account_data.get("positions") or {}).items()):
        bar = bars.get(StockCodeUtil.to_suffix(sym)) or bars.get(sym)
        price = 0.0
        if bar is not None:
            try:
                price = float(bar.close or 0)
            except (TypeError, ValueError):
                price = 0.0
        if price <= 0:
            price = float(pos.get("price") or 0.0)
        mv += price * float(pos.get("volume") or 0.0)
    return cash + mv


def _qty_for_weight(weight: float, price: float, equity: float) -> int:
    if price <= 0 or weight <= 0:
        return 0
    raw = int((equity * weight) // price)
    return (raw // _LOT_SIZE) * _LOT_SIZE


def intents_to_orders(
    intents: list[Any],
    *,
    account_data: dict[str, Any],
    bars: dict[str, Any],
    lot_size: int = _LOT_SIZE,
) -> list[Any]:
    """OrderIntent → rebalance_calculator.Order（具体 qty + 开盘价）。

    价格取 bar.open（回放默认 ``price_mode=open``）fallback close；
    T+1/资金不足由撮合与账户层按既有语义拒绝并留痕。
    """
    from backend.services.simulation.services.rebalance_calculator import Order
    from backend.shared.stock_utils import StockCodeUtil

    lot = max(1, int(lot_size))
    equity = _equity_now(account_data, bars)
    positions = account_data.get("positions") or {}
    out: list[Any] = []

    def _price(suffix: str) -> float | None:
        bar = bars.get(suffix)
        if bar is None:
            return None
        for attr in ("open", "close"):
            try:
                v = float(getattr(bar, attr) or 0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
        return None

    def _holding(suffix: str) -> dict[str, Any]:
        # 回放持仓键为后缀式；兼容前缀键。
        pos = positions.get(suffix)
        if pos is None:
            for k, v in positions.items():
                try:
                    if StockCodeUtil.to_suffix(k) == suffix:
                        return v
                except Exception:
                    continue
            return {}
        return pos

    for o in intents or []:
        side = str(getattr(o, "side", "") or "").lower()
        reason = str(getattr(o, "reason", "") or "code")
        if side == "set_target_holdings":
            targets = []
            for t in getattr(o, "targets", None) or []:
                try:
                    targets.append(StockCodeUtil.to_suffix(str(t)))
                except Exception:
                    continue
            if not targets:
                continue
            weight = 1.0 / len(targets)
            for sym in list(positions.keys()):
                try:
                    sfx = StockCodeUtil.to_suffix(sym)
                except Exception:
                    continue
                if sfx in targets:
                    continue
                pos = _holding(sfx)
                try:
                    vol = int(float(pos.get("volume") or 0))
                except (TypeError, ValueError):
                    vol = 0
                px = _price(sfx)
                if vol > 0 and px:
                    out.append(Order(symbol=sfx, side="SELL", quantity=vol, price=px, reason=reason or "rebalance"))
            for sfx in targets:
                px = _price(sfx)
                if not px:
                    continue
                pos = _holding(sfx)
                try:
                    cur_vol = float(pos.get("volume") or 0)
                    cur_px = float(pos.get("price") or px)
                except (TypeError, ValueError):
                    cur_vol, cur_px = 0.0, px
                delta = equity * weight - cur_vol * cur_px
                if delta > px * lot:
                    qty = (int(delta // px) // lot) * lot
                    if qty > 0:
                        out.append(Order(symbol=sfx, side="BUY", quantity=qty, price=px, reason=reason or "rebalance"))
            continue
        try:
            suffix = StockCodeUtil.to_suffix(str(getattr(o, "symbol", "") or ""))
        except Exception:
            continue
        if not suffix:
            continue
        px = _price(suffix)
        if not px:
            continue
        if side == "buy":
            qty_raw = getattr(o, "qty", None)
            if qty_raw is not None:
                qty = (int(qty_raw) // lot) * lot
            else:
                qty = _qty_for_weight(float(getattr(o, "weight", 0) or 0), px, equity)
                qty = (qty // lot) * lot
            if qty > 0:
                out.append(Order(symbol=suffix, side="BUY", quantity=int(qty), price=px, reason=reason))
        elif side == "sell":
            pos = _holding(suffix)
            try:
                held = float(pos.get("volume") or 0)
            except (TypeError, ValueError):
                held = 0.0
            if held <= 0:
                continue
            if bool(getattr(o, "all", False)):
                qty = int(held)
            elif o.qty is not None:
                qty = min(int(o.qty or 0), int(held))
            else:
                w = float(getattr(o, "weight", 0) or 0)
                qty = (int(held * w) // lot) * lot
            if qty > 0:
                out.append(Order(symbol=suffix, side="SELL", quantity=int(qty), price=px, reason=reason))
        elif side == "set_position":
            w = float(getattr(o, "weight", 0) or 0)
            target = _qty_for_weight(w, px, equity)
            pos = _holding(suffix)
            try:
                cur = int(float(pos.get("volume") or 0))
            except (TypeError, ValueError):
                cur = 0
            delta = target - cur
            if delta >= lot:
                out.append(Order(symbol=suffix, side="BUY", quantity=(delta // lot) * lot, price=px, reason=reason))
            elif delta <= -lot:
                out.append(Order(symbol=suffix, side="SELL", quantity=(-delta // lot) * lot, price=px, reason=reason))
    return out


def _enforce_sdk_risk(
    ctx: Any, account_data: dict[str, Any], bars: dict[str, Any], today: pd.Timestamp
) -> tuple[list[Any], bool]:
    """执行 ctx 注册的风险规则，返回 (强制卖单, 是否熔断后跳过用户单)。

    口径对齐 SimpleBroker._enforce_risk：收盘价触发；账户熔断后只出不进。
    """
    from backend.services.simulation.services.rebalance_calculator import Order
    from backend.shared.stock_utils import StockCodeUtil

    rules = []
    try:
        rules = ctx._drain_risk_rules()
    except Exception:
        rules = []
    stop_loss: dict[str, float] = {}
    take_profit: dict[str, float] = {}
    max_hold: dict[str, int] = {}
    acct_sl = None
    for r in rules or []:
        kind = getattr(r, "kind", None)
        sym = getattr(r, "symbol", None)
        try:
            val = float(getattr(r, "value", 0))
        except (TypeError, ValueError):
            continue
        if kind == "account_stop_loss":
            acct_sl = val
        elif sym:
            try:
                sfx = StockCodeUtil.to_suffix(str(sym))
            except Exception:
                continue
            if kind == "stop_loss":
                stop_loss[sfx] = val
            elif kind == "take_profit":
                take_profit[sfx] = val
            elif kind == "max_holding_days":
                try:
                    max_hold[sfx] = int(getattr(r, "value", 0))
                except (TypeError, ValueError):
                    pass

    forced: list[Any] = []
    halted = False
    positions = account_data.get("positions") or {}

    def _close(sfx: str) -> float | None:
        bar = bars.get(sfx)
        if bar is None:
            return None
        try:
            v = float(bar.close or 0)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    if acct_sl is not None:
        try:
            init_cash = float(getattr(ctx, "cash", 0) or 0)
        except (TypeError, ValueError):
            init_cash = 0.0
        if init_cash > 0 and _equity_now(account_data, bars) / init_cash - 1 <= acct_sl:
            halted = True
            for sym, pos in positions.items():
                try:
                    sfx = StockCodeUtil.to_suffix(sym)
                except Exception:
                    continue
                px = _close(sfx)
                try:
                    vol = int(float(pos.get("volume") or 0))
                except (TypeError, ValueError):
                    vol = 0
                if vol > 0 and px:
                    forced.append(Order(symbol=sfx, side="SELL", quantity=vol, price=px, reason="account_stop_loss"))
            return forced, True

    for sym, pos in positions.items():
        try:
            sfx = StockCodeUtil.to_suffix(sym)
        except Exception:
            continue
        px = _close(sfx)
        try:
            vol = int(float(pos.get("volume") or 0))
            cost = float(pos.get("cost") or 0)
        except (TypeError, ValueError):
            continue
        if vol <= 0 or not px or cost <= 0:
            continue
        ret = px / cost - 1
        sl, tp, mhd = stop_loss.get(sfx), take_profit.get(sfx), max_hold.get(sfx)
        hit = (sl is not None and ret <= sl) or (tp is not None and ret >= tp)
        if not hit and mhd is not None:
            try:
                from datetime import date as _date

                fbd = (pos.get("first_buy_date") or "")[:10]
                if fbd:
                    y, m, d = int(fbd[:4]), int(fbd[5:7]), int(fbd[8:10])
                    if (today.date() - _date(y, m, d)).days >= mhd:
                        hit = True
            except (TypeError, ValueError):
                pass
        if hit:
            forced.append(Order(symbol=sfx, side="SELL", quantity=vol, price=px, reason="sdk_risk"))
    return forced, halted


def run_code_day(
    session_id: uuid.UUID | str,
    trade_date: date,
    account_data: dict[str, Any],
    bars: dict[str, Any],
    market_data: Any,
) -> tuple[list[Any], dict[str, Any]]:
    """执行 code 会话的单个交易日，返回 (orders, info)。

    - info 含 ``signal_count``（= orders 数，供 DayResult 口径对齐）、
      ``pool_id``、``hook_error``（hook 异常只记日志不断流，与 loop 一致）。
    - 用户单被账户熔断跳过时 info 置 ``halted=True``。
    """
    from backend.services.engine.strategy_lab.sdk.bar import Bar

    sid = str(session_id)
    compiled = _SESSIONS.get(sid)
    if compiled is None:
        raise RuntimeError(f"code 会话未编译（进程重启后需重建会话）: {sid}")
    ctx = compiled.ctx
    today = pd.Timestamp(trade_date)

    provider = ReplayDataProvider(market_data)
    try:
        ctx._attach(data_provider=provider, broker=None, cash=float(account_data.get("cash") or 0.0))
    except Exception:
        object.__setattr__(ctx, "_data_provider", provider)
    ctx._set_today(today)

    hook_error = None
    if compiled.on_universe is not None:
        try:
            snap = provider.snapshot(date=today, symbols=compiled.symbols)
        except Exception:
            snap = pd.DataFrame()
        try:
            compiled.on_universe(ctx, today, snap)
        except Exception as exc:
            hook_error = f"on_universe: {exc}"
            ctx.log(f"on_universe error @ {trade_date}: {exc}", level="warning")
    if compiled.on_bar is not None:
        from backend.shared.stock_utils import StockCodeUtil

        for sym in compiled.symbols:
            try:
                s = provider.history(symbol=sym, n=1, fields=["open", "high", "low", "close", "volume"], today=today)
            except Exception:
                continue
            if s is None or len(s) == 0:
                continue
            try:
                row = s.iloc[-1]
                bar = Bar(
                    symbol=sym,
                    date=today,
                    open=float(row.get("open", 0.0) or 0.0),
                    high=float(row.get("high", 0.0) or 0.0),
                    low=float(row.get("low", 0.0) or 0.0),
                    close=float(row.get("close", 0.0) or 0.0),
                    volume=float(row.get("volume", 0.0) or 0.0),
                    adj_close=float(row.get("adj_close", row.get("close", 0.0)) or 0.0),
                )
            except Exception:
                continue
            try:
                compiled.on_bar(ctx, bar)
            except Exception as exc:
                hook_error = f"on_bar {sym}: {exc}"
                ctx.log(f"on_bar error {sym} @ {trade_date}: {exc}", level="warning")

    intents = []
    try:
        intents = ctx._drain_orders()
    except Exception:
        intents = []
    forced, halted = _enforce_sdk_risk(ctx, account_data, bars, today)
    orders = list(forced)
    if not halted:
        try:
            orders.extend(
                intents_to_orders(intents, account_data=account_data, bars=bars)
            )
        except Exception as exc:
            logger.warning("回放 code 意图解析失败 session=%s date=%s: %s", sid, trade_date, exc)
    # 池外安全网：hook 里手写非池标的时直接丢弃（与 signals 模式严格语义对齐）
    if compiled.symbols:
        allowed = set(compiled.symbols)
        from backend.shared.stock_utils import StockCodeUtil

        kept = []
        for o in orders:
            try:
                if StockCodeUtil.normalize(o.symbol) in allowed:
                    kept.append(o)
                    continue
            except Exception:
                pass
            logger.warning("回放 code 丢弃池外订单 session=%s %s", sid, getattr(o, "symbol", "?"))
        orders = kept
    info: dict[str, Any] = {
        "signal_count": len(orders),
        "pool_id": compiled.pool_id,
        "pool_checksum": compiled.pool_checksum,
        "halted": halted,
        "hook_error": hook_error,
    }
    return orders, info


def session_symbols(session_id: uuid.UUID | str) -> list[str]:
    compiled = _SESSIONS.get(str(session_id))
    return list(compiled.symbols) if compiled else []
