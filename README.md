# RITCx Volatility Trader

`rit_vol_trader.py` is a compact, aggressive trader for the RIT volatility
case. Tests are kept separately.

## Strategy

- Reads `/case`, `/news`, and `/securities` from RIT Client.
- Extracts the current week's volatility forecast from news, using the official
  tick boundaries (1–75, 76–150, 151–225, and 226–300).
- Values RTM options with Black–Scholes at executable bid/ask prices.
- Scores profitable options with a preference for trades that reduce portfolio
  Delta, and may pair two opposite-Delta trades before hedging once.
- Requests 100 contracts by default, then selects 100, 50, 25, or zero based on
  projected Delta, ETF hedge capacity, and the published option limits.
- Uses a hysteresis hedge: RTM trading starts beyond ±2,500 portfolio Delta and
  targets ±1,000 instead of repeatedly returning to zero.
- Checks portfolio Delta with both the news forecast and option-implied
  volatility. New option risk normally stays within ±5,000; a trade outside
  that band is allowed only when it reduces existing risk and can be hedged.
- Treats ±30,000 RTM as a warning level and ±35,000 as the strategy limit. If
  that RTM capacity cannot bring Delta below the entry guard, closes 50
  contracts of the option that reduces the most risk instead of using ±50,000.
- During ticks 70–75, 145–150, and 220–225, stops opening positions and flattens
  the portfolio before the scheduled weekly volatility reset.
- From tick 275 onward, closes all options and then keeps flattening RTM to zero;
  it never leaves an old option hedge open after the options are gone.

The official case fine is assessed every second as
`$0.10 × max(0, |portfolio Delta| - 7,000)`, where portfolio Delta includes the
RTM position plus every option's Delta times 100. The internal ±2,500 hedge
trigger and ±5,000 entry guard leave a buffer below that threshold. They reduce
fine risk but cannot guarantee a fixed `fines / P&L` ratio because future P&L is
unknown. Option and initial hedge fees are deducted from each signal's estimated
edge.

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
