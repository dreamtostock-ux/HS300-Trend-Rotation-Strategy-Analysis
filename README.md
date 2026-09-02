# HS300 Trend Rotation Strategy

Big-QMT built-in Python version of an HS300 trend rotation strategy.

## Files

- `hs300_trend_rotation_bigqmt.py`: QMT strategy source with `init(C)` and `handlebar(C)` entry points.
- `backtest_hs300_trend_rotation.py`: reproducible public-data backtest script.
- `backtest_summary.json`: latest backtest summary.
- `backtest_equity_curve.csv`: latest backtest equity curve.
- `backtest_trades.csv`: latest backtest trade log.

## Strategy Summary

- Universe: current HS300 sector constituents from QMT.
- Trend filter: close above MA250, rising MA250, and MA20 > MA60 > MA250.
- Ranking: 60-day return among eligible stocks.
- Rebalance: every 5 trading days, up to 5 positions.
- Exit: daily close below MA60, or exit buffer failure on rebalance day.

## Safety

`LIVE_TRADING = False` by default. In this mode, the strategy only logs intended orders and does not call live trading orders.

Before enabling live trading:

1. Run it in big-QMT simulation/log-only mode.
2. Confirm HS300 sector data, daily bars, account, positions, and order queries work in your broker's QMT build.
3. Change `LIVE_TRADING = True` only after validation.

This project is for strategy research and execution testing only. It is not investment advice or a promise of returns.

## Backtest Limitation

The included backtest uses current HS300 constituents across the full historical window, so it has survivorship bias. Treat it as an approximate strategy sanity check rather than a fully point-in-time index constituent backtest.
