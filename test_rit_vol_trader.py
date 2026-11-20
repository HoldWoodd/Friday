import unittest
from rit_vol_trader import (Option, adaptive_quantity, black_scholes, hedge_change,
    implied_vol, news_vol, option_quantity, portfolio_delta, shifted_bounds, week)

class TraderTests(unittest.TestCase):
    def test_week_boundaries(self):
        self.assertEqual([week(x) for x in (1, 75, 76, 150, 151, 225, 226, 300)],
                         [1, 1, 2, 2, 3, 3, 4, 4])

    def test_put_call_parity(self):
        call, call_delta = black_scholes("C", 50, 50, 1 / 12, 0, .2)
        put, put_delta = black_scholes("P", 50, 50, 1 / 12, 0, .2)
        self.assertAlmostEqual(call, put)
        self.assertAlmostEqual(call_delta - put_delta, 1)

    def test_news_prefers_current_week(self):
        news = [{"tick": 74, "body": "Week 2 volatility is 28%"},
                {"tick": 1, "body": "Week 1 volatility is 20%"}]
        self.assertEqual(news_vol(news, 76), .28)

    def test_news_ignores_unlabeled_next_week_range(self):
        news = [{"tick": 75, "body": "Next week volatility will be between 27-30%"},
                {"tick": 1, "body": "Current volatility is 20%"}]
        self.assertEqual(news_vol(news, 75), .20)

    def test_default_option_order_is_one_hundred(self):
        option = Option("RTM50C", 50, "C", 1, 1.1, 0)
        self.assertEqual(option_quantity([option], option, "BUY"), 100)

    def test_quantity_respects_ticker_limit(self):
        option = Option("RTM50C", 50, "C", 1, 1.1, 390)
        self.assertEqual(option_quantity([option], option, "BUY"), 10)

    def test_delta_uses_contract_multiplier(self):
        option = Option("RTM50C", 50, "C", 1, 1.1, 10)
        self.assertGreater(portfolio_delta(0, [option], 50, 1 / 12, 0, .2), 500)

    def test_hedge_uses_a_band_instead_of_zero(self):
        self.assertEqual(hedge_change(0, (4000, 4000)), -3000)
        self.assertEqual(hedge_change(0, (2500, 2500)), 0)

    def test_adaptive_size_preserves_etf_reserve(self):
        option = Option("RTM50P", 50, "P", 1, 1.1, 0)
        self.assertEqual(adaptive_quantity([option], option, "BUY", -.5, -.5, (0, 0), 0), 100)
        self.assertEqual(adaptive_quantity([option], option, "BUY", -.5, -.5, (-5000, -5000), 44000), 0)

    def test_implied_vol_recovers_market_vol(self):
        price = black_scholes("C", 50, 50, 1 / 12, 0, .25)[0]
        self.assertAlmostEqual(implied_vol("C", price, 50, 50, 1 / 12, 0), .25, places=4)

    def test_projected_bounds_use_both_delta_models(self):
        self.assertEqual(shifted_bounds((0, 0), "BUY", .4, .6, 100), (4000, 6000))

if __name__ == "__main__":
    unittest.main()
