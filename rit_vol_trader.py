#!/usr/bin/env python3
"""Risk-controlled automated trader for the RITCx Volatility Trading Case.

The default mode sends orders. Pass --dry-run for read-only signal testing.
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
from dataclasses import dataclass, replace
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
WEEK_RE = re.compile(r"\bweek\s*(?P<week>[1-4])\b", re.IGNORECASE)


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
    rate_override: float | None
    vol_override: float | None
    request_timeout: float
    per_ticker_limit: int
    option_gross_soft_limit: int
    option_net_soft_limit: int
    etf_soft_limit: int
    risk_stop: float
    unwind_start_tick: int
    unwind_order_size: int
    spot_stress_percent: float


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


def implied_volatility(
    kind: str,
    price: float,
    spot: float,
    strike: float,
    years: float,
    rate: float,
) -> float | None:
    """Invert Black–Scholes with a bounded bisection search."""
    if price <= 0 or years <= 0:
        return None
    intrinsic, _ = black_scholes(kind, spot, strike, 0.0, rate, 0.0)
    if price < intrinsic - 1e-6:
        return None
    low, high = 0.001, 3.0
    low_price, _ = black_scholes(kind, spot, strike, years, rate, low)
    high_price, _ = black_scholes(kind, spot, strike, years, rate, high)
    if not low_price <= price <= high_price:
        return None
    for _ in range(60):
        mid = (low + high) / 2.0
        mid_price, _ = black_scholes(kind, spot, strike, years, rate, mid)
        if mid_price < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


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
        # RIT may publish the next week's current-volatility announcement one
        # tick before that week begins (for example ticker="Week 2" at tick
        # 74). Prefer the explicit news ticker/symbol over the release tick.
        week_label = " ".join(
            str(item.get(key, "")) for key in ("ticker", "symbol")
        )
        week_match = WEEK_RE.search(week_label)
        announced_week = (
            int(week_match.group("week"))
            if week_match
            else case_week(item_tick or tick)
        )
        if announced_week == current_week:
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


def portfolio_delta_bounds(
    stock_position: int,
    options: Iterable[OptionQuote],
    spot: float,
    years: float,
    rate: float,
    forecast_volatility: float,
    spot_stress_percent: float,
) -> tuple[float, float]:
    """Conservative Delta interval across forecast IV, market IV and spot shocks."""
    option_list = list(options)
    market_vols: dict[str, float] = {}
    for option in option_list:
        mid = (option.bid + option.ask) / 2.0
        market_vols[option.ticker] = (
            implied_volatility(
                option.kind, mid, spot, option.strike, years, rate
            )
            or forecast_volatility
        )

    scenarios: list[float] = []
    for shock in (-spot_stress_percent, 0.0, spot_stress_percent):
        stressed_spot = spot * (1.0 + shock)
        for use_market_vol in (False, True):
            exposure = float(stock_position)
            for option in option_list:
                volatility = (
                    market_vols[option.ticker]
                    if use_market_vol
                    else forecast_volatility
                )
                _, option_delta = black_scholes(
                    option.kind,
                    stressed_spot,
                    option.strike,
                    years,
                    rate,
                    volatility,
                )
                exposure += option.position * option_delta * OPTION_MULTIPLIER
            scenarios.append(exposure)
    return min(scenarios), max(scenarios)


def worst_delta(bounds: tuple[float, float]) -> float:
    return max(abs(bounds[0]), abs(bounds[1]))


def hedge_for_bounds(
    stock_position: int, bounds: tuple[float, float], etf_soft_limit: int
) -> int:
    """Center the Delta interval while retaining ETF capacity for future shocks."""
    midpoint = (bounds[0] + bounds[1]) / 2.0
    desired_change = int(round(-midpoint))
    desired_stock = stock_position + desired_change
    desired_stock = max(-etf_soft_limit, min(etf_soft_limit, desired_stock))
    return desired_stock - stock_position


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


def soft_option_capacity(
    options: Iterable[OptionQuote],
    ticker: str,
    action: str,
    config: Config,
) -> int:
    """Capacity under both case limits and conservative strategy limits."""
    option_list = list(options)
    target = next(option for option in option_list if option.ticker == ticker)
    direction = 1 if action == "BUY" else -1
    current_gross = sum(abs(option.position) for option in option_list)
    current_net = sum(option.position for option in option_list)
    capacity = 0
    for quantity in range(1, min(config.option_order_size, OPTION_MAX_ORDER) + 1):
        new_position = target.position + direction * quantity
        new_gross = current_gross - abs(target.position) + abs(new_position)
        new_net = current_net + direction * quantity
        within_hard = new_gross <= OPTION_GROSS_LIMIT and abs(new_net) <= OPTION_NET_LIMIT
        # Always permit a step that reduces an already excessive risk metric.
        within_soft = (
            (abs(new_position) <= config.per_ticker_limit or abs(new_position) < abs(target.position))
            and (new_gross <= config.option_gross_soft_limit or new_gross < current_gross)
            and (abs(new_net) <= config.option_net_soft_limit or abs(new_net) < abs(current_net))
        )
        if within_hard and within_soft:
            capacity = quantity
        else:
            break
    return capacity


def projected_options(
    options: Iterable[OptionQuote], ticker: str, action: str, quantity: int
) -> list[OptionQuote]:
    direction = 1 if action == "BUY" else -1
    return [
        replace(option, position=option.position + direction * quantity)
        if option.ticker == ticker
        else option
        for option in options
    ]


def safest_candidate_quantity(
    options: Iterable[OptionQuote],
    candidate: Candidate,
    stock_position: int,
    spot: float,
    years: float,
    rate: float,
    volatility: float,
    config: Config,
) -> int:
    """Size a trade only if it remains hedgeable inside conservative limits."""
    option_list = list(options)
    capacity = soft_option_capacity(
        option_list, candidate.ticker, candidate.action, config
    )
    safe = 0
    for quantity in range(1, capacity + 1):
        projected = projected_options(
            option_list, candidate.ticker, candidate.action, quantity
        )
        bounds = portfolio_delta_bounds(
            stock_position,
            projected,
            spot,
            years,
            rate,
            volatility,
            config.spot_stress_percent,
        )
        hedge = hedge_for_bounds(stock_position, bounds, config.etf_soft_limit)
        hedged_bounds = (bounds[0] + hedge, bounds[1] + hedge)
        if worst_delta(hedged_bounds) <= config.risk_stop:
            safe = quantity
    return safe


def choose_reduction(
    options: Iterable[OptionQuote],
    stock_position: int,
    spot: float,
    years: float,
    rate: float,
    volatility: float,
    config: Config,
) -> tuple[str, str, int] | None:
    """Choose the position reduction with the best stressed-Delta improvement."""
    option_list = list(options)
    best: tuple[float, str, str, int] | None = None
    for option in option_list:
        if option.position == 0:
            continue
        quantity = min(abs(option.position), config.unwind_order_size)
        action = "SELL" if option.position > 0 else "BUY"
        projected = projected_options(option_list, option.ticker, action, quantity)
        bounds = portfolio_delta_bounds(
            stock_position,
            projected,
            spot,
            years,
            rate,
            volatility,
            config.spot_stress_percent,
        )
        hedge = hedge_for_bounds(stock_position, bounds, config.etf_soft_limit)
        score = worst_delta((bounds[0] + hedge, bounds[1] + hedge))
        proposal = (score, option.ticker, action, quantity)
        if best is None or proposal[0] < best[0]:
            best = proposal
    if best is None:
        return None
    return best[1], best[2], best[3]


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


def read_state(
    api: RitApi,
    config: Config,
    cached_volatility: tuple[int, float] | None,
) -> tuple[int, float, float, str, dict[str, Any], list[OptionQuote]]:
    tick = int(api.case()["tick"])
    underlying, options = parse_market(api.securities())
    spot = (float(underlying.get("bid", 0.0)) + float(underlying.get("ask", 0.0))) / 2.0
    if spot <= 0:
        spot = float(underlying.get("last", 0.0) or 0.0)
    if spot <= 0:
        raise ApiError("RTM has no usable bid/ask or last price")
    volatility = config.vol_override
    source = "override"
    if volatility is None:
        try:
            volatility = volatility_from_news(api.news(), tick)
        except ApiError as exc:
            LOG.warning("news request failed; trying same-week cache: %s", exc)
            volatility = None
        source = "news"
    if volatility is None and cached_volatility is not None:
        cached_week, cached_value = cached_volatility
        if cached_week == case_week(tick):
            volatility = cached_value
            source = "cache"
    if volatility is None:
        raise ApiError("no current-week volatility found; wait for news or pass --vol-percent")
    return tick, spot, volatility, source, underlying, options


def refresh_and_hedge(
    api: RitApi,
    config: Config,
    cached_volatility: tuple[int, float] | None,
) -> None:
    """Refresh actual fills, then hedge immediately when the risk band requires it."""
    if not config.live:
        return
    time.sleep(0.05)
    tick, spot, volatility, _, underlying, options = read_state(
        api, config, cached_volatility
    )
    years = years_remaining(tick)
    rate = effective_rate(underlying, config.rate_override)
    stock_position = int(underlying.get("position", 0) or 0)
    bounds = portfolio_delta_bounds(
        stock_position,
        options,
        spot,
        years,
        rate,
        volatility,
        config.spot_stress_percent,
    )
    if worst_delta(bounds) < config.hedge_trigger:
        return
    change = hedge_for_bounds(stock_position, bounds, config.etf_soft_limit)
    if change:
        LOG.info(
            "IMMEDIATE HEDGE delta_range=[%.1f, %.1f] RTM_change=%d",
            bounds[0],
            bounds[1],
            change,
        )
        submit_chunks(api, "RTM", change, ETF_MAX_ORDER, True)


def run(config: Config) -> None:
    api = RitApi(config)
    last_summary_tick: int | None = None
    last_positions: tuple[int, tuple[tuple[str, int], ...]] | None = None
    last_vol_source: str | None = None
    cached_volatility: tuple[int, float] | None = None
    LOG.warning(
        "startup mode=%s; %s",
        "LIVE" if config.live else "DRY-RUN",
        "REAL MARKET ORDERS ARE ENABLED" if config.live else "orders will only be logged",
    )
    while not STOP:
        try:
            tick, spot, volatility, vol_source, underlying, options = read_state(
                api, config, cached_volatility
            )
            if vol_source in {"news", "override"}:
                cached_volatility = (case_week(tick), volatility)
            if vol_source != last_vol_source:
                LOG.info("volatility source=%s value=%.2f%%", vol_source, volatility * 100.0)
                last_vol_source = vol_source
            if tick >= CASE_TICKS:
                LOG.info("case reached tick %d; no new orders", tick)
                return
            years = years_remaining(tick)
            rate = effective_rate(underlying, config.rate_override)
            stock_position = int(underlying.get("position", 0) or 0)
            option_gross = sum(abs(option.position) for option in options)
            option_net = sum(option.position for option in options)
            delta_now = portfolio_delta(
                stock_position, options, spot, years, rate, volatility
            )
            delta_bounds = portfolio_delta_bounds(
                stock_position,
                options,
                spot,
                years,
                rate,
                volatility,
                config.spot_stress_percent,
            )

            if tick != last_summary_tick:
                LOG.info(
                    "tick=%d week=%d RTM=%.2f vol=%.2f%%(%s) model_delta=%.1f risk_delta=[%.1f,%.1f] stock=%d option_gross=%d option_net=%d mode=%s",
                    tick,
                    case_week(tick),
                    spot,
                    volatility * 100.0,
                    vol_source,
                    delta_now,
                    delta_bounds[0],
                    delta_bounds[1],
                    stock_position,
                    option_gross,
                    option_net,
                    "LIVE" if config.live else "DRY-RUN",
                )
                last_summary_tick = tick

            nonzero_options = tuple(
                sorted((option.ticker, option.position) for option in options if option.position)
            )
            positions = (stock_position, nonzero_options)
            if positions != last_positions:
                detail = ", ".join(f"{ticker}={position}" for ticker, position in nonzero_options)
                LOG.info(
                    "POSITIONS RTM=%d options=[%s] gross=%d net=%d",
                    stock_position,
                    detail or "none",
                    option_gross,
                    option_net,
                )
                last_positions = positions

            hedge = hedge_for_bounds(
                stock_position, delta_bounds, config.etf_soft_limit
            )
            hedged_bounds = (delta_bounds[0] + hedge, delta_bounds[1] + hedge)

            # If even the best ETF hedge inside the soft limit cannot control
            # stressed Delta, reduce options rather than pinning RTM at 50,000.
            if worst_delta(hedged_bounds) > config.risk_stop:
                reduction = choose_reduction(
                    options,
                    stock_position,
                    spot,
                    years,
                    rate,
                    volatility,
                    config,
                )
                if reduction:
                    ticker, action, quantity = reduction
                    LOG.warning(
                        "RISK REDUCTION %s %s x%d; post-hedge stressed Delta would be %.1f",
                        action,
                        ticker,
                        quantity,
                        worst_delta(hedged_bounds),
                    )
                    submit_chunks(
                        api,
                        ticker,
                        quantity if action == "BUY" else -quantity,
                        OPTION_MAX_ORDER,
                        config.live,
                    )
                    refresh_and_hedge(api, config, cached_volatility)
                time.sleep(config.poll_seconds)
                continue

            if worst_delta(delta_bounds) >= config.hedge_trigger:
                change = hedge
                if change:
                    LOG.info(
                        "HEDGE risk_delta=[%.1f, %.1f] RTM_change=%d",
                        delta_bounds[0],
                        delta_bounds[1],
                        change,
                    )
                    submit_chunks(api, "RTM", change, ETF_MAX_ORDER, config.live)
                time.sleep(config.poll_seconds)
                continue

            # The reports show late-case Gamma driving RTM to its hard limit.
            # From this point onward, progressively close option positions.
            if tick >= config.unwind_start_tick:
                reduction = choose_reduction(
                    options,
                    stock_position,
                    spot,
                    years,
                    rate,
                    volatility,
                    config,
                )
                if reduction:
                    ticker, action, quantity = reduction
                    LOG.info("SCHEDULED UNWIND %s %s x%d", action, ticker, quantity)
                    submit_chunks(
                        api,
                        ticker,
                        quantity if action == "BUY" else -quantity,
                        OPTION_MAX_ORDER,
                        config.live,
                    )
                    refresh_and_hedge(api, config, cached_volatility)
                elif stock_position:
                    submit_chunks(
                        api, "RTM", -stock_position, ETF_MAX_ORDER, config.live
                    )
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
                quantity = safest_candidate_quantity(
                    options,
                    best,
                    stock_position,
                    spot,
                    years,
                    rate,
                    volatility,
                    config,
                )
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
                    refresh_and_hedge(api, config, cached_volatility)
                else:
                    LOG.info(
                        "signal %s %s blocked by soft limits or stressed-Delta hedge capacity",
                        best.action,
                        best.ticker,
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
    parser.add_argument("--dry-run", action="store_true", help="disable real orders and only log signals")
    parser.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--base-url", default="http://localhost:9999/v1")
    parser.add_argument("--api-key", default=os.getenv("RIT_API_KEY", "Rotman"))
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--option-order-size", type=positive_int, default=50)
    parser.add_argument("--min-profit", type=float, default=5.0, help="minimum estimated dollars per contract after immediate fees")
    parser.add_argument("--hedge-trigger", type=float, default=2_500.0)
    parser.add_argument("--per-ticker-limit", type=positive_int, default=400)
    parser.add_argument("--option-gross-soft-limit", type=positive_int, default=2_400)
    parser.add_argument("--option-net-soft-limit", type=positive_int, default=900)
    parser.add_argument("--etf-soft-limit", type=positive_int, default=47_000)
    parser.add_argument("--risk-stop", type=float, default=6_000.0)
    parser.add_argument("--unwind-start-tick", type=int, default=275)
    parser.add_argument("--unwind-order-size", type=positive_int, default=50)
    parser.add_argument("--spot-stress-percent", type=float, default=0.25, help="Delta stress move in percent")
    parser.add_argument("--risk-free-percent", type=float, default=None)
    parser.add_argument("--vol-percent", type=float, default=None, help="override news with an annualized percentage, e.g. 20")
    parser.add_argument("--request-timeout", type=float, default=2.0)
    parser.add_argument("--verbose", action="store_true")
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    if args.live and args.dry_run:
        raise SystemExit("--live and --dry-run cannot be used together")
    if not 0 < args.option_order_size <= OPTION_MAX_ORDER:
        raise SystemExit("--option-order-size must be between 1 and 100")
    if not 0 < args.hedge_trigger < DELTA_HARD_LIMIT:
        raise SystemExit("--hedge-trigger must be in (0, 7000)")
    if args.poll_seconds <= 0 or args.request_timeout <= 0:
        raise SystemExit("poll interval and request timeout must be positive")
    if args.vol_percent is not None and args.vol_percent <= 0:
        raise SystemExit("--vol-percent must be positive")
    if not 0 < args.per_ticker_limit <= OPTION_GROSS_LIMIT:
        raise SystemExit("--per-ticker-limit must be in (0, 2500]")
    if not 0 < args.option_gross_soft_limit <= OPTION_GROSS_LIMIT:
        raise SystemExit("--option-gross-soft-limit must be in (0, 2500]")
    if not 0 < args.option_net_soft_limit <= OPTION_NET_LIMIT:
        raise SystemExit("--option-net-soft-limit must be in (0, 1000]")
    if not 0 < args.etf_soft_limit < ETF_POSITION_LIMIT:
        raise SystemExit("--etf-soft-limit must be in (0, 50000)")
    if not args.hedge_trigger < args.risk_stop < DELTA_HARD_LIMIT:
        raise SystemExit("require hedge-trigger < risk-stop < 7000")
    if not 1 <= args.unwind_start_tick < CASE_TICKS:
        raise SystemExit("--unwind-start-tick must be between 1 and 299")
    if not 0 < args.unwind_order_size <= OPTION_MAX_ORDER:
        raise SystemExit("--unwind-order-size must be between 1 and 100")
    if not 0 < args.spot_stress_percent <= 10:
        raise SystemExit("--spot-stress-percent must be in (0, 10]")
    return Config(
        base_url=args.base_url,
        api_key=args.api_key,
        live=not args.dry_run,
        poll_seconds=args.poll_seconds,
        option_order_size=args.option_order_size,
        min_profit_per_contract=args.min_profit,
        hedge_trigger=args.hedge_trigger,
        rate_override=None if args.risk_free_percent is None else args.risk_free_percent / 100.0,
        vol_override=None if args.vol_percent is None else args.vol_percent / 100.0,
        request_timeout=args.request_timeout,
        per_ticker_limit=args.per_ticker_limit,
        option_gross_soft_limit=args.option_gross_soft_limit,
        option_net_soft_limit=args.option_net_soft_limit,
        etf_soft_limit=args.etf_soft_limit,
        risk_stop=args.risk_stop,
        unwind_start_tick=args.unwind_start_tick,
        unwind_order_size=args.unwind_order_size,
        spot_stress_percent=args.spot_stress_percent / 100.0,
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
