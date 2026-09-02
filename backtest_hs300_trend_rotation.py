# coding: utf-8
"""Backtest the HS300 trend rotation strategy with public A-share data.

Data source:
- Point-in-time HS300 constituents: BaoStock snapshots sampled every 20
  trading days and forward-filled without look-ahead.
- A-share daily OHLC: AkShare Tencent endpoint stock_zh_a_hist_tx, qfq.
- HS300 benchmark: AkShare Tencent endpoint stock_zh_index_daily_tx.

Important limitation:
Public snapshots are sampled rather than queried for every trading day, so
index additions and removals can be reflected up to 20 trading days late.
The result remains an approximate research backtest rather than broker-grade
tick-level execution simulation.
"""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import akshare as ak
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
CACHE = ROOT / ".cache" / "akshare_tx"
CACHE.mkdir(parents=True, exist_ok=True)
MEMBERSHIP_CACHE = ROOT / ".cache" / "hs300_membership_daily_v2.csv"
MEMBERSHIP_SNAPSHOT_CACHE = ROOT / ".cache" / "hs300_membership_snapshots_v2.csv"
MEMBERSHIP_SNAPSHOT_DAYS = 20

START_DATE = "20200701"      # warm-up for MA250 before the 5-year test window
TRADE_START = "20210902"
END_DATE = "20260902"
INITIAL_CASH = 1_000_000.0

MAX_POSITIONS = 5
ENTRY_RANK = 5
EXIT_RANK = 10
REBALANCE_DAYS = 5
MA_FAST = 20
MA_MID = 60
MA_LONG = 250
LONG_SLOPE_LOOKBACK = 20
MOMENTUM_PERIOD = 60
CASH_BUFFER = 0.01
MARKET_FILTER_ENABLED = True
MARKET_OK_STREAK_REQUIRED = 2
MARKET_MA_PERIOD = 20
MARKET_DAILY_DROP_BLOCK = 0.03
FILTER_STAR_MARKET = True
FILTER_SUSPENDED_BUYS = True
FILTER_LIMIT_UP_BUYS = True
FILTER_LIMIT_DOWN_BUYS = True
LIMIT_UP_THRESHOLD = 0.09
LIMIT_DOWN_THRESHOLD = 0.09
AMOUNT_LOOKBACK = 20
MIN_AVG_AMOUNT = 20000000.0
MIN_PRICE_ROWS = MA_LONG + LONG_SLOPE_LOOKBACK

COMMISSION = 0.0003
MIN_COMMISSION = 5.0
STAMP_DUTY = 0.0005
TRANSFER_FEE = 0.00001
LOT_SIZE = 100


@dataclass
class Order:
    code: str
    shares: int


def normalize_code(code: str) -> str:
    raw = str(code).strip().lower().replace(".", "")
    if raw.startswith(("sh", "sz")) and len(raw) >= 8:
        return raw[:2] + raw[-6:]
    if raw.startswith(("6", "5", "9")):
        return "sh" + raw
    return "sz" + raw


def clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    # AkShare Tencent endpoint uses English columns; keep this tolerant.
    mapping = {}
    for col in df.columns:
        name = str(col).lower()
        if "date" in name or "日期" in str(col):
            mapping[col] = "date"
        elif name == "open" or "开盘" in str(col):
            mapping[col] = "open"
        elif name == "close" or "收盘" in str(col):
            mapping[col] = "close"
        elif name == "high" or "最高" in str(col):
            mapping[col] = "high"
        elif name == "low" or "最低" in str(col):
            mapping[col] = "low"
        elif name in ["volume", "vol"] or "成交量" in str(col):
            mapping[col] = "volume"
        elif name == "amount" or "成交额" in str(col):
            mapping[col] = "amount"
    out = df.rename(columns=mapping).copy()
    required = ["date", "open", "close", "high", "low"]
    missing = [col for col in required if col not in out.columns]
    if missing:
        raise ValueError(f"missing columns {missing}; got {list(df.columns)}")
    optional = [col for col in ["volume", "amount"] if col in out.columns]
    out = out[required + optional]
    out["date"] = pd.to_datetime(out["date"])
    for col in ["open", "close", "high", "low", "volume", "amount"]:
        if col not in out.columns:
            continue
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna().drop_duplicates("date").sort_values("date")
    return out


