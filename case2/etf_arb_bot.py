"""RITCx bot: full-book tender VWAP and ETF/basket arbitrage.

Creation/redemption is not automated: the case makes converters manual-only.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Iterable
import requests

API_URL = os.getenv("RIT_API_URL", "http://localhost:9999/v1")
API_KEY = os.getenv("RIT_API_KEY", "PUT_YOUR_API_KEY_HERE")
ETF, BULL, BEAR, USD = "RITC", "BULL", "BEAR", "USD"
MAX_ORDER_SIZE, BOOK_LEVELS = 10_000, 100
MARKET_FEE, POLL_SECONDS = 0.02, 0.10

# Edges are CAD per ETF unit, after estimated market-order fees.
TENDER_SAMPLE_MIN_EDGE_CAD = float(os.getenv("RIT_TENDER_SAMPLE_EDGE", "0.05"))
TENDER_FULL_MIN_EDGE_CAD = float(os.getenv("RIT_TENDER_FULL_EDGE", "-0.01"))
TENDER_VWAP_SAMPLE = 10_000
TENDER_UNWIND_CHUNK = 5_000
TENDER_MIN_CHUNK = 1_000
TENDER_RECHECK_SECONDS = 0.25
TENDER_MAX_PATIENCE_SECONDS = 12.0
TENDER_MIN_EXIT_EDGE_USD = 0.005
TENDER_PENDING_TICKS = 10
ARB_OPEN_EDGE_CAD = float(os.getenv("RIT_ARB_OPEN_EDGE", "0.22"))
ARB_MIN_ROUND_TRIP_CAD = float(os.getenv("RIT_ARB_ROUND_TRIP", "0.08"))
ARB_QUANTITY = int(os.getenv("RIT_ARB_QUANTITY", "1000"))
MAX_REASONABLE_ARB_EDGE_CAD = 2.00
STOP_OPENING_AT_TICK = int(os.getenv("RIT_STOP_OPENING_AT_TICK", "270"))
FORCE_CLOSE_AT_TICK = int(os.getenv("RIT_FORCE_CLOSE_AT_TICK", "285"))
HEDGE_USD = os.getenv("RIT_HEDGE_USD", "0") == "1"
ENABLE_BASKET_ARB = os.getenv("RIT_ENABLE_BASKET_ARB", "1") == "1"
MIN_VISIBLE_DEPTH_RATIO = 0.20
UNSEEN_DEPTH_PENALTY = 0.03
ETF_LIMIT_WEIGHT = 2.0


@dataclass(frozen=True)
class Level:
    price: float
    quantity: int


@dataclass(frozen=True)
class Book:
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]


@dataclass
class ArbLot:
    direction: int  # +1 long ETF/short basket; -1 short ETF/long basket
    quantity: int
    entry_edge: float


@dataclass
class PendingTender:
    tender_id: int
    before_position: int
    expected_side: str
    submitted_tick: int
    quantity: int
    price: float


def api(session: requests.Session, method: str, path: str, **kwargs):
    response = session.request(method, f"{API_URL}{path}", timeout=1.5, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None


def remaining(level: dict) -> int:
    return max(0, int(level.get("quantity", 0)) - int(level.get("quantity_filled", 0)))


def parse_levels(raw: Iterable[dict]) -> tuple[Level, ...]:
    return tuple(Level(float(x["price"]), remaining(x)) for x in raw if remaining(x))


def get_book(session: requests.Session, ticker: str) -> Book:
    raw = api(session, "GET", "/securities/book", params={"ticker": ticker, "limit": BOOK_LEVELS})
    return Book(parse_levels(raw["bids"]), parse_levels(raw["asks"]))


def security_by_ticker(securities: Iterable[dict], ticker: str) -> dict:
    """Select by ticker instead of trusting API list order or query filtering."""
    for security in securities:
        if security.get("ticker") == ticker:
            return security
    raise KeyError(f"Security {ticker!r} not returned by API")


def vwap(levels: Iterable[Level], quantity: int) -> float | None:
    """Executable VWAP, or None if displayed depth cannot fill the quantity."""
    left, notional = quantity, 0.0
    for level in levels:
        take = min(left, level.quantity)
        notional += take * level.price
        left -= take
        if left == 0:
            return notional / quantity
    return None


def conservative_vwap(levels: Iterable[Level], quantity: int, action: str):
    """Estimate a large fill when at least 20% of its depth is displayed."""
    levels = tuple(levels)
    visible = sum(level.quantity for level in levels)
    coverage = min(1.0, visible / quantity) if quantity else 1.0
    if not levels or coverage < MIN_VISIBLE_DEPTH_RATIO:
        return None, coverage
    if visible >= quantity:
        return vwap(levels, quantity), coverage
    notional = sum(level.price * level.quantity for level in levels)
    worst = levels[-1].price
    extrapolated = worst - UNSEEN_DEPTH_PENALTY if action == "SELL" else worst + UNSEEN_DEPTH_PENALTY
    notional += (quantity - visible) * extrapolated
    return notional / quantity, coverage


def profitable_unwind_slice(
    levels: Iterable[Level],
    remaining_quantity: int,
    tender_price: float,
    unwind_action: str,
) -> tuple[int, float, float] | None:
    """Largest 1k-5k slice whose displayed VWAP remains profitable."""
    levels = tuple(levels)
    largest = min(TENDER_UNWIND_CHUNK, remaining_quantity)
    sizes = list(range((largest // 1000) * 1000, TENDER_MIN_CHUNK - 1, -1000))
    if remaining_quantity < TENDER_MIN_CHUNK:
        sizes = [remaining_quantity]
    for size in sizes:
        price = vwap(levels, size)
        if price is None:
            continue
        edge = (
            price - tender_price - MARKET_FEE
            if unwind_action == "SELL"
            else tender_price - price - MARKET_FEE
        )
        if edge >= TENDER_MIN_EXIT_EDGE_USD:
            return size, price, edge
    return None


def usd_cad(session: requests.Session) -> float | None:
    raw = api(session, "GET", "/securities", params={"ticker": USD})
    usd = security_by_ticker(raw, USD)
    bid, ask = float(usd["bid"]), float(usd["ask"])
    fx = (bid + ask) / 2
    # This case constrains USD roughly to 0.7-1.3 CAD. Zero/stale opening
    # quotes create fake ~$25 arbitrage edges, so wait for a valid market.
    return fx if 0.65 <= bid <= ask <= 1.35 else None


def basket_edges(etf: Book, bull: Book, bear: Book, quantity: int, fx: float):
    """Return (ETF-rich edge, ETF-cheap edge) in CAD per unit."""
    eb, ea = vwap(etf.bids, quantity), vwap(etf.asks, quantity)
    ub, ua = vwap(bull.bids, quantity), vwap(bull.asks, quantity)
    bb, ba = vwap(bear.bids, quantity), vwap(bear.asks, quantity)
    fee = MARKET_FEE * fx + 2 * MARKET_FEE
    rich = None if None in (eb, ua, ba) else eb * fx - ua - ba - fee
    cheap = None if None in (ea, ub, bb) else ub + bb - ea * fx - fee
    return rich, cheap


def order(
    session: requests.Session,
    ticker: str,
    action: str,
    quantity: int,
    pause_between_chunks: float = 0.0,
    max_chunk: int | None = None,
) -> None:
    while quantity > 0:
        max_size = 2_500_000 if ticker in (USD, "CAD") else MAX_ORDER_SIZE
        if max_chunk is not None:
            max_size = min(max_size, max_chunk)
        size = min(quantity, max_size)
        api(session, "POST", "/orders", params={"ticker": ticker, "type": "MARKET", "quantity": size, "action": action})
        quantity -= size
        if quantity > 0 and pause_between_chunks:
            time.sleep(pause_between_chunks)


def hedge_fx(session: requests.Session, etf_action: str, quantity: int, price: float) -> None:
    if HEDGE_USD:
        usd_amount = max(1, round(quantity * price))
        order(session, USD, "SELL" if etf_action == "SELL" else "BUY", usd_amount)


def execute_basket(session, etf_action, stock_action, quantity, etf_price) -> None:
    order(session, ETF, etf_action, quantity)
    order(session, BULL, stock_action, quantity)
    order(session, BEAR, stock_action, quantity)
    hedge_fx(session, etf_action, quantity, etf_price)


def position_usage(session: requests.Session) -> tuple[int, int]:
    raw = api(session, "GET", "/securities")
    pos = {x["ticker"]: int(x.get("position", 0)) for x in raw}
    weighted = [pos.get(BULL, 0), pos.get(BEAR, 0), ETF_LIMIT_WEIGHT * pos.get(ETF, 0)]
    return sum(map(abs, weighted)), sum(weighted)


def security_position(session: requests.Session, ticker: str) -> int:
    raw = api(session, "GET", "/securities", params={"ticker": ticker})
    security = security_by_ticker(raw, ticker)
    return int(security.get("position", 0))


def within_limits(
    session: requests.Session, extra_gross: float, extra_net: float = 0.0
) -> bool:
    """Check projected gross and net usage, including the proposed trade."""
    gross, net = position_usage(session)
    limits = api(session, "GET", "/limits")
    gl = min((int(x["gross_limit"]) for x in limits if x.get("gross_limit") is not None), default=10**18)
    nl = min((int(x["net_limit"]) for x in limits if x.get("net_limit") is not None), default=10**18)
    return gross + extra_gross <= gl and abs(net + extra_net) <= nl


def tender_edge_cad(tender: dict, book: Book, fx: float):
    """Evaluate the unwind that corresponds to the API's fixed tender side."""
    quantity, price = int(tender["quantity"]), float(tender["price"])
    side = str(tender.get("action", "")).upper()
    if side not in ("BUY", "SELL"):
        return None, None, None, 0.0
    levels = book.bids if side == "BUY" else book.asks
    unwind_action = "SELL" if side == "BUY" else "BUY"

    def edge_at(executable_price: float | None) -> float | None:
        if executable_price is None:
            return None
        raw = (
            executable_price - price
            if side == "BUY"
            else price - executable_price
        )
        return (raw - MARKET_FEE) * fx

    sample = min(quantity, TENDER_VWAP_SAMPLE)
    sample_vwap = vwap(levels, sample)
    sample_edge = edge_at(sample_vwap)
    full_vwap, coverage = conservative_vwap(levels, quantity, unwind_action)
    full_edge = edge_at(full_vwap)
    return sample_edge, full_edge, side, coverage


