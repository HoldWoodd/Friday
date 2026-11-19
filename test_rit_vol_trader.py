import unittest

from rit_vol_trader import (
    ApiError,
    Config,
    Candidate,
    OptionQuote,
    black_scholes,
    build_parser,
    case_week,
    config_from_args,
    delta_safe_quantity,
    find_candidates,
    hedge_for_bounds,
    hedge_quantity,
    implied_volatility,
    option_capacity,
    portfolio_delta,
    portfolio_delta_bounds,
    read_state,
    soft_option_capacity,
    volatility_from_news,
    years_remaining,
)


def make_config(**overrides):
    values = dict(
        base_url="http://localhost",
        api_key="test",
        live=False,
        poll_seconds=1,
        option_order_size=5,
        min_profit_per_contract=10,
        hedge_trigger=1000,
        hedge_target=0,
        delta_buffer=3000,
        rate_override=0,
        vol_override=None,
        request_timeout=1,
        per_ticker_limit=100,
        option_gross_soft_limit=1500,
        option_net_soft_limit=500,
        etf_soft_limit=40_000,
        risk_stop=3500,
        unwind_start_tick=225,
        unwind_order_size=25,
        spot_stress_percent=0.01,
    )
    values.update(overrides)
    return Config(**values)


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

    def test_implied_volatility_recovers_input(self):
        price, _ = black_scholes("C", 50, 50, 1 / 12, 0, 0.20)
        result = implied_volatility("C", price, 50, 50, 1 / 12, 0)
        self.assertAlmostEqual(result, 0.20, places=6)


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

    def test_explicit_week_ticker_wins_at_release_boundary(self):
        news = [
            {
                "tick": 1,
                "ticker": "Week 1",
                "body": "Current annualized realized volatility is 32%",
            },
            {
                "tick": 74,
                "ticker": "Week 2",
                "body": "The realized volatility of RTM this week will be 23%",
            },
        ]
        self.assertEqual(volatility_from_news(news, 96), 0.23)

    def test_future_week_announcement_is_not_used_early(self):
        news = [
            {
                "tick": 74,
                "ticker": "Week 2",
                "body": "The realized volatility of RTM this week will be 23%",
            }
        ]
        self.assertIsNone(volatility_from_news(news, 74))

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

        config = make_config()
        _, _, volatility, source, _, _ = read_state(FakeApi(), config, (3, 0.28))
        self.assertEqual(volatility, 0.28)
        self.assertEqual(source, "cache")

        with self.assertRaises(ApiError):
            read_state(FakeApi(), config, (2, 0.20))


class RiskTests(unittest.TestCase):
    def test_safer_defaults_from_two_case_review(self):
        config = config_from_args(build_parser().parse_args([]))
        self.assertEqual(config.poll_seconds, 1.0)
        self.assertEqual(config.option_order_size, 5)
        self.assertEqual(config.hedge_trigger, 1000)
        self.assertEqual(config.etf_soft_limit, 40_000)
        self.assertEqual(config.option_net_soft_limit, 500)
        self.assertEqual(config.unwind_start_tick, 225)

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

    def test_soft_capacity_prevents_ticker_concentration(self):
        options = [OptionQuote("RTM50C", 50, "C", 1, 1.02, 98)]
        self.assertEqual(
            soft_option_capacity(options, "RTM50C", "BUY", make_config()), 2
        )

    def test_soft_capacity_allows_reducing_excess(self):
        options = [OptionQuote("RTM50C", 50, "C", 1, 1.02, 120)]
        self.assertEqual(
            soft_option_capacity(options, "RTM50C", "SELL", make_config()), 5
        )

    def test_stressed_delta_bounds_and_soft_etf_limit(self):
        options = [OptionQuote("RTM50C", 50, "C", 1.10, 1.12, -100)]
        bounds = portfolio_delta_bounds(0, options, 50, 1 / 12, 0, 0.20, 0.01)
        self.assertLess(bounds[0], bounds[1])
        change = hedge_for_bounds(39_000, (-10_000, -8_000), 40_000)
        self.assertEqual(change, 1_000)


if __name__ == "__main__":
    unittest.main()