def fetch_historical_membership(trade_dates, refresh: bool = False) -> dict[pd.Timestamp, set[str]]:
    """Load point-in-time HS300 snapshots and forward-fill without look-ahead."""
    wanted = sorted(set(pd.Timestamp(value).normalize() for value in trade_dates))
    if not wanted:
        return {}
    if MEMBERSHIP_CACHE.exists() and not refresh:
        cached = pd.read_csv(MEMBERSHIP_CACHE, dtype={"code": str})
        cached["date"] = pd.to_datetime(cached["date"])
        cached["code"] = cached["code"].map(normalize_code)
        by_date = {
            pd.Timestamp(date).normalize(): set(group["code"])
            for date, group in cached.groupby("date")
        }
        if all(date in by_date for date in wanted):
            return dict((date, by_date[date]) for date in wanted)

    anchor_indexes = list(range(0, len(wanted), MEMBERSHIP_SNAPSHOT_DAYS))
    if anchor_indexes[-1] != len(wanted) - 1:
        anchor_indexes.append(len(wanted) - 1)
    anchors = [wanted[index] for index in anchor_indexes]
    snapshots = {}
    if MEMBERSHIP_SNAPSHOT_CACHE.exists() and not refresh:
        frame = pd.read_csv(MEMBERSHIP_SNAPSHOT_CACHE, dtype={"code": str})
        frame["date"] = pd.to_datetime(frame["date"])
        frame["code"] = frame["code"].map(normalize_code)
        snapshots = {
            pd.Timestamp(date).normalize(): set(group["code"])
            for date, group in frame.groupby("date")
        }

    missing = [date for date in anchors if date not in snapshots]
    if missing:
        try:
            import baostock as bs
        except ImportError as exc:
            raise RuntimeError(
                "Point-in-time constituents require baostock; run pip install baostock"
            ) from exc
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError("BaoStock login failed: {}".format(login.error_msg))
        try:
            for idx, date in enumerate(missing, 1):
                query = bs.query_hs300_stocks(date=date.strftime("%Y-%m-%d"))
                if query.error_code != "0":
                    raise RuntimeError(
                        "BaoStock HS300 query failed for {}: {}".format(
                            date.date(), query.error_msg))
                fields = list(query.fields)
                code_idx = fields.index("code")
                members = set()
                while query.next():
                    members.add(normalize_code(query.get_row_data()[code_idx]))
                if not members:
                    raise RuntimeError("BaoStock returned no HS300 members for {}".format(
                        date.date()))
                snapshots[date] = members
                if idx % 10 == 0 or idx == len(missing):
                    rows = [
                        {"date": snap_date, "code": code}
                        for snap_date in sorted(snapshots)
                        for code in sorted(snapshots[snap_date])
                    ]
                    pd.DataFrame(rows).to_csv(MEMBERSHIP_SNAPSHOT_CACHE, index=False)
                    print("membership snapshots {}/{} queried".format(idx, len(missing)))
        finally:
            bs.logout()

    by_date = {}
    current = None
    for idx, date in enumerate(wanted):
        if idx in anchor_indexes:
            current = snapshots[date]
        by_date[date] = set(current)
    rows = [
        {"date": date, "code": code}
        for date in wanted
        for code in sorted(by_date[date])
    ]
    pd.DataFrame(rows).to_csv(MEMBERSHIP_CACHE, index=False)
    print("membership {} dates cached from {} historical snapshots".format(
        len(wanted), len(anchors)))
    return by_date


