#!/usr/bin/env python3
"""Compact aggressive trader for the RIT volatility case."""
import argparse, logging, math, os, re, time
from dataclasses import dataclass
import requests

TICKS, DAYS, MULT = 300, 20, 100
OPT_GROSS, OPT_NET, ETF_LIMIT = 2500, 1000, 50000
OPT_ORDER, ETF_ORDER, PER_TICKER = 100, 10000, 400
DELTA_LIMIT, ENTRY_LIMIT = 7000, 5000
HEDGE_TRIGGER, HEDGE_TARGET = 2500, 1000
ETF_WARNING, ETF_RESERVE = 40000, 45000
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

def implied_vol(kind, price, spot, strike, years, rate):
    if price <= 0 or years <= 0:
        return None
    low, high = .001, 3.0
    if not black_scholes(kind, spot, strike, years, rate, low)[0] <= price <= black_scholes(kind, spot, strike, years, rate, high)[0]:
        return None
    for _ in range(35):
        mid = (low + high) / 2
        if black_scholes(kind, spot, strike, years, rate, mid)[0] < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2

def week(tick):
    return min(4, (max(tick, 1) - 1) // 75 + 1)

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
        if not labeled and not re.search(r"next\s+week|between", text, re.I) and fallback is None:
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

def projected_options(options, target, action, quantity):
    direction = 1 if action == "BUY" else -1
    return [Option(x.ticker, x.strike, x.kind, x.bid, x.ask,
        x.position + direction * quantity if x.ticker == target.ticker else x.position)
        for x in options]

def shifted_bounds(bounds, action, forecast_delta, market_delta, quantity):
    direction = 1 if action == "BUY" else -1
    changes = (direction * forecast_delta * MULT * quantity,
               direction * market_delta * MULT * quantity)
    return bounds[0] + min(changes), bounds[1] + max(changes)

def worst_delta(bounds):
    return max(abs(bounds[0]), abs(bounds[1]))

def planned_hedge(position, bounds, limit):
    if worst_delta(bounds) <= HEDGE_TRIGGER:
        return 0
    midpoint = sum(bounds) / 2
    target = math.copysign(HEDGE_TARGET, midpoint)
    desired = max(-limit, min(limit, position + round(target - midpoint)))
    return desired - position

def adaptive_quantity(options, target, action, forecast_delta, market_delta,
                      bounds, etf_position, requested=OPT_ORDER):
    capacity = option_quantity(options, target, action, requested)
    for quantity in dict.fromkeys((min(100, capacity), min(50, capacity), min(25, capacity))):
        if quantity <= 0:
            continue
        projected = shifted_bounds(bounds, action, forecast_delta, market_delta, quantity)
        if worst_delta(projected) > ENTRY_LIMIT and worst_delta(projected) >= worst_delta(bounds):
            continue
        change = planned_hedge(etf_position, projected, ETF_RESERVE)
        desired_etf = etf_position + change
        post_hedge = (projected[0] + change, projected[1] + change)
        if abs(etf_position) >= ETF_WARNING and abs(desired_etf) >= abs(etf_position):
            continue
        if abs(desired_etf) <= ETF_RESERVE and worst_delta(post_hedge) < ENTRY_LIMIT:
            return quantity
    return 0

def portfolio_delta_bounds(etf_position, options, spot, years, rate, forecast_vol):
    forecast = market_value = float(etf_position)
    for option in options:
        forecast_delta = black_scholes(option.kind, spot, option.strike, years, rate, forecast_vol)[1]
        mid = (option.bid + option.ask) / 2
        market_vol = implied_vol(option.kind, mid, spot, option.strike, years, rate) or forecast_vol
        market_delta = black_scholes(option.kind, spot, option.strike, years, rate, market_vol)[1]
        forecast += option.position * forecast_delta * MULT
        market_value += option.position * market_delta * MULT
    return min(forecast, market_value), max(forecast, market_value)

def portfolio_delta(etf_position, options, spot, years, rate, vol):
    return sum(portfolio_delta_bounds(etf_position, options, spot, years, rate, vol)) / 2

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

def hedge_change(position, bounds):
    limit = ETF_LIMIT if worst_delta(bounds) > 4500 else ETF_RESERVE
    return planned_hedge(position, bounds, limit)

def hedge(api, vol, live):
    _, etf, options, spot, years, rate, _ = state(api, vol)
    position = int(etf.get("position", 0))
    bounds = portfolio_delta_bounds(position, options, spot, years, rate, vol)
    change = hedge_change(position, bounds)
    if change:
        LOG.info("hedge risk_delta=[%.0f, %.0f] change=%d", *bounds, change)
        send(api, "RTM", change, ETF_ORDER, live)

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
                open_options = [x for x in options if x.position]
                for target in open_options:
                    send(api, target.ticker, -target.position, 100, args.live)
                if open_options:
                    hedge(api, vol, args.live)
                time.sleep(args.poll)
                continue
            etf_position = int(etf.get("position", 0))
            bounds = portfolio_delta_bounds(etf_position, options, spot, years, rate, vol)
            LOG.info("tick=%d RTM=%d risk_delta=[%.0f, %.0f]", tick, etf_position, *bounds)
            candidates = []
            for option in options:
                if option.bid <= 0 or option.ask <= 0:
                    continue
                fair, forecast_delta = black_scholes(option.kind, spot, option.strike, years, rate, vol)
                market_vol = implied_vol(option.kind, (option.bid + option.ask) / 2,
                                         spot, option.strike, years, rate) or vol
                market_delta = black_scholes(option.kind, spot, option.strike, years, rate, market_vol)[1]
                fee = OPT_FEE + max(abs(forecast_delta), abs(market_delta)) * MULT * ETF_FEE
                trades = (("BUY", (fair - option.ask) * MULT - fee, option.ask),
                          ("SELL", (option.bid - fair) * MULT - fee, option.bid))
                for action, edge, price in trades:
                    if edge >= args.edge:
                        signed_delta = (forecast_delta + market_delta) / 2 * (1 if action == "BUY" else -1)
                        midpoint = sum(bounds) / 2
                        relief = abs(midpoint) - abs(midpoint + signed_delta * MULT * 50)
                        candidates.append((edge + max(0, relief) / 500, edge, option,
                                           action, fair, price, signed_delta,
                                           forecast_delta, market_delta))
            candidates.sort(reverse=True, key=lambda x: x[0])
            batch, working_options, working_bounds = [], options, bounds
            for candidate in candidates:
                _, edge, option, action, fair, price, signed_delta, forecast_delta, market_delta = candidate
                if batch and signed_delta * batch[0][6] >= 0:
                    continue
                quantity = adaptive_quantity(working_options, option, action,
                    forecast_delta, market_delta, working_bounds, etf_position, args.size)
                new_bounds = shifted_bounds(working_bounds, action, forecast_delta, market_delta, quantity)
                if quantity and (not batch or worst_delta(new_bounds) < worst_delta(working_bounds)):
                    batch.append((edge, option, action, fair, price, quantity,
                                  signed_delta, forecast_delta, market_delta))
                    working_options = projected_options(working_options, option, action, quantity)
                    working_bounds = new_bounds
                if len(batch) == 2:
                    break
            for edge, option, action, fair, price, quantity, *_ in batch:
                LOG.info("signal %s %s x%d fair=%.3f market=%.3f edge=$%.2f", action, option.ticker, quantity, fair, price, edge)
                send(api, option.ticker, quantity if action == "BUY" else -quantity, 100, args.live)
            if batch:
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
