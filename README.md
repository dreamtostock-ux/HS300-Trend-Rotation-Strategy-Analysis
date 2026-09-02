# HS300 Trend Rotation Strategy

Big-QMT built-in Python version of an HS300 trend rotation strategy.

## Files

- `hs300_trend_rotation_bigqmt.py`: QMT strategy source with `init(C)` and `handlebar(C)` entry points.
- `backtest_hs300_trend_rotation.py`: reproducible public-data backtest script.
- `backtest_summary.json`: latest backtest summary.
- `backtest_equity_curve.csv`: latest backtest equity curve.
- `backtest_trades.csv`: latest backtest trade log.
- `parameter_scan_hs300.py`: small parameter sensitivity scan for rebalance/rank/MA settings.

## Strategy Summary

- Universe: current HS300 sector constituents from QMT.
- Trend filter: close above MA250, rising MA250, and MA20 > MA60 > MA250.
- Ranking: 60-day return among eligible stocks.
- Rebalance: every 5 trading days, up to 5 positions.
- Exit: daily close below MA60, or exit buffer failure on rebalance day.
- Borrowed safety components: HS300 index market filter, ST/STAR-board buy filter, suspended/limit-up/limit-down buy avoidance, liquidity filter, state persistence, active-order checks, and account/position query failure protection.
- Rebalance timing: based on recorded trading dates instead of a simple counter.
- Logging: prints candidate counts, blocked-buy reasons, top rankings, targets, and position drift.

## Safety

`LIVE_TRADING = False` by default. In this mode, the strategy only logs intended orders and does not call live trading orders.

Before enabling live trading:

1. Run it in big-QMT simulation/log-only mode.
2. Confirm HS300 sector data, daily bars, account, positions, and order queries work in your broker's QMT build.
3. Change `LIVE_TRADING = True` only after validation.

This project is for strategy research and execution testing only. It is not investment advice or a promise of returns.

## Latest Backtest

Public-data backtest window: 2021-09-02 to 2026-09-02, initial capital 1,000,000 CNY.

- Strategy total return: 93.27%; annual return: 14.09%.
- Max drawdown: -41.04%; Sharpe: 0.58.
- Trade count: 512.
- HS300 benchmark total return: -5.30%; annual return: -1.08%; max drawdown: -37.86%.

## Backtest Limitation

The included backtest uses current HS300 constituents across the full historical window, so it has survivorship bias. The latest run used 286 usable symbols out of 288 current constituents because several public-data requests failed or had insufficient history. Treat it as an approximate strategy sanity check rather than a fully point-in-time index constituent backtest.
