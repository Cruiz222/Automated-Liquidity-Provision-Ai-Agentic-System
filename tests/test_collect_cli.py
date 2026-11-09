import unittest
from pathlib import Path

from app.dex.collect_cli import (
    build_parser,
    build_provider,
    resolve_database_path,
    resolve_min_tvl,
)
from app.dex.defillama import DefiLlamaPoolProvider
from app.dex.meteora import MeteoraDLMMProvider
from app.dex.provider import FixturePoolProvider


class CollectCLIProviderTests(unittest.TestCase):
    def test_live_provider_defaults_to_defillama(self):
        args = build_parser().parse_args(["--mode", "live"])

        self.assertEqual(args.provider, "defillama")
        self.assertIsInstance(build_provider(args), DefiLlamaPoolProvider)

    def test_live_mode_can_select_meteora(self):
        args = build_parser().parse_args([
            "--mode", "live",
            "--provider", "meteora",
        ])

        self.assertIsInstance(build_provider(args), MeteoraDLMMProvider)

    def test_meteora_options_are_passed_to_its_universe(self):
        args = build_parser().parse_args([
            "--mode", "live",
            "--provider", "meteora",
            "--min-tvl", "250000",
            "--max-pools", "75",
            "--page-size", "50",
            "--max-pages", "4",
            "--include-blacklisted",
        ])

        provider = build_provider(args)

        self.assertEqual(provider.universe.min_tvl_usd, 250000)
        self.assertEqual(provider.universe.max_pools, 75)
        self.assertEqual(provider.universe.page_size, 50)
        self.assertEqual(provider.universe.max_pages, 4)
        self.assertTrue(provider.universe.include_blacklisted)

    def test_defillama_default_min_tvl(self):
        args = build_parser().parse_args(["--mode", "live"])

        self.assertEqual(resolve_min_tvl(args), 5_000_000.0)
        self.assertEqual(
            build_provider(args).universe.min_tvl_usd,
            5_000_000.0,
        )

    def test_meteora_default_min_tvl(self):
        args = build_parser().parse_args([
            "--mode", "live",
            "--provider", "meteora",
        ])

        self.assertEqual(resolve_min_tvl(args), 100_000.0)
        self.assertEqual(
            build_provider(args).universe.min_tvl_usd,
            100_000.0,
        )

    def test_explicit_min_tvl_overrides_provider_default(self):
        args = build_parser().parse_args([
            "--mode", "live",
            "--provider", "meteora",
            "--min-tvl", "250000",
        ])

        self.assertEqual(resolve_min_tvl(args), 250_000.0)

    def test_defillama_uses_separate_default_database(self):
        args = build_parser().parse_args(["--mode", "live"])

        self.assertEqual(
            resolve_database_path(args),
            Path("data/defillama_snapshots.sqlite3"),
        )

    def test_meteora_uses_separate_default_database(self):
        args = build_parser().parse_args([
            "--mode", "live",
            "--provider", "meteora",
        ])

        self.assertEqual(
            resolve_database_path(args),
            Path("data/meteora_snapshots.sqlite3"),
        )

    def test_fixture_uses_separate_default_database(self):
        args = build_parser().parse_args(["--mode", "fixture"])

        self.assertEqual(
            resolve_database_path(args),
            Path("data/fixture_snapshots.sqlite3"),
        )

    def test_explicit_database_path_overrides_default(self):
        args = build_parser().parse_args([
            "--mode", "live",
            "--provider", "meteora",
            "--database", "/tmp/custom-research.sqlite3",
        ])

        self.assertEqual(
            resolve_database_path(args),
            Path("/tmp/custom-research.sqlite3"),
        )

    def test_fixture_mode_still_builds_fixture_provider(self):
        args = build_parser().parse_args(["--mode", "fixture"])

        self.assertIsInstance(build_provider(args), FixturePoolProvider)


if __name__ == "__main__":
    unittest.main()
