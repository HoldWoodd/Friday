# RITCx Volatility Trader

`rit_vol_trader.py` is a compact, aggressive trader for the RIT volatility
case. Tests are kept separately.

## Strategy

- Reads `/case`, `/news`, and `/securities` from RIT Client.
- Extracts the current week's volatility forecast from news.
- Values RTM options with Black–Scholes at executable bid/ask prices.
- Scores profitable options with a preference for trades that reduce portfolio
  Delta, and may pair two opposite-Delta trades before hedging once.
- Requests 100 contracts by default, then selects 100, 50, 25, or zero based on
  projected Delta, ETF hedge capacity, and the published option limits.
- Uses a hysteresis hedge: RTM trading starts beyond ±3,000 portfolio Delta and
  targets ±1,000 instead of repeatedly returning to zero.
- Treats ±40,000 RTM as a warning level, ±45,000 as the normal hedge-capacity
  boundary, and uses the final 5,000 shares only for emergency Delta control.
- From tick 275 onward, closes all option positions before one final RTM hedge.

The REST feed does not expose a live fine total, so the program cannot measure
`fines / P&L` reliably. It stays inside the published position limits to target
limit fines below the requested 3% of P&L. Option and initial hedge fees are
deducted from every signal's estimated edge.

## Install and test

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest -v
```

## Run

Live trading is the default:

```bash
python rit_vol_trader.py
```

Useful overrides:

```bash
python rit_vol_trader.py \
  --size 100 \
  --edge 5 \
  --poll 0.5 \
  --vol 20
```

- `--size`: requested option order size, from 1 to 100.
- `--edge`: minimum estimated dollars per contract after immediate fees.
- `--vol`: annualized volatility percentage; overrides news parsing.
- `--dry-run`: logs orders without sending them.
- `--key`: RIT API key; defaults to `RIT_API_KEY` or `Rotman`.

The trader requires a running RIT Client with the REST API enabled at
`http://localhost:9999/v1`.