def tender_can_wait(tender: dict, tick: int) -> bool:
    """True only when the API supplies a future absolute expiry tick."""
    try:
        expires = int(tender["expires"])
    except (KeyError, TypeError, ValueError):
        return False
    return tick < expires - 1


def handle_tender(
    session: requests.Session, tender: dict, fx: float, tick: int
) -> tuple[str, PendingTender | None, tuple[float | None, float | None, float]]:
    tid, quantity = int(tender["tender_id"]), int(tender["quantity"])
    if tender.get("is_fixed_bid") is False:
        print(f"TENDER reject #{tid}: competitive auction tender is unsupported")
        api(session, "DELETE", f"/tenders/{tid}")
        return "rejected", None, (None, None, 0.0)
    sample_edge, full_edge, expected_side, coverage = tender_edge_cad(
        tender, get_book(session, ETF), fx
    )
    profitable = (
        sample_edge is not None
        and sample_edge >= TENDER_SAMPLE_MIN_EDGE_CAD
        and full_edge is not None
        and full_edge >= TENDER_FULL_MIN_EDGE_CAD
    )
    signed = ETF_LIMIT_WEIGHT * quantity * (1 if expected_side == "BUY" else -1)
    limits_ok = expected_side is not None and within_limits(
        session, abs(signed), signed
    )
    if not profitable or not limits_ok:
        if tender_can_wait(tender, tick):
            return "waiting", None, (sample_edge, full_edge, coverage)
        print(
            f"TENDER reject #{tid}: side={expected_side}, sample_edge={sample_edge}, "
            f"full_edge={full_edge}, visible={coverage:.0%}, limits_ok={limits_ok}"
        )
        api(session, "DELETE", f"/tenders/{tid}")
        return "rejected", None, (sample_edge, full_edge, coverage)
    print(
        f"TENDER accept #{tid}: side={expected_side}, "
        f"quantity={quantity:,}, sample_edge={sample_edge:.3f}, "
        f"full_edge={full_edge:.3f}, visible={coverage:.0%}"
    )
    before = security_position(session, ETF)
    api(session, "POST", f"/tenders/{tid}")
    print(f"TENDER #{tid}: submitted; tracking settlement for {TENDER_PENDING_TICKS} ticks")
    return (
        "accepted",
        PendingTender(tid, before, expected_side, tick, quantity, float(tender["price"])),
        (sample_edge, full_edge, coverage),
    )


