import unittest

from etf_arb_trader import (
    BEAR,
    BULL,
    CAD,
    RITC,
    USD,
    Config,
    Quote,
    build_parser,
    config_from_args,
    discovered_limits,
    find_opportunities,
    projected_positions,
    tender_opportunity,
    weighted_exposure,
    within_limits,
)


def config(**overrides):
    values = dict(
        api_mode="rest",
        base_url="http://localhost:9999/v1",
        api_key="test",
        username=None,
        password=None,
        live=False,
        poll_seconds=0.5,
        request_timeout=2,
        order_size=1000,
        min_net_edge=0.02,
        tender_min_net_edge=0.03,
        limit_usage=0.85,
        fallback_gross_limit=300_000,
        fallback_net_limit=25_000,
        fx_tolerance=100,
        max_retries=4,
    )
    values.update(overrides)
    return Config(**values)


class PricingTests(unittest.TestCase):
    def test_basket_rich_uses_executable_fx_ask_and_fees(self):
        opportunities = find_opportunities(
            Quote(10.10, 10.12, 2000, 2000),
            Quote(15.10, 15.12, 2000, 2000),
            Quote(17.00, 17.02, 2000, 2000),
            Quote(1.40, 1.41, 1_000_000, 1_000_000),
            1000,
            0.02,
        )
        self.assertTrue(opportunities)
        self.assertEqual(opportunities[0].direction, "BASKET_RICH")
        expected_gross = 10.10 + 15.10 - 17.02 * 1.41
        expected_net = expected_gross - (0.02 + 0.02 + 0.02 * 1.41)
        self.assertAlmostEqual(opportunities[0].net_edge, expected_net)

    def test_no_trade_when_edge_does_not_cover_fees(self):
        opportunities = find_opportunities(
            Quote(10.00, 10.01, 1000, 1000),
            Quote(15.00, 15.01, 1000, 1000),
            Quote(17.85, 17.86, 1000, 1000),
            Quote(1.40, 1.41, 1_000_000, 1_000_000),
            1000,
            0.02,
        )
        self.assertEqual(opportunities, [])

    def test_tender_buy_is_hedged_by_selling_basket(self):
        result = tender_opportunity(
            {"ticker": RITC, "action": "BUY", "price": 17.0},
            Quote(12.1, 12.2, 1000, 1000),
            Quote(13.1, 13.2, 1000, 1000),
            Quote(1.4, 1.41, 1000, 1000),
        )
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "BASKET_RICH")


class RiskTests(unittest.TestCase):
    def test_ritc_has_position_multiplier_two(self):
        positions = {BULL: 100, BEAR: -50, RITC: 200, USD: 0, CAD: 0}
        gross, net = weighted_exposure(positions)
        self.assertEqual(gross, 550)
        self.assertEqual(net, 450)

    def test_completed_package_is_weighted_net_neutral(self):
        positions = {BULL: 0, BEAR: 0, RITC: 0, USD: 0, CAD: 0}
        projected = projected_positions(positions, "BASKET_RICH", 1000)
        gross, net = weighted_exposure(projected)
        self.assertEqual(gross, 4000)
        self.assertEqual(net, 0)

    def test_api_limits_are_softened(self):
        rows = [{"name": "LIMIT", "gross_limit": 100_000, "net_limit": 20_000}]
        self.assertEqual(discovered_limits(rows, config()), (85_000, 17_000))

    def test_projected_limit_check_blocks_oversize_package(self):
        positions = {BULL: 0, BEAR: 0, RITC: 0, USD: 0, CAD: 0}
        projected = projected_positions(positions, "ETF_RICH", 30_000)
        rows = [{"gross_limit": 100_000, "net_limit": 20_000}]
        self.assertFalse(within_limits(projected, rows, config()))

    def test_default_config(self):
        result = config_from_args(build_parser().parse_args([]))
        self.assertEqual(result.api_mode, "rest")
        self.assertEqual(result.order_size, 1000)
        self.assertEqual(result.base_url, "http://localhost:9999/v1")
        self.assertTrue(result.live)

    def test_dma_default_endpoint(self):
        result = config_from_args(
            build_parser().parse_args(
                ["--api-mode", "dma", "--username", "u", "--password", "p"]
            )
        )
        self.assertEqual(result.base_url, "http://flserver.rotman.utoronto.ca:16635/v1")


if __name__ == "__main__":
    unittest.main()
