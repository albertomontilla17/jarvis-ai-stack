"""
setup_polymarket_creds.py
--------------------------
Generates Polymarket CLOB API credentials (apiKey, secret, passphrase)
from your MetaMask private key. Run this ONCE to get your credentials,
then store them safely as environment variables.

Usage:
    python setup_polymarket_creds.py

Environment variables required:
    POLYGON_PRIVATE_KEY   — Your MetaMask wallet private key (with 0x prefix)
"""

import os
from py_clob_client_v2 import ClobClient

def main():
    private_key = os.environ.get("POLYGON_PRIVATE_KEY")
    if not private_key:
        print("❌ Error: POLYGON_PRIVATE_KEY not set.")
        print("   Run: $env:POLYGON_PRIVATE_KEY=\"0x...your_key...\"")
        return

    print("🔐 Connecting to Polymarket CLOB...")

    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,  # Polygon mainnet
        key=private_key,
        signature_type=0,  # EOA — standard MetaMask wallet
        funder=os.environ.get("POLYGON_ADDRESS", ""),
    )

    print("🔑 Generating API credentials...")
    credentials = client.create_or_derive_api_key()

    print("\n✅ Credentials generated successfully!")
    print("=" * 60)
    print(f"API Key:     {credentials.api_key}")
    print(f"Secret:      {credentials.api_secret}")
    print(f"Passphrase:  {credentials.api_passphrase}")
    print("=" * 60)
    print("\n⚠️  Save these values securely — add them to your .bat file:")
    print(f'set POLY_API_KEY={credentials.api_key}')
    print(f'set POLY_SECRET={credentials.api_secret}')
    print(f'set POLY_PASSPHRASE={credentials.api_passphrase}')
    print("\nNever commit these to GitHub.")

if __name__ == "__main__":
    main()
