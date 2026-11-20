import unittest
from unittest.mock import patch

from etf_arb_bot import (
    Book,
    Level,
    Runner,
    basket_edges,
    conservative_vwap,
    manage_runner,
    profitable_unwind_slice,
    runner_move,
    security_by_ticker,
    tender_can_wait,
    tender_edge_cad,
    vwap,
    within_limits,
)


class StrategyTest(unittest.TestCase):
    def test_security_lookup_does_not_trust_first_item(self):
        securities = [
            {"ticker": "CAD", "position": 0},
            {"ticker": "RITC", "position": 90_000},
        ]
        self.assertEqual(security_by_ticker(securities, "RITC")["position"], 90_000)

    def test_adaptive_unwind_uses_profitable_vwap_slice(self):
        levels = (Level(25.10, 2_000), Level(25.04, 3_000))
        choice = profitable_unwind_slice(levels, 5_000, 25.00, "SELL")
        self.assertEqual(choice[0], 5_000)
        self.assertAlmostEqual(choice[1], 25.064)

    def test_adaptive_unwind_waits_when_no_slice_is_profitable(self):
        levels = (Level(25.01, 5_000),)
        self.assertIsNone(profitable_unwind_slice(levels, 5_000, 25.00, "SELL"))

    def test_vwap_multiple_levels(self):
        levels = (Level(20.00, 1000), Level(19.90, 2000))
        self.assertAlmostEqual(vwap(levels, 2000), 19.95)

    def test_vwap_rejects_insufficient_depth(self):
        self.assertIsNone(vwap((Level(20.00, 999),), 1000))

    def test_buy_tender_uses_bid_side(self):
        book = Book((Level(20.00, 500), Level(19.80, 500)), (Level(20.10, 1000),))
        edge, full_edge, exit_price, coverage = tender_edge_cad(
            {"action": "BUY", "price": 19.50, "quantity": 1000}, book, 1.25
        )
        self.assertEqual(exit_price, "BUY")
        self.assertAlmostEqual(edge, 0.475)
        self.assertAlmostEqual(full_edge, 0.475)
        self.assertEqual(coverage, 1.0)

    def test_sell_tender_uses_ask_side(self):
        book = Book((Level(20.00, 1000),), (Level(20.10, 500), Level(20.20, 500)))
        edge, full_edge, exit_price, coverage = tender_edge_cad(
            {"action": "SELL", "price": 20.50, "quantity": 1000}, book, 1.25
        )
        self.assertEqual(exit_price, "SELL")
        self.assertAlmostEqual(edge, 0.4125)
        self.assertAlmostEqual(full_edge, 0.4125)
        self.assertEqual(coverage, 1.0)

    def test_large_tender_detects_bad_full_book_vwap(self):
        book = Book(
            (Level(25.10, 10_000), Level(24.90, 10_000)),
            (Level(25.20, 20_000),),
        )
        sample_edge, full_edge, side, coverage = tender_edge_cad(
            {"action": "BUY", "price": 25.00, "quantity": 20_000}, book, 1.0
        )
        self.assertEqual(side, "BUY")
        self.assertAlmostEqual(sample_edge, 0.08)
        self.assertAlmostEqual(full_edge, -0.02)
        self.assertEqual(coverage, 1.0)

    def test_invalid_tender_action_is_rejected(self):
        book = Book((Level(25.10, 10_000),), (Level(25.20, 10_000),))
        self.assertEqual(
            tender_edge_cad(
                {"action": "UNKNOWN", "price": 25.00, "quantity": 10_000},
                book,
                1.0,
            ),
            (None, None, None, 0.0),
        )

    def test_tender_waits_until_one_tick_before_expiry(self):
        self.assertTrue(tender_can_wait({"expires": 120}, 118))
        self.assertFalse(tender_can_wait({"expires": 120}, 119))
        self.assertFalse(tender_can_wait({}, 100))

    def test_long_runner_uses_bid_and_short_runner_uses_ask(self):
        book = Book((Level(25.12, 8_000),), (Level(25.18, 8_000),))
        long_move, long_action = runner_move(
            book, Runner("BUY", 8_000, 25.00, 1, 100)
        )
        short_move, short_action = runner_move(
            book, Runner("SELL", 8_000, 25.30, 2, 100)
        )
        self.assertEqual(long_action, "SELL")
        self.assertAlmostEqual(long_move, 0.10)
        self.assertEqual(short_action, "BUY")
        self.assertAlmostEqual(short_move, 0.10)

    @patch("etf_arb_bot.order")
    @patch("etf_arb_bot.get_book")
    def test_runner_closes_after_max_hold(self, mock_book, mock_order):
        mock_book.return_value = Book(
            (Level(25.00, 3_000),), (Level(25.02, 3_000),)
        )
        runner = Runner("BUY", 3_000, 25.00, 7, 100)
        self.assertIsNone(manage_runner(object(), runner, 120))
        mock_order.assert_called_once()

    @patch("etf_arb_bot.api")
    @patch("etf_arb_bot.position_usage", return_value=(100_000, 40_000))
    def test_limits_include_proposed_net_position(self, _usage, mock_api):
        mock_api.return_value = [{"gross_limit": 500_000, "net_limit": 100_000}]
        self.assertTrue(within_limits(object(), 50_000, 50_000))
        self.assertFalse(within_limits(object(), 50_000, 70_000))

    def test_large_tender_extrapolates_shallow_book(self):
        price, coverage = conservative_vwap(
            (Level(25.00, 10_000), Level(24.90, 10_000)), 80_000, "SELL"
        )
        self.assertEqual(coverage, 0.25)
        self.assertAlmostEqual(price, 24.89)

    def test_detects_rich_etf(self):
        etf = Book((Level(20.50, 1000),), (Level(20.60, 1000),))
        bull = Book((Level(9.90, 1000),), (Level(10.00, 1000),))
        bear = Book((Level(14.90, 1000),), (Level(15.00, 1000),))
        rich, cheap = basket_edges(etf, bull, bear, 1000, 1.25)
        self.assertGreater(rich, 0)
        self.assertLess(cheap, 0)

    def test_detects_cheap_etf(self):
        etf = Book((Level(19.40, 1000),), (Level(19.50, 1000),))
        bull = Book((Level(10.00, 1000),), (Level(10.10, 1000),))
        bear = Book((Level(15.00, 1000),), (Level(15.10, 1000),))
        rich, cheap = basket_edges(etf, bull, bear, 1000, 1.25)
        self.assertLess(rich, 0)
        self.assertGreater(cheap, 0)


if __name__ == "__main__":
    unittest.main()
