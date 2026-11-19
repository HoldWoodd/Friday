# RITCx Volatility Trader

`rit_vol_trader.py` is a compact, aggressive trader for the RIT volatility
case. The complete live strategy is 190 lines; tests are kept separately.

## Strategy

- Reads `/case`, `/news`, and `/securities` from RIT Client.
- Extracts the current week's volatility forecast from news.
- Values RTM options with Black–Scholes at executable bid/ask prices.
- Buys or sells the option with the largest estimated edge.
- Sends 50 contracts per order by default.
- Shrinks an order only near the published limits: 400 contracts per ticker,
  2,500 option gross, or ±1,000 option net.
- Delta-hedges with RTM immediately after an option order and whenever absolute
  portfolio Delta reaches 2,500.
- Keeps RTM inside its ±50,000 hard position limit.
- From tick 275 onward, closes option positions 50 contracts at a time.

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
  --size 50 \
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
