#!/usr/bin/env python3
"""Automated RITCx Algorithmic ETF Arbitrage trader.

Supports the local RIT Client REST API and the competition DMA API. The default
mode sends orders; pass --dry-run to inspect decisions without changing state.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests


LOG = logging.getLogger("rit-etf-arb")
STOP = False

CAD = "CAD"
USD = "USD"
BULL = "BULL"
BEAR = "BEAR"
RITC = "RITC"
INSTRUMENTS = (CAD, USD, BULL, BEAR, RITC)

EQUITY_MAX_ORDER = 10_000
FX_MAX_ORDER = 2_500_000
MARKET_FEE_CAD = 0.02
DEFAULT_RITC_LIMIT_MULTIPLIER = 0.5


class ApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    api_mode: str
    base_url: str
    api_key: str
    username: str | None
    password: str | None
    live: bool
    poll_seconds: float
    request_timeout: float
    order_size: int
    min_net_edge: float
    tender_min_net_edge: float
    limit_usage: float
    fallback_gross_limit: int
    fallback_net_limit: int
    fx_tolerance: int
    max_retries: int
    ritc_limit_multiplier: float


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    bid_levels: tuple[tuple[float, int], ...] = ()
    ask_levels: tuple[tuple[float, int], ...] = ()


@dataclass(frozen=True)
class Opportunity:
    direction: str
    gross_edge: float
    net_edge: float
    quantity: int


class RitApi:
    def __init__(self, config: Config) -> None:
        self.base_url = config.base_url.rstrip("/")
        self.timeout = config.request_timeout
        self.max_retries = config.max_retries
        self.session = requests.Session()
        if config.api_mode == "dma":
            if not config.username or not config.password:
                raise ApiError("DMA mode requires --username and --password")
            self.session.auth = (config.username, config.password)
        else:
            self.session.headers.update({"X-API-Key": config.api_key})

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    f"{self.base_url}/{path.lstrip('/')}",
                    timeout=self.timeout,
                    **kwargs,
                )
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise ApiError(f"{method} {path} failed: {exc}") from exc
                time.sleep(min(0.25 * 2**attempt, 2.0))
                continue
            if response.status_code == 429:
                if attempt == self.max_retries:
                    raise ApiError(f"{method} {path}: repeated HTTP 429")
                try:
                    body_wait = float(response.json().get("wait", 0))
                except (ValueError, TypeError, AttributeError):
                    body_wait = 0
                wait = float(response.headers.get("Retry-After", body_wait or 0.5))
                time.sleep(max(wait, 0.05))
                continue
            if response.status_code == 401:
                raise ApiError("authentication failed; check API key or DMA credentials")
            if not response.ok:
                detail = response.text[:300].replace("\n", " ")
                raise ApiError(f"{method} {path}: HTTP {response.status_code}: {detail}")
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise ApiError(f"{method} {path} returned non-JSON data") from exc
        raise ApiError(f"{method} {path} exhausted retries")

    def case(self) -> dict[str, Any]:
        result = self.request("GET", "case")
        if not isinstance(result, dict):
            raise ApiError("/case response is not an object")
        return result

    def book(self, ticker: str) -> Quote:
        result = self.request("GET", "securities/book", params={"ticker": ticker})
        bids = result.get("bids", [])
        asks = result.get("asks", [])
        if not bids or not asks:
            raise ApiError(f"{ticker} has an empty book side")
        bid = bids[0]
        ask = asks[0]
        bid_levels = tuple(
            (float(level["price"]), int(level.get("quantity", 0) or 0))
            for level in bids
            if int(level.get("quantity", 0) or 0) > 0
        )
        ask_levels = tuple(
            (float(level["price"]), int(level.get("quantity", 0) or 0))
            for level in asks
            if int(level.get("quantity", 0) or 0) > 0
        )
        return Quote(
            bid=float(bid["price"]),
            ask=float(ask["price"]),
            bid_size=int(bid.get("quantity", bid.get("quantity_filled", 0)) or 0),
            ask_size=int(ask.get("quantity", ask.get("quantity_filled", 0)) or 0),
            bid_levels=bid_levels,
            ask_levels=ask_levels,
        )

    def positions(self) -> dict[str, int]:
        result = self.request("GET", "securities")
        positions = {item["ticker"]: int(item.get("position", 0) or 0) for item in result}
        for ticker in INSTRUMENTS:
            positions.setdefault(ticker, 0)
        return positions

    def limits(self) -> list[dict[str, Any]]:
        result = self.request("GET", "limits")
        return result if isinstance(result, list) else []

    def tenders(self) -> list[dict[str, Any]]:
        result = self.request("GET", "tenders")
        return result if isinstance(result, list) else []

    def market_order(self, ticker: str, action: str, quantity: int) -> dict[str, Any]:
        result = self.request(
            "POST",
            "orders",
            params={
                "ticker": ticker,
                "type": "MARKET",
                "action": action,
                "quantity": int(quantity),
            },
        )
        return result if isinstance(result, dict) else {}

    def accept_tender(self, tender: dict[str, Any]) -> dict[str, Any]:
        tender_id = int(tender["tender_id"])
        params = None if tender.get("is_fixed_bid", True) else {"price": tender["price"]}
        result = self.request("POST", f"tenders/{tender_id}", params=params)
        return result if isinstance(result, dict) else {}

    def decline_tender(self, tender_id: int) -> dict[str, Any]:
        result = self.request("DELETE", f"tenders/{int(tender_id)}")
        return result if isinstance(result, dict) else {}


def weighted_exposure(
    positions: dict[str, int], ritc_multiplier: float = DEFAULT_RITC_LIMIT_MULTIPLIER
) -> tuple[float, float]:
    components = (
        positions[BULL],
        positions[BEAR],
        ritc_multiplier * positions[RITC],
    )
    return sum(abs(value) for value in components), sum(components)


def projected_positions(
    positions: dict[str, int], direction: str, quantity: int
) -> dict[str, int]:
    projected = dict(positions)
    if direction == "BASKET_RICH":
        projected[BULL] -= quantity
        projected[BEAR] -= quantity
        projected[RITC] += quantity
    elif direction == "ETF_RICH":
        projected[BULL] += quantity
        projected[BEAR] += quantity
        projected[RITC] -= quantity
    else:
        raise ValueError(f"unknown direction: {direction}")
    return projected


def discovered_limits(
    limit_rows: Iterable[dict[str, Any]], config: Config
) -> tuple[int, int]:
    gross_limits: list[int] = []
    net_limits: list[int] = []
    for row in limit_rows:
        gross = int(float(row.get("gross_limit", 0) or 0))
        net = int(float(row.get("net_limit", 0) or 0))
        if gross > 0:
            gross_limits.append(gross)
        if net > 0:
            net_limits.append(net)
    gross_limit = min(gross_limits) if gross_limits else config.fallback_gross_limit
    net_limit = min(net_limits) if net_limits else config.fallback_net_limit
    return int(gross_limit * config.limit_usage), int(net_limit * config.limit_usage)


def within_limits(
    positions: dict[str, int], limit_rows: Iterable[dict[str, Any]], config: Config
) -> bool:
    gross, net = weighted_exposure(positions, config.ritc_limit_multiplier)
    gross_limit, net_limit = discovered_limits(limit_rows, config)
    return gross <= gross_limit and abs(net) <= net_limit


def market_fee_per_package(usd_cad: float) -> float:
    return MARKET_FEE_CAD + MARKET_FEE_CAD + MARKET_FEE_CAD * usd_cad


def sweep_notional(quote: Quote, side: str, quantity: int) -> float | None:
    """CAD or native-currency notional to sweep quantity through one book side."""
    if quantity <= 0:
        return 0.0
    levels = quote.bid_levels if side == "bid" else quote.ask_levels
    if not levels:
        price = quote.bid if side == "bid" else quote.ask
        return price * quantity
    remaining = quantity
    total = 0.0
    for price, available in levels:
        fill = min(remaining, available)
        total += fill * price
        remaining -= fill
        if remaining == 0:
            return total
    return None


def find_opportunities(
    bull: Quote,
    bear: Quote,
    ritc: Quote,
    usd: Quote,
    max_quantity: int,
    min_net_edge: float,
) -> list[Opportunity]:
    # Buying USD uses the FX ask; selling USD uses the FX bid.
    basket_rich_gross = bull.bid + bear.bid - ritc.ask * usd.ask
    basket_rich_net = basket_rich_gross - market_fee_per_package(usd.ask)
    basket_rich_qty = min(
        max_quantity,
        bull.bid_size or max_quantity,
        bear.bid_size or max_quantity,
        ritc.ask_size or max_quantity,
    )

    etf_rich_gross = ritc.bid * usd.bid - bull.ask - bear.ask
    etf_rich_net = etf_rich_gross - market_fee_per_package(usd.bid)
    etf_rich_qty = min(
        max_quantity,
        ritc.bid_size or max_quantity,
        bull.ask_size or max_quantity,
        bear.ask_size or max_quantity,
    )

    opportunities = []
    if basket_rich_net >= min_net_edge and basket_rich_qty > 0:
        opportunities.append(
            Opportunity("BASKET_RICH", basket_rich_gross, basket_rich_net, basket_rich_qty)
        )
    if etf_rich_net >= min_net_edge and etf_rich_qty > 0:
        opportunities.append(
            Opportunity("ETF_RICH", etf_rich_gross, etf_rich_net, etf_rich_qty)
        )
    return sorted(opportunities, key=lambda item: item.net_edge, reverse=True)


def tender_opportunity(
    tender: dict[str, Any],
    bull: Quote,
    bear: Quote,
    usd: Quote,
) -> tuple[str, float] | None:
    if str(tender.get("ticker", RITC)) != RITC:
        return None
    raw_quantity = int(tender.get("quantity", 0) or 0)
    quantity = abs(raw_quantity)
    if quantity == 0:
        return None
    action = str(tender.get("action", "")).upper()
    if action not in {"BUY", "SELL"}:
        action = "BUY" if raw_quantity > 0 else "SELL"
    price = float(tender.get("price", 0) or 0)
    if price <= 0:
        return None
    # RIT's action is the trader's inventory action: BUY adds RITC, SELL removes it.
    if action == "BUY":
        bull_proceeds = sweep_notional(bull, "bid", quantity)
        bear_proceeds = sweep_notional(bear, "bid", quantity)
        usd_cost = sweep_notional(usd, "ask", int(round(price * quantity)))
        if bull_proceeds is None or bear_proceeds is None or usd_cost is None:
            return None
        net_total = bull_proceeds + bear_proceeds - usd_cost
        net_total -= 2 * MARKET_FEE_CAD * quantity
        return "BASKET_RICH", net_total / quantity
    if action == "SELL":
        usd_proceeds = sweep_notional(usd, "bid", int(round(price * quantity)))
        bull_cost = sweep_notional(bull, "ask", quantity)
        bear_cost = sweep_notional(bear, "ask", quantity)
        if usd_proceeds is None or bull_cost is None or bear_cost is None:
            return None
        net_total = usd_proceeds - bull_cost - bear_cost
        net_total -= 2 * MARKET_FEE_CAD * quantity
        return "ETF_RICH", net_total / quantity
    return None


def submit_market_chunks(
    api: RitApi, ticker: str, action: str, quantity: int, live: bool
) -> None:
    max_order = FX_MAX_ORDER if ticker in (USD, CAD) else EQUITY_MAX_ORDER
    remaining = abs(int(quantity))
    while remaining:
        child = min(remaining, max_order)
        if live:
            result = api.market_order(ticker, action, child)
            LOG.info(
                "ORDER %s %d %s accepted id=%s",
                action,
                child,
                ticker,
                result.get("order_id", result.get("id", "?")),
            )
        else:
            LOG.info("DRY-RUN would %s %d %s", action, child, ticker)
        remaining -= child


def hedge_usd(api: RitApi, config: Config) -> None:
    if not config.live:
        return
    positions = api.positions()
    usd_position = positions[USD]
    if abs(usd_position) <= config.fx_tolerance:
        return
    action = "SELL" if usd_position > 0 else "BUY"
    LOG.info("FX HEDGE %s %d USD", action, abs(usd_position))
    submit_market_chunks(api, USD, action, abs(usd_position), True)


def execute_package(
    api: RitApi, direction: str, quantity: int, config: Config
) -> None:
    # Execute the ETF first, then both basket legs, and finally neutralize USD.
    if direction == "BASKET_RICH":
        legs = ((RITC, "BUY"), (BULL, "SELL"), (BEAR, "SELL"))
    else:
        legs = ((RITC, "SELL"), (BULL, "BUY"), (BEAR, "BUY"))
    for ticker, action in legs:
        submit_market_chunks(api, ticker, action, quantity, config.live)
    hedge_usd(api, config)


def process_tenders(
    api: RitApi,
    config: Config,
    quotes: dict[str, Quote],
    positions: dict[str, int],
    limit_rows: list[dict[str, Any]],
    seen: set[int],
) -> bool:
    for tender in api.tenders():
        tender_id = int(tender["tender_id"])
        if tender_id in seen:
            continue
        if not tender.get("is_fixed_bid", True):
            LOG.info("DECLINE competitive tender id=%d; no blind bidding", tender_id)
            if config.live:
                api.decline_tender(tender_id)
            else:
                LOG.info("DRY-RUN would decline tender id=%d", tender_id)
            seen.add(tender_id)
            continue
        evaluation = tender_opportunity(
            tender, quotes[BULL], quotes[BEAR], quotes[USD]
        )
        if evaluation is None:
            LOG.info("DECLINE unsupported tender id=%d", tender_id)
            if config.live:
                api.decline_tender(tender_id)
            else:
                LOG.info("DRY-RUN would decline tender id=%d", tender_id)
            seen.add(tender_id)
            continue
        direction, net_edge = evaluation
        quantity = abs(int(tender.get("quantity", 0) or 0))
        projected = projected_positions(positions, direction, quantity)
        profitable = net_edge >= config.tender_min_net_edge
        safe = quantity > 0 and within_limits(projected, limit_rows, config)
        LOG.info(
            "TENDER id=%d action=%s qty=%d net_edge=%.4f profitable=%s safe=%s",
            tender_id,
            tender.get("action"),
            quantity,
            net_edge,
            profitable,
            safe,
        )
        if not (profitable and safe):
            reason = "insufficient edge" if not profitable else "projected limit risk"
            LOG.info("TENDER DECLINED id=%d reason=%s", tender_id, reason)
            if config.live:
                api.decline_tender(tender_id)
            else:
                LOG.info("DRY-RUN would decline tender id=%d", tender_id)
            seen.add(tender_id)
            continue
        if config.live:
            api.accept_tender(tender)
            LOG.info("TENDER ACCEPTED id=%d", tender_id)
        else:
            LOG.info("DRY-RUN would accept tender id=%d", tender_id)
        seen.add(tender_id)
        # The accepted RITC inventory is hedged with the two CAD stocks.
        if direction == "BASKET_RICH":
            submit_market_chunks(api, BULL, "SELL", quantity, config.live)
            submit_market_chunks(api, BEAR, "SELL", quantity, config.live)
        else:
            submit_market_chunks(api, BULL, "BUY", quantity, config.live)
            submit_market_chunks(api, BEAR, "BUY", quantity, config.live)
        hedge_usd(api, config)
        return True
    return False


def run(config: Config) -> None:
    api = RitApi(config)
    seen_tenders: set[int] = set()
    LOG.warning(
        "startup api=%s mode=%s; %s",
        config.api_mode.upper(),
        "LIVE" if config.live else "DRY-RUN",
        "REAL ORDERS AND TENDER ACCEPTANCE ENABLED"
        if config.live
        else "orders and tenders will only be logged",
    )
    while not STOP:
        try:
            case = api.case()
            tick = int(case.get("tick", 0))
            status = str(case.get("status", "")).upper()
            if status and status != "ACTIVE":
                LOG.info("case status=%s tick=%d; waiting", status, tick)
                time.sleep(max(config.poll_seconds, 0.5))
                continue

            quotes = {ticker: api.book(ticker) for ticker in (BULL, BEAR, RITC, USD)}
            positions = api.positions()
            limit_rows = api.limits()
            gross, net = weighted_exposure(
                positions, config.ritc_limit_multiplier
            )

            if process_tenders(
                api, config, quotes, positions, limit_rows, seen_tenders
            ):
                time.sleep(config.poll_seconds)
                continue

            opportunities = find_opportunities(
                quotes[BULL],
                quotes[BEAR],
                quotes[RITC],
                quotes[USD],
                config.order_size,
                config.min_net_edge,
            )
            chosen: Opportunity | None = None
            for opportunity in opportunities:
                projected = projected_positions(
                    positions, opportunity.direction, opportunity.quantity
                )
                if within_limits(projected, limit_rows, config):
                    chosen = opportunity
                    break

            edge1 = (
                quotes[BULL].bid
                + quotes[BEAR].bid
                - quotes[RITC].ask * quotes[USD].ask
                - market_fee_per_package(quotes[USD].ask)
            )
            edge2 = (
                quotes[RITC].bid * quotes[USD].bid
                - quotes[BULL].ask
                - quotes[BEAR].ask
                - market_fee_per_package(quotes[USD].bid)
            )
            LOG.info(
                "tick=%d net_edges basket_rich=%.4f etf_rich=%.4f gross=%d net=%d positions[BULL=%d BEAR=%d RITC=%d USD=%d]",
                tick,
                edge1,
                edge2,
                gross,
                net,
                positions[BULL],
                positions[BEAR],
                positions[RITC],
                positions[USD],
            )
            if chosen:
                LOG.info(
                    "ARB %s qty=%d gross_edge=%.4f net_edge=%.4f",
                    chosen.direction,
                    chosen.quantity,
                    chosen.gross_edge,
                    chosen.net_edge,
                )
                execute_package(api, chosen.direction, chosen.quantity, config)
            elif opportunities:
                LOG.info("arbitrage blocked by projected weighted limits")
            elif config.live and abs(positions[USD]) > config.fx_tolerance:
                hedge_usd(api, config)
            time.sleep(config.poll_seconds)
        except ApiError as exc:
            LOG.warning("%s", exc)
            time.sleep(max(config.poll_seconds, 0.5))


def positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-mode", choices=("rest", "dma"), default="rest")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=os.getenv("RIT_API_KEY", "JOE"))
    parser.add_argument("--username", default=os.getenv("RIT_DMA_USERNAME"))
    parser.add_argument("--password", default=os.getenv("RIT_DMA_PASSWORD"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--request-timeout", type=float, default=2.0)
    parser.add_argument("--order-size", type=positive_int, default=1_000)
    parser.add_argument("--min-net-edge", type=float, default=0.05)
    parser.add_argument("--tender-min-net-edge", type=float, default=0.05)
    parser.add_argument("--limit-usage", type=float, default=0.85)
    parser.add_argument("--fallback-gross-limit", type=positive_int, default=300_000)
    parser.add_argument("--fallback-net-limit", type=positive_int, default=25_000)
    parser.add_argument("--fx-tolerance", type=positive_int, default=100)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument(
        "--ritc-limit-multiplier",
        type=float,
        default=DEFAULT_RITC_LIMIT_MULTIPLIER,
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    if not 0 < args.poll_seconds or not 0 < args.request_timeout:
        raise SystemExit("poll interval and timeout must be positive")
    if not 0 < args.order_size <= EQUITY_MAX_ORDER:
        raise SystemExit("--order-size must be in (0, 10000]")
    if not 0 < args.limit_usage < 1:
        raise SystemExit("--limit-usage must be between 0 and 1")
    if args.min_net_edge < 0 or args.tender_min_net_edge < 0:
        raise SystemExit("edge thresholds cannot be negative")
    if args.max_retries < 0:
        raise SystemExit("--max-retries cannot be negative")
    if args.ritc_limit_multiplier <= 0:
        raise SystemExit("--ritc-limit-multiplier must be positive")
    base_url = args.base_url
    if base_url is None:
        base_url = (
            "http://flserver.rotman.utoronto.ca:16635/v1"
            if args.api_mode == "dma"
            else "http://localhost:9999/v1"
        )
    return Config(
        api_mode=args.api_mode,
        base_url=base_url,
        api_key=args.api_key,
        username=args.username,
        password=args.password,
        live=not args.dry_run,
        poll_seconds=args.poll_seconds,
        request_timeout=args.request_timeout,
        order_size=args.order_size,
        min_net_edge=args.min_net_edge,
        tender_min_net_edge=args.tender_min_net_edge,
        limit_usage=args.limit_usage,
        fallback_gross_limit=args.fallback_gross_limit,
        fallback_net_limit=args.fallback_net_limit,
        fx_tolerance=args.fx_tolerance,
        max_retries=args.max_retries,
        ritc_limit_multiplier=args.ritc_limit_multiplier,
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
    except (ApiError, KeyboardInterrupt) as exc:
        if isinstance(exc, ApiError):
            LOG.error("%s", exc)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
