#!/usr/bin/env python3
"""Wrap USDC.e → pUSD via Polymarket CollateralOnramp contract."""

import os
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from web3 import Web3
from eth_account import Account
from lib.contracts import CONTRACTS, ERC20_ABI, POLYGON_CHAIN_ID

ONRAMP_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "_asset", "type": "address"},
            {"internalType": "address", "name": "_to", "type": "address"},
            {"internalType": "uint256", "name": "_amount", "type": "uint256"},
        ],
        "name": "wrap",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def wrap_usdc_to_pusd(amount_usd: float):
    private_key = os.environ.get("POLYCLAW_PRIVATE_KEY")
    rpc_url = os.environ.get("CHAINSTACK_NODE")

    if not private_key or not rpc_url:
        print("Error: Set POLYCLAW_PRIVATE_KEY and CHAINSTACK_NODE environment variables.")
        sys.exit(1)

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    account = Account.from_key(private_key)
    address = Web3.to_checksum_address(account.address)
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 60, "proxies": {}}))

    amount_wei = int(amount_usd * 1e6)

    # Check USDC.e balance
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(CONTRACTS["USDC_E"]),
        abi=ERC20_ABI,
    )
    usdc_balance = usdc.functions.balanceOf(address).call()
    print(f"USDC.e balance: {usdc_balance / 1e6:.2f}")

    if usdc_balance < amount_wei:
        print(f"Error: Insufficient USDC.e. Have {usdc_balance / 1e6:.2f}, need {amount_usd:.2f}")
        sys.exit(1)

    # Step 1: Approve CollateralOnramp to spend USDC.e
    print(f"Step 1: Approving CollateralOnramp to spend {amount_usd} USDC.e...")
    approve_tx = usdc.functions.approve(
        Web3.to_checksum_address(CONTRACTS["COLLATERAL_ONRAMP"]),
        amount_wei,
    ).build_transaction({
        "from": address,
        "nonce": w3.eth.get_transaction_count(address),
        "gas": 100000,
        "gasPrice": w3.eth.gas_price,
        "chainId": POLYGON_CHAIN_ID,
    })
    signed = account.sign_transaction(approve_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Approve TX: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        print("Error: Approve transaction failed.")
        sys.exit(1)
    print("  Approved ✓")

    # Step 2: Call wrap(_asset, _to, _amount)
    print(f"Step 2: Wrapping {amount_usd} USDC.e → pUSD...")
    onramp = w3.eth.contract(
        address=Web3.to_checksum_address(CONTRACTS["COLLATERAL_ONRAMP"]),
        abi=ONRAMP_ABI,
    )
    wrap_tx = onramp.functions.wrap(
        Web3.to_checksum_address(CONTRACTS["USDC_E"]),  # _asset
        address,                                          # _to
        amount_wei,                                       # _amount
    ).build_transaction({
        "from": address,
        "nonce": w3.eth.get_transaction_count(address),
        "gas": 200000,
        "gasPrice": w3.eth.gas_price,
        "chainId": POLYGON_CHAIN_ID,
    })
    signed = account.sign_transaction(wrap_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Wrap TX: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        print("Error: Wrap transaction failed.")
        sys.exit(1)
    print("  Wrap confirmed ✓")

    # Check new pUSD balance
    pusd = w3.eth.contract(
        address=Web3.to_checksum_address(CONTRACTS["PUSD"]),
        abi=ERC20_ABI,
    )
    pusd_balance = pusd.functions.balanceOf(address).call() / 1e6
    print(f"\nDone! pUSD balance: {pusd_balance:.2f}")


def main():
    parser = argparse.ArgumentParser(description="Wrap USDC.e → pUSD")
    parser.add_argument("amount", type=float, help="Amount in USD to wrap (e.g. 20)")
    args = parser.parse_args()
    wrap_usdc_to_pusd(args.amount)


if __name__ == "__main__":
    main()