def adaptive_tender_unwind(
    session: requests.Session,
    pending: PendingTender,
    unwind_action: str,
    quantity: int,
) -> None:
    """Price-aware liquidation with a hard deadline for inventory safety."""
    left = quantity
    protected_quantity = 0
    started = time.monotonic()
    while left > 0:
        book = get_book(session, ETF)
        levels = book.bids if unwind_action == "SELL" else book.asks
        choice = profitable_unwind_slice(
            levels, left, pending.price, unwind_action
        )
        if choice is not None:
            size, price, edge = choice
            order(session, ETF, unwind_action, size)
            left -= size
            protected_quantity += size
            print(
                f"TENDER #{pending.tender_id}: {unwind_action} {size:,} "
                f"@ est {price:.4f}, edge {edge:.4f}, left {left:,}"
            )
            time.sleep(TENDER_RECHECK_SECONDS)
            continue

        elapsed = time.monotonic() - started
        if elapsed < TENDER_MAX_PATIENCE_SECONDS:
            time.sleep(TENDER_RECHECK_SECONDS)
            continue

        print(
            f"TENDER #{pending.tender_id}: {elapsed:.1f}s deadline; "
            f"market-unwinding remaining {left:,}"
        )
        order(
            session,
            ETF,
            unwind_action,
            left,
            pause_between_chunks=TENDER_RECHECK_SECONDS,
            max_chunk=TENDER_UNWIND_CHUNK,
        )
        left = 0

    print(
        f"TENDER #{pending.tender_id}: unwind complete; "
        f"price-protected quantity {protected_quantity:,}/{quantity:,}"
    )


