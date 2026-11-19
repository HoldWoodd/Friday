# RITCx Algorithmic ETF Arbitrage trader

This branch contains a separate trader for the Algorithmic ETF Arbitrage Case:

```text
etf_arb_trader.py
test_etf_arb_trader.py
```

It does not replace the volatility-case trader. The strategy supports both the
local RIT Client REST API and the competition DMA API.

## Strategy

The equilibrium relation in CAD is:

```text
RITC (USD) × USD/CAD = BULL (CAD) + BEAR (CAD)
```

The program evaluates both executable directions using bid/ask prices:

- Basket rich: buy RITC, sell BULL and BEAR.
- ETF rich: sell RITC, buy BULL and BEAR.

It subtracts market fees before trading, limits child orders to visible top-of-
book size and the configured order size, applies the RITC ×2 position-limit
multiplier, checks projected positions against `/limits`, and neutralizes the
resulting USD currency position.

Private fixed-price RITC tenders are accepted only when their executable
basket hedge has sufficient net edge and the completed package fits within
softened API limits. All other tenders are explicitly declined, so they do not
remain unanswered until timeout. Competitive tenders are declined rather than
bid blindly.
After acceptance, BULL/BEAR hedges and an USD hedge are submitted immediately.

ETF Creation and Redemption converters are not automated because the case PDF
states that they are available only through the RIT Client to human participants.

## Install and test

```bash
python -m pip install -r requirements.txt
python -m unittest -v test_etf_arb_trader.py
```

The default mode is live. Always use `--dry-run` for the first test.

## Spyder with local REST API

Run RIT Client on the same computer, open the ETF Arbitrage Case, enable its
API, and set Spyder's working directory to the downloaded project directory.

Dry-run command-line options:

```text
--dry-run --api-mode rest --api-key YOUR_KEY
```

After verifying quotes, positions, tender decisions and edges, remove
`--dry-run` to enable orders and tender acceptance:

```text
--api-mode rest --api-key YOUR_KEY
```

The default REST API key is `JOE`, so `--api-key` can be omitted when the RIT
Client uses that key.

## DMA API

DMA talks directly to the competition server and does not require RIT Client.
Do not put credentials into the Python file or GitHub. In Spyder's IPython
console, set them for the current kernel:

```python
import os
os.environ["RIT_DMA_USERNAME"] = "your trader ID"
os.environ["RIT_DMA_PASSWORD"] = "your password"
```

Then use these command-line options:

```text
--dry-run --api-mode dma
```

Remove `--dry-run` only after the DMA dry-run works. HTTP 429 responses are
handled with `Retry-After`/exponential retry logic.

## Defaults

```text
poll interval             0.5 seconds
arbitrage child order     1,000 shares per leg
minimum net arb edge      CAD 0.02/share after market fees
minimum tender net edge   CAD 0.03/share after stock hedge fees
limit usage               85% of limits reported by /limits
USD hedge tolerance       100 USD
```

Useful options:

```text
--order-size 500
--min-net-edge 0.03
--tender-min-net-edge 0.05
--limit-usage 0.75
--poll-seconds 1
```

## Safety and limitations

- The three arbitrage legs cannot be submitted atomically. A leg can move or
  fail before the package completes.
- Market orders can walk beyond displayed top-of-book prices. The expected
  edge is not guaranteed.
- The implementation assumes the tender `action` is the trader's inventory
  action, consistent with the RIT API convention: BUY adds RITC and SELL removes
  RITC. Confirm this in dry-run/client tender display before live use.
- Fixed tenders are evaluated; competitive tenders are intentionally skipped.
- USD inventory is hedged after completed packages, so temporary currency risk
  exists between legs.
- There is no automated converter access, as prohibited by the case design.
- The fallback limits are used only when `/limits` supplies no positive limits.
- Two live instances on the same Trader ID can duplicate packages and hedges;
  use only one live instance per account.
