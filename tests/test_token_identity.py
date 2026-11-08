import unittest

from app.dex.token_identity import (
    normalize_chain,
    normalize_token_address,
    normalize_token_addresses,
)


class TestTokenIdentity(unittest.TestCase):
    def test_normalizes_chain_name(self):
        self.assertEqual(normalize_chain(" Solana "), "solana")

    def test_lowercases_evm_addresses(self):
        self.assertEqual(
            normalize_token_address("ethereum", "  0xAbCdEf  "),
            "0xabcdef",
        )

    def test_preserves_solana_base58_case(self):
        address = "So11111111111111111111111111111111111111112"
        self.assertEqual(
            normalize_token_address("solana", address),
            address,
        )

    def test_preserves_unknown_chain_address_case(self):
        address = "AbCd1234"
        self.assertEqual(
            normalize_token_address("unknown-chain", address),
            address,
        )

    def test_normalizes_address_tuple_by_chain(self):
        self.assertEqual(
            normalize_token_addresses(
                "bsc",
                ("0xAbC", "0xDeF"),
            ),
            ("0xabc", "0xdef"),
        )

    def test_rejects_missing_chain(self):
        with self.assertRaises(ValueError):
            normalize_chain(" ")

    def test_rejects_missing_address(self):
        with self.assertRaises(ValueError):
            normalize_token_address("solana", " ")


    def test_normalizes_complete_defillama_coin_ids(self):
        from app.dex.token_identity import normalize_coin_id

        self.assertEqual(
            normalize_coin_id("ethereum:0xAbCd"),
            "ethereum:0xabcd",
        )
        self.assertEqual(
            normalize_coin_id("avax:0xAbCd"),
            "avax:0xabcd",
        )

    def test_preserves_solana_coin_id_case(self):
        from app.dex.token_identity import normalize_coin_id

        mint = "So11111111111111111111111111111111111111112"

        self.assertEqual(
            normalize_coin_id(f"Solana:{mint}"),
            f"solana:{mint}",
        )

    def test_preserves_unknown_chain_coin_id_case(self):
        from app.dex.token_identity import normalize_coin_id

        self.assertEqual(
            normalize_coin_id("custom:AbCd1234"),
            "custom:AbCd1234",
        )

    def test_rejects_invalid_coin_id(self):
        from app.dex.token_identity import normalize_coin_id

        with self.assertRaises(ValueError):
            normalize_coin_id("invalid-coin-id")


if __name__ == "__main__":
    unittest.main()