def fetch_one(code: str, refresh: bool = False) -> tuple[str, pd.DataFrame | None, str]:
    path = CACHE / f"{code}_{START_DATE}_{END_DATE}_tx_qfq.csv"
    legacy_path = CACHE / f"{code}_{START_DATE}_{END_DATE}_em_qfq.csv"
    if not refresh:
        for cached_path in [path, legacy_path]:
            if not cached_path.exists():
                continue
            try:
                return code, clean_columns(pd.read_csv(cached_path)), "cache"
            except Exception:
                pass
    last_error = ""
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_hist_tx(
                symbol=code,
                start_date=START_DATE,
                end_date=END_DATE,
                adjust="qfq",
                timeout=20,
            )
            df = clean_columns(df)
            if len(df) < MIN_PRICE_ROWS:
                return code, None, "insufficient history: {} < {}".format(
                    len(df), MIN_PRICE_ROWS)
            df.to_csv(path, index=False)
            return code, df, "tencent"
        except Exception as exc:
            last_error = str(exc)
            time.sleep(0.8 + attempt)
    try:
        df = ak.stock_zh_a_hist(
            symbol=code[2:], period="daily", start_date=START_DATE,
            end_date=END_DATE, adjust="qfq")
        df = clean_columns(df)
        if len(df) < MIN_PRICE_ROWS:
            return code, None, "insufficient history: {} < {}".format(
                len(df), MIN_PRICE_ROWS)
        df.to_csv(path, index=False)
        return code, df, "eastmoney_fallback"
    except Exception as exc:
        last_error = "Tencent: {}; Eastmoney: {}".format(last_error, exc)
    return code, None, last_error


