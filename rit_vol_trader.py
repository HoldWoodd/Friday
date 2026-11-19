#!/usr/bin/env python3
"""First-pass automated trader for the RITCx Volatility Trading Case.

The default mode is deliberately read-only. Pass --live to allow market orders.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests


LOG = logging.getLogger("rit-vol-trader")
STOP = False

CASE_TICKS = 300
TRADING_DAYS_PER_YEAR = 240
CASE_DAYS = 20
OPTION_MULTIPLIER = 100
OPTION_FEE_PER_CONTRACT = 2.00
ETF_FEE_PER_SHARE = 0.02
OPTION_GROSS_LIMIT = 2_500
OPTION_NET_LIMIT = 1_000
ETF_POSITION_LIMIT = 50_000
OPTION_MAX_ORDER = 100
ETF_MAX_ORDER = 10_000
DELTA_HARD_LIMIT = 7_000.0
OPTION_RE = re.compile(r"^RTM(?P<strike>\d+(?:\.\d+)?)(?P<kind>[CP])$")
PERCENT_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*%")


class ApiError(RuntimeError):
    """Raised when RIT returns an unsuccessful or malformed response."""


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    live: bool
    poll_seconds: float
    option_order_size: int
    min_profit_per_contract: float
    hedge_trigger: float
    hedge_target: float
    delta_buffer: float
    rate_override: float | None
    vol_override: float | None
    request_timeout: float


@dataclass(frozen=True)
class OptionQuote:
    ticker: str
    strike: float
    kind: str
    bid: float
    ask: float
    position: int


@dataclass(frozen=True)
class Candidate:
    ticker: str
    action: str
    edge_per_contract: float
    option_delta: float
    fair_value: float
    market_price: float


class RitApi:
    def __init__(self, config: Config) -> None:
        self.base_url = config.base_url.rstrip("/")
        self.timeout = config.request_timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": config.api_key})

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.session.request(
                method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise ApiError(f"{method} {path} failed: {exc}") from exc
        if not response.ok:
            detail = response.text[:300].replace("\n", " ")
            raise ApiError(f"{method} {path}: HTTP {response.status_code}: {detail}")
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(f"{method} {path} returned non-JSON data") from exc

    def case(self) -> dict[str, Any]:
        result = self._request("GET", "/case")
        if not isinstance(result, dict) or "tick" not in result:
            raise ApiError("/case response has no tick")
        return result

    def securities(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/securities")
        if not isinstance(result, list):
            raise ApiError("/securities response is not a list")
        return result

    def news(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/news")
        if not isinstance(result, list):
            raise ApiError("/news response is not a list")
        return result

    def market_order(self, ticker: str, action: str, quantity: int) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/orders",
            params={
                "ticker": ticker,
                "type": "MARKET",
                "quantity": int(quantity),
                "action": action,
            },
        )
        if not isinstance(result, dict):
            raise ApiError("/orders response is not an object")
        return result


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def black_scholes(
    kind: str, spot: float, strike: float, years: float, rate: float, volatility: float
) -> tuple[float, float]:
    """Return (price, delta) for a non-dividend-paying European option."""
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if years <= 0 or volatility <= 0:
        if kind == "C":
            return max(spot - strike, 0.0), 1.0 if spot > strike else 0.0
        return max(strike - spot, 0.0), -1.0 if spot < strike else 0.0

    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * volatility**2) * years) / (
        volatility * root_t
    )
    d2 = d1 - volatility * root_t
    discounted_strike = strike * math.exp(-rate * years)
    if kind == "C":
        return spot * normal_cdf(d1) - discounted_strike * normal_cdf(d2), normal_cdf(d1)
    if kind == "P":
        return discounted_strike * normal_cdf(-d2) - spot * normal_cdf(-d1), normal_cdf(d1) - 1.0
    raise ValueError(f"unknown option kind: {kind}")


def years_remaining(tick: int) -> float:
    # The 300-second case represents 20/240 = 1/12 trading years.
    remaining_fraction = max(CASE_TICKS - tick, 0) / CASE_TICKS
    return remaining_fraction * CASE_DAYS / TRADING_DAYS_PER_YEAR


def case_week(tick: int) -> int:
    # Boundary ticks 75/150/225 belong to the week that is just ending.
    return min(4, max(1, (max(tick, 1) - 1) // 75 + 1))


def _news_text(item: dict[str, Any]) -> str:
    return " ".join(str(item.get(key, "")) for key in ("headline", "body", "text"))


def volatility_from_news(news: Iterable[dict[str, Any]], tick: int) -> float | None:
    """Extract the latest current-week realized-volatility forecast.

    A next-week range is intentionally not treated as the current week's value.
    """
    current_week = case_week(tick)
    matches: list[tuple[int, float]] = []
    for item in news:
        text = _news_text(item).lower()
        if "volatil" not in text or "next week" in text or "between" in text:
            continue
        percentages = PERCENT_RE.findall(text)
        if not percentages:
            continue
        item_tick = int(item.get("tick", item.get("period", 0)) or 0)
        if case_week(item_tick or tick) == current_week:
            matches.append((item_tick, float(percentages[-1]) / 100.0))
    if not matches:
        return None
    return max(matches, key=lambda pair: pair[0])[1]


def parse_market(
    securities: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], list[OptionQuote]]:
    underlying: dict[str, Any] | None = None
    options: list[OptionQuote] = []
    for security in securities:
        ticker = str(security.get("ticker", ""))
        if ticker == "RTM":
            underlying = security
            continue
        match = OPTION_RE.fullmatch(ticker)
        if not match:
            continue
        options.append(
            OptionQuote(
                ticker=ticker,
                strike=float(match.group("strike")),
                kind=match.group("kind"),
                bid=float(security.get("bid", 0.0) or 0.0),
                ask=float(security.get("ask", 0.0) or 0.0),
                position=int(security.get("position", 0) or 0),
            )
        )
    if underlying is None:
        raise ApiError("RTM underlying was not present in /securities")
    if not options:
        raise ApiError("no RTM options were present in /securities")
    return underlying, options


def portfolio_delta(
    stock_position: int,
    options: Iterable[OptionQuote],
    spot: float,
    years: float,
    rate: float,
    volatility: float,
) -> float:
    exposure = float(stock_position)
    for option in options:
        _, option_delta = black_scholes(
            option.kind, spot, option.strike, years, rate, volatility
        )
        exposure += option.position * option_delta * OPTION_MULTIPLIER
    return exposure


def find_candidates(
    options: Iterable[OptionQuote],
    spot: float,
    years: float,
    rate: float,
    volatility: float,
    min_profit: float,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for option in options:
        if option.bid <= 0 or option.ask <= 0 or option.ask < option.bid:
            continue
        fair, option_delta = black_scholes(
            option.kind, spot, option.strike, years, rate, volatility
        )
        estimated_hedge_fee = abs(option_delta) * OPTION_MULTIPLIER * ETF_FEE_PER_SHARE
        buy_edge = (fair - option.ask) * OPTION_MULTIPLIER
        buy_edge -= OPTION_FEE_PER_CONTRACT + estimated_hedge_fee
        sell_edge = (option.bid - fair) * OPTION_MULTIPLIER
        sell_edge -= OPTION_FEE_PER_CONTRACT + estimated_hedge_fee
        if buy_edge >= min_profit:
            candidates.append(
                Candidate(option.ticker, "BUY", buy_edge, option_delta, fair, option.ask)
            )
        if sell_edge >= min_profit:
            candidates.append(
                Candidate(option.ticker, "SELL", sell_edge, option_delta, fair, option.bid)
            )
    return sorted(candidates, key=lambda candidate: candidate.edge_per_contract, reverse=True)


def option_capacity(options: Iterable[OptionQuote], action: str) -> int:
    positions = [option.position for option in options]
    gross_remaining = OPTION_GROSS_LIMIT - sum(abs(position) for position in positions)
    net = sum(positions)
    net_remaining = OPTION_NET_LIMIT - net if action == "BUY" else OPTION_NET_LIMIT + net
    return max(0, min(gross_remaining, net_remaining))


def delta_safe_quantity(
    current_delta: float, option_delta: float, action: str, buffer: float
) -> int:
    signed_change = option_delta * OPTION_MULTIPLIER * (1 if action == "BUY" else -1)
    if abs(signed_change) < 1e-12:
        return OPTION_MAX_ORDER
    limit = DELTA_HARD_LIMIT - buffer
    safe = 0
    for quantity in range(1, OPTION_MAX_ORDER + 1):
        if abs(current_delta + quantity * signed_change) <= limit:
            safe = quantity
        else:
            break
    return safe


def hedge_quantity(stock_position: int, delta_exposure: float, target: float) -> int:
    desired_change = int(round(target - delta_exposure))
    desired_stock = stock_position + desired_change
    desired_stock = max(-ETF_POSITION_LIMIT, min(ETF_POSITION_LIMIT, desired_stock))
    return desired_stock - stock_position


def submit_chunks(
    api: RitApi,
    ticker: str,
    signed_quantity: int,
    max_order: int,
    live: bool,
) -> None:
    remaining = abs(int(signed_quantity))
    action = "BUY" if signed_quantity > 0 else "SELL"
    while remaining:
        quantity = min(remaining, max_order)
        if live:
            result = api.market_order(ticker, action, quantity)
            LOG.info("ORDER %s %d %s accepted: id=%s", action, quantity, ticker, result.get("order_id", result.get("id", "?")))
        else:
            LOG.info("DRY-RUN would %s %d %s", action, quantity, ticker)
        remaining -= quantity


def effective_rate(underlying: dict[str, Any], override: float | None) -> float:
    if override is not None:
        return override
    raw = float(underlying.get("interest_rate", 0.0) or 0.0)
    # RIT normally reports a decimal; tolerate a percentage-form value.
    return raw / 100.0 if abs(raw) > 1.0 else raw


def read_state(api: RitApi, config: Config) -> tuple[int, float, float, dict[str, Any], list[OptionQuote]]:
    tick = int(api.case()["tick"])
    underlying, options = parse_market(api.securities())
    spot = (float(underlying.get("bid", 0.0)) + float(underlying.get("ask", 0.0))) / 2.0
    if spot <= 0:
        spot = float(underlying.get("last", 0.0) or 0.0)
    if spot <= 0:
        raise ApiError("RTM has no usable bid/ask or last price")
    volatility = config.vol_override
    if volatility is None:
        volatility = volatility_from_news(api.news(), tick)
    if volatility is None:
        raise ApiError("no current-week volatility found; wait for news or pass --vol-percent")
    return tick, spot, volatility, underlying, options


def run(config: Config) -> None:
    api = RitApi(config)
    last_summary_tick: int | None = None
    while not STOP:
        try:
            tick, spot, volatility, underlying, options = read_state(api, config)
            if tick >= CASE_TICKS:
                LOG.info("case reached tick %d; no new orders", tick)
                return
            years = years_remaining(tick)
            rate = effective_rate(underlying, config.rate_override)
            stock_position = int(underlying.get("position", 0) or 0)
            delta_now = portfolio_delta(
                stock_position, options, spot, years, rate, volatility
            )

            if tick != last_summary_tick:
                LOG.info(
                    "tick=%d week=%d RTM=%.2f vol=%.2f%% delta=%.1f stock=%d mode=%s",
                    tick,
                    case_week(tick),
                    spot,
                    volatility * 100.0,
                    delta_now,
                    stock_position,
                    "LIVE" if config.live else "DRY-RUN",
                )
                last_summary_tick = tick

            if abs(delta_now) >= config.hedge_trigger:
                target = math.copysign(config.hedge_target, delta_now) if config.hedge_target else 0.0
                change = hedge_quantity(stock_position, delta_now, target)
                if change:
                    submit_chunks(api, "RTM", change, ETF_MAX_ORDER, config.live)
                time.sleep(config.poll_seconds)
                continue

            candidates = find_candidates(
                options,
                spot,
                years,
                rate,
                volatility,
                config.min_profit_per_contract,
            )
            if candidates:
                best = candidates[0]
                capacity = option_capacity(options, best.action)
                safe_quantity = delta_safe_quantity(
                    delta_now, best.option_delta, best.action, config.delta_buffer
                )
                quantity = min(config.option_order_size, OPTION_MAX_ORDER, capacity, safe_quantity)
                if quantity > 0:
                    signed_quantity = quantity if best.action == "BUY" else -quantity
                    LOG.info(
                        "signal %s %s x%d fair=%.4f executable=%.4f net_edge=$%.2f/contract",
                        best.action,
                        best.ticker,
                        quantity,
                        best.fair_value,
                        best.market_price,
                        best.edge_per_contract,
                    )
                    submit_chunks(
                        api, best.ticker, signed_quantity, OPTION_MAX_ORDER, config.live
                    )
            time.sleep(config.poll_seconds)
        except ApiError as exc:
            LOG.warning("%s", exc)
            time.sleep(max(config.poll_seconds, 0.5))


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="enable real market orders")
    parser.add_argument("--base-url", default="http://localhost:9999/v1")
    parser.add_argument("--api-key", default=os.getenv("RIT_API_KEY", "Rotman"))
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--option-order-size", type=positive_int, default=25)
    parser.add_argument("--min-profit", type=float, default=3.0, help="minimum estimated dollars per contract after immediate fees")
    parser.add_argument("--hedge-trigger", type=float, default=3_500.0)
    parser.add_argument("--hedge-target", type=float, default=500.0)
    parser.add_argument("--delta-buffer", type=float, default=1_000.0)
    parser.add_argument("--risk-free-percent", type=float, default=None)
    parser.add_argument("--vol-percent", type=float, default=None, help="override news with an annualized percentage, e.g. 20")
    parser.add_argument("--request-timeout", type=float, default=2.0)
    parser.add_argument("--verbose", action="store_true")
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    if not 0 < args.option_order_size <= OPTION_MAX_ORDER:
        raise SystemExit("--option-order-size must be between 1 and 100")
    if not 0 <= args.hedge_target < args.hedge_trigger < DELTA_HARD_LIMIT:
        raise SystemExit("require 0 <= hedge-target < hedge-trigger < 7000")
    if not 0 <= args.delta_buffer < DELTA_HARD_LIMIT:
        raise SystemExit("--delta-buffer must be in [0, 7000)")
    if args.poll_seconds <= 0 or args.request_timeout <= 0:
        raise SystemExit("poll interval and request timeout must be positive")
    if args.vol_percent is not None and args.vol_percent <= 0:
        raise SystemExit("--vol-percent must be positive")
    return Config(
        base_url=args.base_url,
        api_key=args.api_key,
        live=args.live,
        poll_seconds=args.poll_seconds,
        option_order_size=args.option_order_size,
        min_profit_per_contract=args.min_profit,
        hedge_trigger=args.hedge_trigger,
        hedge_target=args.hedge_target,
        delta_buffer=args.delta_buffer,
        rate_override=None if args.risk_free_percent is None else args.risk_free_percent / 100.0,
        vol_override=None if args.vol_percent is None else args.vol_percent / 100.0,
        request_timeout=args.request_timeout,
    )


def stop_handler(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    try:
        run(config_from_args(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
