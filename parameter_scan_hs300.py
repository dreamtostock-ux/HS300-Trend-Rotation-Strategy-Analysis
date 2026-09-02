# coding: utf-8
"""Run a small parameter sensitivity scan for the HS300 rotation backtest."""

from __future__ import annotations

import itertools
import json

import akshare as ak
import pandas as pd

import backtest_hs300_trend_rotation as bt


GRID = {
    "REBALANCE_DAYS": [3, 5, 10],
    "ENTRY_RANK": [5, 8],
    "EXIT_RANK": [10, 15],
    "MA_MID": [50, 60],
    "MA_LONG": [200, 250],
}


def set_params(params: dict) -> None:
    for key, value in params.items():
        setattr(bt, key, value)
    bt.DAILY_BAR_COUNT = max(
        bt.MA_LONG + bt.LONG_SLOPE_LOOKBACK,
        bt.MOMENTUM_PERIOD + 1,
    ) + 10


def main() -> None:
    codes = bt.fetch_constituents()
    data = bt.load_price_data(codes)
    benchmark = bt.clean_columns(ak.stock_zh_index_daily_tx(symbol="sh000300"))

    keys = list(GRID.keys())
    rows = []
    for values in itertools.product(*(GRID[key] for key in keys)):
        params = dict(zip(keys, values))
        if params["EXIT_RANK"] < params["ENTRY_RANK"]:
            continue
        set_params(params)
        equity, trades = bt.backtest(data, benchmark)
        stats = bt.calc_stats(equity, benchmark)
        stats["trade_count"] = int(len(trades))
        stats["params"] = json.dumps(params, sort_keys=True)
        rows.append({**params, **stats})
        print(
            "params={} total={:.2%} annual={:.2%} dd={:.2%} sharpe={}".format(
                params,
                stats["total_return"],
                stats["annual_return"],
                stats["max_drawdown"],
                stats["sharpe"],
            ),
            flush=True,
        )

    out = pd.DataFrame(rows).sort_values(
        ["annual_return", "max_drawdown"], ascending=[False, False])
    out.to_csv(bt.ROOT / "parameter_scan_results.csv", index=False, encoding="utf-8-sig")
    print("wrote {}".format(bt.ROOT / "parameter_scan_results.csv"), flush=True)


if __name__ == "__main__":
    main()