def load_price_data(codes: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(fetch_one, code) for code in codes]
        for idx, fut in enumerate(as_completed(futures), 1):
            code, df, status = fut.result()
            if df is None:
                failures.append((code, status))
            else:
                out[code] = df
            if idx % 25 == 0 or idx == len(futures):
                print(f"loaded {idx}/{len(futures)}; ok={len(out)} fail={len(failures)}")
    fail_path = ROOT / "backtest_data_failures.json"
    fail_path.write_text(
        json.dumps(failures, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if failures:
        print(f"failures written to {fail_path}")
    return out


def commission(shares: int, price: float) -> float:
    value = abs(shares) * price
    broker = max(value * COMMISSION, MIN_COMMISSION)
    transfer = value * TRANSFER_FEE
    stamp = value * STAMP_DUTY if shares < 0 else 0.0
    return broker + transfer + stamp


def sma(values: pd.Series, period: int) -> pd.Series:
    return values.rolling(period, min_periods=period).mean()


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "volume" not in out.columns:
        out["volume"] = np.nan
    if "amount" not in out.columns:
        out["amount"] = np.nan
    out["ma20"] = sma(out["close"], MA_FAST)
    out["ma60"] = sma(out["close"], MA_MID)
    out["ma250"] = sma(out["close"], MA_LONG)
    out["ma250_old"] = out["ma250"].shift(LONG_SLOPE_LOOKBACK)
    out["old_close"] = out["close"].shift(MOMENTUM_PERIOD)
    out["momentum60"] = out["close"] / out["old_close"] - 1.0
    out["avg_amount"] = out["amount"].rolling(AMOUNT_LOOKBACK, min_periods=1).mean()
    out["eligible"] = (
        (out["close"] > out["ma250"]) &
        (out["ma250"] > out["ma250_old"]) &
        (out["ma20"] > out["ma60"]) &
        (out["ma60"] > out["ma250"])
    )
    return out.set_index("date")


def enrich_benchmark(df: pd.DataFrame) -> pd.DataFrame:
    out = clean_columns(df)
    out["ma_market"] = sma(out["close"], MARKET_MA_PERIOD)
    fast = out["close"].ewm(span=12, adjust=False).mean()
    slow = out["close"].ewm(span=26, adjust=False).mean()
    dif = fast - slow
    dea = dif.ewm(span=9, adjust=False).mean()
    out["macd_hist"] = dif - dea
    out["pct_change"] = out["close"].pct_change()
    return out.set_index("date")


def is_star_market(code: str) -> bool:
    return str(code).lower().startswith("sh688")


def buy_block_reason(record: dict) -> str:
    if record.get("close", 0.0) < record.get("ma60", 0.0):
        return "below_ma60"
    if FILTER_SUSPENDED_BUYS and record.get("volume", 0.0) <= 0:
        return "suspended_or_zero_volume"
    prev_close = record.get("prev_close")
    close = record.get("close")
    if prev_close is not None and close is not None and prev_close > 0:
        if FILTER_LIMIT_UP_BUYS and close >= prev_close * (1.0 + LIMIT_UP_THRESHOLD):
            return "limit_up"
        if FILTER_LIMIT_DOWN_BUYS and close <= prev_close * (1.0 - LIMIT_DOWN_THRESHOLD):
            return "limit_down"
    avg_amount = record.get("avg_amount")
    if MIN_AVG_AMOUNT > 0 and avg_amount is not None and np.isfinite(avg_amount):
        if avg_amount < MIN_AVG_AMOUNT:
            return "low_liquidity"
    return ""


def check_market_trend(benchmark: pd.DataFrame, date: pd.Timestamp) -> tuple[bool, str]:
    if not MARKET_FILTER_ENABLED:
        return True, "disabled"
    if date not in benchmark.index:
        return False, "index_missing"
    row = benchmark.loc[date]
    values = [row.get("close"), row.get("ma_market"), row.get("macd_hist")]
    if any(pd.isna(value) for value in values):
        return False, "index_indicator_unavailable"
    if float(row.get("pct_change", 0.0)) <= -MARKET_DAILY_DROP_BLOCK:
        return False, "index_daily_drop"
    if float(row["close"]) > float(row["ma_market"]) and float(row["macd_hist"]) > 0:
        return True, "ok"
    return False, "index_trend_weak"


def floor_lot(value: float) -> int:
    if not np.isfinite(value) or value <= 0:
        return 0
    return int(math.floor(value / LOT_SIZE) * LOT_SIZE)


def select_target_codes(rows: list[dict], held_codes) -> list[str]:
    ranked_codes = [row["code"] for row in rows]
    exit_buffer = set(ranked_codes[:EXIT_RANK])
    held = set(held_codes)
    targets = [code for code in ranked_codes if code in held and code in exit_buffer]
    targets = targets[:MAX_POSITIONS]
    for code in ranked_codes[:ENTRY_RANK]:
        if len(targets) >= MAX_POSITIONS:
            break
        if code not in targets:
            targets.append(code)
    return targets


def backtest(data: dict[str, pd.DataFrame], benchmark: pd.DataFrame,
             membership_by_date: dict[pd.Timestamp, set[str]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    enriched = {code: enrich(df) for code, df in data.items()}
    benchmark_enriched = enrich_benchmark(benchmark)
    calendars = sorted(set().union(*(set(df.index) for df in enriched.values())))
    calendars = [d for d in calendars if d >= pd.Timestamp(TRADE_START)]

    cash = INITIAL_CASH
    positions: dict[str, int] = {}
    pending: list[Order] = []
    trades: list[dict] = []
    equity_rows: list[dict] = []
    last_rebalance_idx: int | None = None
    market_ok_streak = 0

    prev_date = None
    for trade_idx, date in enumerate(calendars):
        # Execute orders generated on the previous trading day at today's open.
        for order in pending:
            df = enriched.get(order.code)
            if df is None or date not in df.index:
                continue
            price = float(df.at[date, "open"])
            if not np.isfinite(price) or price <= 0:
                continue
            shares = int(order.shares)
            cost = commission(shares, price)
            if shares > 0:
                max_affordable = floor_lot((cash - cost) / price)
                shares = min(shares, max_affordable)
                if shares < LOT_SIZE:
                    continue
                cost = commission(shares, price)
                cash -= shares * price + cost
                positions[order.code] = positions.get(order.code, 0) + shares
            else:
                held = positions.get(order.code, 0)
                sell_shares = min(abs(shares), held)
                if sell_shares < held:
                    sell_shares = floor_lot(sell_shares)
                if sell_shares <= 0:
                    continue
                shares = -sell_shares
                cost = commission(shares, price)
                cash += sell_shares * price - cost
                remaining = held - sell_shares
                if remaining > 0:
                    positions[order.code] = remaining
                else:
                    positions.pop(order.code, None)
            trades.append({
                "date": date.date().isoformat(),
                "code": order.code,
                "shares": shares,
                "price": price,
                "commission": cost,
                "cash": cash,
            })
        pending = []

        close_value = cash
        for code, shares in positions.items():
            df = enriched.get(code)
            if df is not None and date in df.index:
                close_value += shares * float(df.at[date, "close"])
            elif prev_date is not None and df is not None and prev_date in df.index:
                close_value += shares * float(df.at[prev_date, "close"])

        # Signals are generated from today's close and executed next open.
        rows = []
        all_rows = {}
        blocked_counts: dict[str, int] = {}
        members = membership_by_date.get(pd.Timestamp(date).normalize())
        if members is None:
            raise RuntimeError("Missing point-in-time HS300 membership for {}".format(date))
        for code, df in enriched.items():
            if date not in df.index:
                continue
            row = df.loc[date]
            if pd.isna(row["ma250_old"]) or pd.isna(row["momentum60"]):
                continue
            record = {
                "code": code,
                "close": float(row["close"]),
                "prev_close": float(df.at[prev_date, "close"]) if prev_date in df.index else None,
                "volume": float(row["volume"]) if not pd.isna(row["volume"]) else 0.0,
                "amount": float(row["amount"]) if not pd.isna(row["amount"]) else None,
                "avg_amount": float(row["avg_amount"]) if not pd.isna(row["avg_amount"]) else None,
                "ma60": float(row["ma60"]),
                "eligible": bool(row["eligible"]),
                "momentum60": float(row["momentum60"]),
            }
            all_rows[code] = record
            if code not in members:
                continue
            if not record["eligible"]:
                continue
            if FILTER_STAR_MARKET and is_star_market(code):
                blocked_counts["star_market"] = blocked_counts.get("star_market", 0) + 1
                continue
            reason = buy_block_reason(record)
            if reason:
                blocked_counts[reason] = blocked_counts.get(reason, 0) + 1
                continue
            if record["eligible"]:
                rows.append(record)
        rows.sort(key=lambda x: x["momentum60"], reverse=True)

        market_ok, market_reason = check_market_trend(benchmark_enriched, date)
        if market_ok:
            market_ok_streak += 1
        else:
            market_ok_streak = 0
        market_allows_buys = (
            (not MARKET_FILTER_ENABLED) or
            market_ok_streak >= MARKET_OK_STREAK_REQUIRED
        )

        order_codes = set()
        for code, shares in list(positions.items()):
            row = all_rows.get(code)
            if row and row["close"] < row["ma60"]:
                pending.append(Order(code, -shares))
                order_codes.add(code)

        rebalanced = (
            last_rebalance_idx is None or
            trade_idx - last_rebalance_idx >= REBALANCE_DAYS
        )
        if rebalanced:
            last_rebalance_idx = trade_idx
            targets = select_target_codes(rows, positions.keys())
            target_set = set(targets)
            for code, shares in list(positions.items()):
                if code not in target_set and code not in order_codes:
                    pending.append(Order(code, -shares))
                    order_codes.add(code)

            if market_allows_buys:
                target_value = close_value / MAX_POSITIONS
                for code in targets:
                    if code in order_codes:
                        continue
                    row = all_rows.get(code)
                    if not row:
                        continue
                    current = positions.get(code, 0)
                    target_shares = floor_lot(target_value * (1.0 - CASH_BUFFER) / row["close"])
                    delta = target_shares - current
                    if abs(delta) >= LOT_SIZE:
                        pending.append(Order(code, delta))
                        order_codes.add(code)

        equity_rows.append({
            "date": date,
            "equity": close_value,
            "cash": cash,
            "positions": len([s for s in positions.values() if s > 0]),
            "eligible": len(rows),
            "rebalanced": rebalanced,
            "market_ok": market_ok,
            "market_reason": market_reason,
            "market_ok_streak": market_ok_streak,
            "blocked": json.dumps(blocked_counts, ensure_ascii=False, sort_keys=True),
        })
        prev_date = date

    equity = pd.DataFrame(equity_rows).set_index("date")
    trade_df = pd.DataFrame(trades)
    return equity, trade_df


def calc_stats(equity: pd.DataFrame, benchmark: pd.DataFrame) -> dict:
    daily = equity["equity"].pct_change().dropna()
    total_return = equity["equity"].iloc[-1] / equity["equity"].iloc[0] - 1
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    annual = (1 + total_return) ** (1 / years) - 1
    cummax = equity["equity"].cummax()
    drawdown = equity["equity"] / cummax - 1
    sharpe = np.nan
    if daily.std() > 0:
        sharpe = daily.mean() / daily.std() * np.sqrt(252)

    bm = benchmark.set_index("date").sort_index()
    bm = bm[(bm.index >= equity.index[0]) & (bm.index <= equity.index[-1])]
    bm_return = bm["close"].iloc[-1] / bm["close"].iloc[0] - 1 if len(bm) else np.nan
    bm_annual = (1 + bm_return) ** (1 / years) - 1 if len(bm) else np.nan
    bm_dd = np.nan
    if len(bm):
        bm_dd = (bm["close"] / bm["close"].cummax() - 1).min()

    return {
        "start": equity.index[0].date().isoformat(),
        "end": equity.index[-1].date().isoformat(),
        "initial_equity": float(equity["equity"].iloc[0]),
        "final_equity": float(equity["equity"].iloc[-1]),
        "total_return": float(total_return),
        "annual_return": float(annual),
        "max_drawdown": float(drawdown.min()),
        "sharpe": None if np.isnan(sharpe) else float(sharpe),
        "trade_count": int(0),
        "benchmark_total_return": None if np.isnan(bm_return) else float(bm_return),
        "benchmark_annual_return": None if np.isnan(bm_annual) else float(bm_annual),
        "benchmark_max_drawdown": None if np.isnan(bm_dd) else float(bm_dd),
    }


def main():
    benchmark = clean_columns(ak.stock_zh_index_daily_tx(symbol="sh000300"))
    membership_dates = benchmark.loc[
        (benchmark["date"] >= pd.Timestamp(TRADE_START)) &
        (benchmark["date"] <= pd.Timestamp(END_DATE)), "date"]
    membership = fetch_historical_membership(membership_dates)
    codes = sorted(set().union(*membership.values()))
    print(f"historical_constituents={len(codes)}")
    data = load_price_data(codes)
    print(f"usable_symbols={len(data)}")
    equity, trades = backtest(data, benchmark, membership)
    stats = calc_stats(equity, benchmark)
    stats["trade_count"] = int(len(trades))
    stats["constituents"] = len(codes)
    stats["usable_symbols"] = len(data)
    stats["constituent_mode"] = "point_in_time_baostock_20day_snapshots"

    equity.to_csv(ROOT / "backtest_equity_curve.csv", encoding="utf-8-sig")
    trades.to_csv(ROOT / "backtest_trades.csv", index=False, encoding="utf-8-sig")
    (ROOT / "backtest_summary.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
