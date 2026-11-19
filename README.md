# RITCx Volatility Trading Case — first-pass trader

This Python script connects to the RIT Client REST API, values the RTM European
options with Black–Scholes, looks for executable mispricing, observes the case
position limits, and delta-hedges with RTM. It starts in **live mode** and sends
real market orders. Pass `--dry-run` for read-only signal testing.

## Case rules represented in the code

- 300 ticks represent 20 trading days out of a 240-day trading year.
- The underlying is the non-dividend-paying RTM ETF.
- Each option contract represents 100 RTM shares.
- Option fee: $2 per contract; RTM fee: $0.02 per share.
- Maximum order: 100 option contracts or 10,000 RTM shares.
- Options: gross position limit 2,500; net position limit 1,000.
- RTM position limit: 50,000 shares.
- Hard portfolio Delta boundary: ±7,000; the default internal hedge trigger is
  ±3,500 with a target of ±500.
- Risk-free rate comes from the RTM security record and can be overridden.
- The script only trades a current-week volatility value found in news, unless
  the operator explicitly supplies `--vol-percent`. A successfully parsed value
  is cached for the rest of that same week so a temporary news omission does not
  interrupt trading; the cache is never carried into a new week.
- Every loop reads actual RTM and option positions from `/securities`. Logs show
  stock position, nonzero option positions, option gross/net and portfolio Delta.

## Installation

Use the existing checkout; do not create another Git worktree for cloud tasks.

```bash
cd /workspace/Friday
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest -v
```

On the competition computer, start RIT Client, open the Volatility Trading
Case, and enable its API. The default endpoint is `http://localhost:9999/v1`
and the template API key is `Rotman`. If the instructor provides another key:

```bash
export RIT_API_KEY='the-key-shown-in-RIT-Client'
```

## Safe operating procedure

1. Start the case in RIT Client.
2. Run the script with `--dry-run` first:

   ```bash
   python rit_vol_trader.py --dry-run
   ```

3. Confirm that it prints a sensible RTM price, current volatility, portfolio
   Delta, and `DRY-RUN would ...` messages. If the instructor's news wording is
   not recognized, specify the announced annualized value explicitly, e.g.:

   ```bash
   python rit_vol_trader.py --vol-percent 20
   ```

4. Stop with Ctrl-C. Only after checking the dry-run output, run without
   `--dry-run` to enable orders:

   ```bash
   python rit_vol_trader.py
   ```

5. Keep the RIT Client visible. Stop the script immediately if its displayed
   positions differ from the client or repeated API warnings appear.

Useful conservative controls:

```bash
python rit_vol_trader.py \
  --option-order-size 10 \
  --min-profit 5 \
  --hedge-trigger 3000 \
  --hedge-target 500
```

`--min-profit` is estimated dollars per option contract after the $2 option
fee and the estimated fee for the immediate Delta hedge. It is not a guaranteed
profit and does not include every possible future re-hedge or exit cost.

## Current limitations

- News parsing recognizes English percentage announcements for the current
  week. Novel instructor wording may require `--vol-percent` or parser changes.
- The first version uses the current week's announced volatility as a constant
  Black–Scholes volatility for the remaining maturity. It records neither a
  forward weekly variance curve nor the next-week range.
- It sends market orders. There is no limit-order market making, order-book
  queue model, or fill reconciliation beyond refreshing positions next loop.
- It estimates only the immediate option fee and first Delta hedge fee. Future
  re-hedges, closing costs, volatility uncertainty, and slippage are not fully
  modeled.
- It ranks single option legs. It does not construct straddles, strangles,
  spreads, conversions, reversals, or optimize a portfolio across strikes.
- Delta is recomputed every loop, but Gamma, Vega, Theta and stress losses are
  not constrained.
- REST requests and orders are sequential. There is no atomic option-and-hedge
  transaction, so brief residual Delta can exist between fills.
- The strategy has unit tests but cannot be end-to-end tested without a running
  RIT Client and an active case.

## Natural next improvements

1. Parse current and next-week forecasts into a weekly forward-variance curve.
2. Add implied-volatility inversion and compare market IV directly to the
   analyst forecast with confidence bands.
3. Optimize multi-leg trades by net Vega, Gamma, Delta, fees and position limits.
4. Add fill-aware order tracking, cancellation and controlled limit orders.
5. Add Gamma-aware adaptive hedging instead of a fixed Delta band.
6. Record ticks, news, quotes, signals and fills for replay/backtesting.
7. Build a simulator/replay harness and calibrate thresholds from repeated cases.
