#!/usr/bin/env python3
"""Compact aggressive trader for the RIT volatility case."""
import argparse, logging, math, os, re, time
from dataclasses import dataclass
import requests

TICKS, DAYS, MULT = 300, 20, 100
OPT_GROSS, OPT_NET, ETF_LIMIT = 2500, 1000, 50000
OPT_ORDER, ETF_ORDER, PER_TICKER = 50, 10000, 400
OPT_FEE, ETF_FEE = 2.0, 0.02
LOG = logging.getLogger("rit-vol-trader")
OPT_RE = re.compile(r"^RTM(\d+(?:\.\d+)?)([CP])$")

@dataclass
class Option:
    ticker: str
    strike: float
    kind: str
    bid: float
    ask: float
    position: int

class API:
    def __init__(self, url, key, timeout=2):
        self.url, self.timeout = url.rstrip("/"), timeout
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = key

    def get(self, path):
        response = self.session.get(self.url + path, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def order(self, ticker, action, quantity):
        response = self.session.post(self.url + "/orders", params={
            "ticker": ticker, "type": "MARKET", "action": action, "quantity": quantity
        }, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

def black_scholes(kind, spot, strike, years, rate, vol):
    if years <= 0:
        price = max(spot - strike, 0) if kind == "C" else max(strike - spot, 0)
        delta = 1.0 if kind == "C" and spot > strike else -1.0 if kind == "P" and spot < strike else 0.0
        return price, delta
    cdf = lambda x: .5 * (1 + math.erf(x / math.sqrt(2)))
    root = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / root
    d2, discount = d1 - root, math.exp(-rate * years)
    if kind == "C":
        return spot * cdf(d1) - strike * discount * cdf(d2), cdf(d1)
    return strike * discount * cdf(-d2) - spot * cdf(-d1), cdf(d1) - 1

def week(tick):
    return min(4, tick // 75 + 1)

def news_vol(news, tick):
    current, fallback = week(tick), None
    for item in sorted(news, key=lambda x: x.get("tick", 0), reverse=True):
        text = f'{item.get("headline", "")} {item.get("body", "")}'
        values = [float(x) / 100 for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", text)]
        if not values:
            continue
        labeled = re.search(r"week\s*([1-4])", text, re.I)
        if labeled and int(labeled.group(1)) == current:
            return values[0]
        if not labeled and fallback is None:
            fallback = values[0]
    return fallback

def market(securities):
    etf, options = None, []
    for item in securities:
        if item.get("ticker") == "RTM":
            etf = item
            continue
        match = OPT_RE.match(item.get("ticker", ""))
        if match:
            options.append(Option(item["ticker"], float(match.group(1)), match.group(2),
                float(item.get("bid", 0)), float(item.get("ask", 0)), int(item.get("position", 0))))
    if not etf or not options:
        raise RuntimeError("RTM market is unavailable")
    return etf, options

def option_quantity(options, target, action, requested=OPT_ORDER):
    direction = 1 if action == "BUY" else -1
    gross, net = sum(abs(x.position) for x in options), sum(x.position for x in options)
    quantity = 0
    for size in range(1, min(requested, 100) + 1):
        position = target.position + direction * size
        new_gross = gross - abs(target.position) + abs(position)
        if abs(position) <= PER_TICKER and new_gross <= OPT_GROSS and abs(net + direction * size) <= OPT_NET:
            quantity = size
        else:
            break
    return quantity

def portfolio_delta(etf_position, options, spot, years, rate, vol):
    total = float(etf_position)
    for option in options:
        total += option.position * black_scholes(option.kind, spot, option.strike, years, rate, vol)[1] * MULT
    return total

def send(api, ticker, signed_quantity, maximum, live):
    action, remaining = ("BUY" if signed_quantity > 0 else "SELL"), abs(signed_quantity)
    while remaining:
        quantity = min(remaining, maximum)
        LOG.info("%s %s x%d", action, ticker, quantity)
        if live:
            api.order(ticker, action, quantity)
        remaining -= quantity

def state(api, vol):
    case = api.get("/case")
    etf, options = market(api.get("/securities"))
    spot = (float(etf["bid"]) + float(etf["ask"])) / 2
    years = max((TICKS - int(case["tick"])) / TICKS * DAYS / 240, 1e-6)
    raw_rate = float(etf.get("interest_rate", 0))
    return case, etf, options, spot, years, raw_rate / 100 if raw_rate > 1 else raw_rate, vol

def hedge(api, vol, live):
    _, etf, options, spot, years, rate, _ = state(api, vol)
    position = int(etf.get("position", 0))
    delta = portfolio_delta(position, options, spot, years, rate, vol)
    if abs(delta) >= 2500:
        desired = max(-ETF_LIMIT, min(ETF_LIMIT, position - round(delta)))
        send(api, "RTM", desired - position, ETF_ORDER, live)

def run(args):
    api, cached = API(args.url, args.key), None
    while True:
        try:
            case = api.get("/case")
            tick = int(case["tick"])
            if tick >= TICKS:
                return
            vol = args.vol / 100 if args.vol else news_vol(api.get("/news"), tick) or cached
            if not vol:
                raise RuntimeError("no current volatility forecast")
            cached = vol
            _, etf, options, spot, years, rate, _ = state(api, vol)
            if tick >= 275:
                target = next((x for x in options if x.position), None)
                if target:
                    send(api, target.ticker, -max(-50, min(50, target.position)), 100, args.live)
                    hedge(api, vol, args.live)
                time.sleep(args.poll)
                continue
            best = None
            for option in options:
                if option.bid <= 0 or option.ask <= 0:
                    continue
                fair, delta = black_scholes(option.kind, spot, option.strike, years, rate, vol)
                fee = OPT_FEE + abs(delta) * MULT * ETF_FEE
                trades = (("BUY", (fair - option.ask) * MULT - fee, option.ask),
                          ("SELL", (option.bid - fair) * MULT - fee, option.bid))
                for action, edge, price in trades:
                    if edge >= args.edge and (best is None or edge > best[0]):
                        best = edge, option, action, fair, price
            if best:
                edge, option, action, fair, price = best
                quantity = option_quantity(options, option, action, args.size)
                if quantity:
                    LOG.info("signal %s %s x%d fair=%.3f market=%.3f edge=$%.2f", action, option.ticker, quantity, fair, price, edge)
                    send(api, option.ticker, quantity if action == "BUY" else -quantity, 100, args.live)
                    hedge(api, vol, args.live)
            else:
                hedge(api, vol, args.live)
            time.sleep(args.poll)
        except (requests.RequestException, RuntimeError, KeyError, ValueError) as error:
            LOG.warning("%s", error)
            time.sleep(max(args.poll, .5))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:9999/v1")
    parser.add_argument("--key", default=os.getenv("RIT_API_KEY", "Rotman"))
    parser.add_argument("--size", type=int, default=OPT_ORDER)
    parser.add_argument("--edge", type=float, default=5)
    parser.add_argument("--vol", type=float)
    parser.add_argument("--poll", type=float, default=.5)
    parser.add_argument("--dry-run", dest="live", action="store_false")
    args = parser.parse_args()
    if not 1 <= args.size <= 100 or args.poll <= 0:
        parser.error("size must be 1..100 and poll must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(args)

if __name__ == "__main__":
    main()