def manage_pending_tender(
    session: requests.Session, pending: PendingTender, tick: int
) -> bool:
    """Return True while pending; hedge verified fills and return False when done."""
    after = security_position(session, ETF)
    delta = after - pending.before_position
    if delta == 0:
        elapsed = tick - pending.submitted_tick
        if elapsed < TENDER_PENDING_TICKS:
            return True
        print(
            f"TENDER #{pending.tender_id}: no fill after {elapsed} ticks; "
            "stopping settlement tracking"
        )
        return False

    actual_side = "BUY" if delta > 0 else "SELL"
    unwind = "SELL" if delta > 0 else "BUY"
    if actual_side != pending.expected_side:
        print(
            f"TENDER #{pending.tender_id} WARNING: expected {pending.expected_side}, "
            f"actual {actual_side}; "
            "flattening verified position immediately"
        )
    print(
        f"TENDER #{pending.tender_id}: settled delta={delta:+,}; "
        f"unwinding {unwind} {abs(delta):,}"
    )
    adaptive_tender_unwind(session, pending, unwind, abs(delta))
    return False
    # Tender and unwind create opposite USD cash flows, so no separate FX leg.


def manage_arbitrage(
    session: requests.Session,
    lots: list[ArbLot],
    tick: int,
    fx: float,
    scan_log_ticks: set[int],
) -> None:
    etf, bull, bear = get_book(session, ETF), get_book(session, BULL), get_book(session, BEAR)
    if lots:
        lot = lots[0]
        rich, cheap = basket_edges(etf, bull, bear, lot.quantity, fx)
        closing_edge = rich if lot.direction == 1 else cheap
        expected_round_trip = None if closing_edge is None else lot.entry_edge + closing_edge
        should_close = (
            expected_round_trip is not None
            and expected_round_trip >= ARB_MIN_ROUND_TRIP_CAD
        )
        if tick >= FORCE_CLOSE_AT_TICK or should_close:
            if lot.direction == 1:
                px = vwap(etf.bids, lot.quantity)
                if px is None:
                    return
                execute_basket(session, "SELL", "BUY", lot.quantity, px)
            else:
                px = vwap(etf.asks, lot.quantity)
                if px is None:
                    return
                execute_basket(session, "BUY", "SELL", lot.quantity, px)
            print(f"ARB close {lot.quantity:,}; expected round trip={expected_round_trip}")
            lots.pop(0)
        return

    basket_gross = (2 + ETF_LIMIT_WEIGHT) * ARB_QUANTITY
    if tick >= STOP_OPENING_AT_TICK or not within_limits(session, basket_gross):
        return
    rich, cheap = basket_edges(etf, bull, bear, ARB_QUANTITY, fx)
    if tick % 10 == 0 and tick not in scan_log_ticks:
        print(
            f"ARB scan tick={tick}: rich={rich}, cheap={cheap}, "
            f"open_threshold={ARB_OPEN_EDGE_CAD:.3f}"
        )
        scan_log_ticks.add(tick)
    if any(
        edge is not None and abs(edge) > MAX_REASONABLE_ARB_EDGE_CAD
        for edge in (rich, cheap)
    ):
        print(f"ARB skip invalid market snapshot: rich={rich}, cheap={cheap}")
        return
    if rich is not None and rich >= ARB_OPEN_EDGE_CAD:
        execute_basket(session, "SELL", "BUY", ARB_QUANTITY, vwap(etf.bids, ARB_QUANTITY))
        lots.append(ArbLot(-1, ARB_QUANTITY, rich))
        print(f"ARB open ETF rich: edge {rich:.3f} CAD")
    elif cheap is not None and cheap >= ARB_OPEN_EDGE_CAD:
        execute_basket(session, "BUY", "SELL", ARB_QUANTITY, vwap(etf.asks, ARB_QUANTITY))
        lots.append(ArbLot(1, ARB_QUANTITY, cheap))
        print(f"ARB open ETF cheap: edge {cheap:.3f} CAD")


