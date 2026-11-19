import unittest
from rit_vol_trader import Option, black_scholes, news_vol, option_quantity, portfolio_delta, week

class TraderTests(unittest.TestCase):
    def test_week_boundaries(self):
        self.assertEqual([week(x) for x in (0, 75, 150, 225, 300)], [1, 2, 3, 4, 4])

    def test_put_call_parity(self):
        call, call_delta = black_scholes("C", 50, 50, 1 / 12, 0, .2)
        put, put_delta = black_scholes("P", 50, 50, 1 / 12, 0, .2)
        self.assertAlmostEqual(call, put)
        self.assertAlmostEqual(call_delta - put_delta, 1)

    def test_news_prefers_current_week(self):
        news = [{"tick": 74, "body": "Week 2 volatility is 28%"},
                {"tick": 1, "body": "Week 1 volatility is 20%"}]
        self.assertEqual(news_vol(news, 76), .28)

    def test_default_option_order_is_fifty(self):
        option = Option("RTM50C", 50, "C", 1, 1.1, 0)
        self.assertEqual(option_quantity([option], option, "BUY"), 50)

    def test_quantity_respects_ticker_limit(self):
        option = Option("RTM50C", 50, "C", 1, 1.1, 390)
        self.assertEqual(option_quantity([option], option, "BUY"), 10)

    def test_delta_uses_contract_multiplier(self):
        option = Option("RTM50C", 50, "C", 1, 1.1, 10)
        self.assertGreater(portfolio_delta(0, [option], 50, 1 / 12, 0, .2), 500)

if __name__ == "__main__":
    unittest.main()
