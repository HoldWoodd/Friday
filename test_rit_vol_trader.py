import math
import unittest

from rit_vol_trader import (
    ApiError,
    Config,
    Candidate,
    OptionQuote,
    black_scholes,
    case_week,
    delta_safe_quantity,
    find_candidates,
    hedge_quantity,
    option_capacity,
    portfolio_delta,
    read_state,
    volatility_from_news,
    years_remaining,
)


class ModelTests(unittest.TestCase):
    def test_case_time_is_one_twelfth_year(self):
        self.assertAlmostEqual(years_remaining(0), 1 / 12)
        self.assertAlmostEqual(years_remaining(150), 1 / 24)
        self.assertEqual(years_remaining(300), 0)

    def test_week_boundaries(self):
        self.assertEqual(case_week(1), 1)
        self.assertEqual(case_week(75), 1)
        self.assertEqual(case_week(76), 2)
        self.assertEqual(case_week(300), 4)

    def test_put_call_parity_and_delta(self):
        call, call_delta = black_scholes("C", 50, 50, 1 / 12, 0, 0.20)
        put, put_delta = black_scholes("P", 50, 50, 1 / 12, 0, 0.20)
        self.assertAlmostEqual(call, put, places=10)
        self.assertAlmostEqual(call_delta - put_delta, 1.0, places=10)

    def test_portfolio_delta_has_100_share_multiplier(self):
        options = [OptionQuote("RTM50C", 50, "C", 1, 1.02, 10)]
        _, delta = black_scholes("C", 50, 50, 1 / 12, 0, 0.20)
        result = portfolio_delta(100, options, 50, 1 / 12, 0, 0.20)
        self.assertAlmostEqual(result, 100 + 10 * delta * 100)


class NewsTests(unittest.TestCase):
    def test_current_week_value_excludes_next_week_range(self):
        news = [
            {"tick": 1, "body": "Realized volatility for this week will be 20%"},
            {"tick": 37, "body": "Volatility for next week will be between 27-30%"},
        ]
        self.assertEqual(volatility_from_news(news, 50), 0.20)

    def test_latest_current_week_release(self):
        news = [
            {"tick": 1, "body": "Realized volatility for this week will be 20%"},
            {"tick": 76, "body": "Realized volatility for this week will be 29%"},
        ]
        self.assertEqual(volatility_from_news(news, 100), 0.29)

    def test_news_value_can_be_retained_by_caller_for_same_week(self):
        class FakeApi:
            def case(self):
                return {"tick": 159}

            def securities(self):
                return [
                    {"ticker": "RTM", "bid": 50, "ask": 50.02, "position": 0},
                    {"ticker": "RTM50C", "bid": 1, "ask": 1.02, "position": 0},
                ]

            def news(self):
                return []

        config = Config(
            base_url="http://localhost",
            api_key="test",
            live=False,
            poll_seconds=1,
            option_order_size=1,
            min_profit_per_contract=1,
            hedge_trigger=3000,
            hedge_target=500,
            delta_buffer=1000,
            rate_override=0,
            vol_override=None,
            request_timeout=1,
        )
        _, _, volatility, source, _, _ = read_state(FakeApi(), config, (3, 0.28))
        self.assertEqual(volatility, 0.28)
        self.assertEqual(source, "cache")

        with self.assertRaises(ApiError):
            read_state(FakeApi(), config, (2, 0.20))


class RiskTests(unittest.TestCase):
    def test_option_capacity_obeys_gross_and_net(self):
        options = [
            OptionQuote("RTM48C", 48, "C", 1, 1.02, 900),
            OptionQuote("RTM48P", 48, "P", 1, 1.02, -100),
        ]
        self.assertEqual(option_capacity(options, "BUY"), 200)
        self.assertEqual(option_capacity(options, "SELL"), 1500)

    def test_delta_quantity_keeps_buffer(self):
        self.assertEqual(delta_safe_quantity(0, 0.5, "BUY", 1000), 100)
        self.assertEqual(delta_safe_quantity(5000, 0.5, "BUY", 1000), 20)

    def test_hedge_respects_stock_limit(self):
        self.assertEqual(hedge_quantity(0, 6000, 0), -6000)
        self.assertEqual(hedge_quantity(49_000, -10_000, 0), 1000)

    def test_signal_uses_executable_side_and_fees(self):
        option = OptionQuote("RTM50C", 50, "C", 0.10, 0.12, 0)
        candidates = find_candidates([option], 50, 1 / 12, 0, 0.20, 1.0)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].action, "BUY")
        self.assertGreater(candidates[0].edge_per_contract, 1.0)


if __name__ == "__main__":
    unittest.main()
