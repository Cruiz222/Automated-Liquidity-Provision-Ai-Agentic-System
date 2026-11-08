"""Chain-aware token address normalization."""

from __future__ import annotations

EVM_CHAINS = frozenset({
    "ethereum",
    "arbitrum",
    "base",
    "polygon",
    "optimism",
    "bsc",
    "avalanche",
    "avax",
})


def normalize_chain(chain: str) -> str:
    if not isinstance(chain, str) or not chain.strip():
        raise ValueError("chain is required")
    return chain.strip().lower()


def normalize_token_address(chain: str, address: str) -> str:
    normalized_chain = normalize_chain(chain)

    if not isinstance(address, str) or not address.strip():
        raise ValueError("token address is required")

    address = address.strip()

    if normalized_chain in EVM_CHAINS:
        return address.lower()

    # Solana Base58 addresses and unknown-chain addresses retain case.
    return address



def normalize_coin_id(coin_id: str) -> str:
    """Normalize a DefiLlama chain:address coin identifier.

    EVM addresses are case-insensitive. Solana Base58 mint addresses
    and unknown-chain addresses retain their original casing.
    """
    if not isinstance(coin_id, str) or ":" not in coin_id:
        raise ValueError(
            "coin_id must contain a chain prefix and address"
        )

    chain, address = coin_id.split(":", 1)

    normalized_chain = normalize_chain(chain)
    normalized_address = normalize_token_address(
        normalized_chain,
        address,
    )

    return f"{normalized_chain}:{normalized_address}"


def normalize_token_addresses(
    chain: str,
    addresses: tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    return tuple(
        normalize_token_address(chain, address)
        for address in addresses
    )