def run() -> None:
    if API_KEY == "PUT_YOUR_API_KEY_HERE":
        raise SystemExit("Set RIT_API_KEY before starting the bot.")
    with requests.Session() as session:
        session.headers.update({"X-API-Key": API_KEY})
        seen, lots, pending = set(), [], None
        wait_log_tick: dict[int, int] = {}
        arb_scan_log_ticks: set[int] = set()
        last_status = None
        print(f"ETF arbitrage bot connected to {API_URL}")
        print(
            "Strategies: tender=ON, "
            f"ETF-rich/ETF-cheap={'ON' if ENABLE_BASKET_ARB else 'OFF'}, "
            f"USD-hedge={'ON' if HEDGE_USD else 'OFF'}"
        )
        while True:
            try:
                case = api(session, "GET", "/case")
                if case["status"] != last_status:
                    print(f"Case status: {case['status']}")
                    last_status = case["status"]
                if case["status"] == "STOPPED":
                    # Stay alive across practice resets. Clear IDs and internal
                    # lots so a new heat starts with clean strategy state.
                    seen.clear()
                    wait_log_tick.clear()
                    arb_scan_log_ticks.clear()
                    lots.clear()
                    pending = None
                    time.sleep(0.25)
                    continue
                if case["status"] != "ACTIVE":
                    time.sleep(POLL_SECONDS)
                    continue
                fx = usd_cad(session)
                if fx is None:
                    time.sleep(POLL_SECONDS)
                    continue
                tick = int(case["tick"])
                if pending is not None:
                    if not manage_pending_tender(session, pending, tick):
                        pending = None
                    # Do not trade RITC for another strategy while measuring the
                    # tender's position delta.
                    time.sleep(POLL_SECONDS)
                    continue

                for tender in api(session, "GET", "/tenders"):
                    tid = int(tender["tender_id"])
                    if tid not in seen:
                        outcome, pending, metrics = handle_tender(
                            session, tender, fx, tick
                        )
                        if outcome != "waiting":
                            seen.add(tid)
                            wait_log_tick.pop(tid, None)
                        elif wait_log_tick.get(tid) != tick:
                            sample_edge, full_edge, coverage = metrics
                            print(
                                f"TENDER wait #{tid}: sample_edge={sample_edge}, "
                                f"full_edge={full_edge}, visible={coverage:.0%}, "
                                f"expires={tender.get('expires')}"
                            )
                            wait_log_tick[tid] = tick
                        if pending is not None:
                            break
                if pending is None and ENABLE_BASKET_ARB:
                    manage_arbitrage(session, lots, tick, fx, arb_scan_log_ticks)
            except (requests.RequestException, KeyError, ValueError) as exc:
                print(f"Loop error: {exc}")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
