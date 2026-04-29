#!/usr/bin/env python3
"""
Solana Wallet MCP Server
Exposes Solana wallet management as tools for AI agents (Claude Code, OpenClaw).

Installation:
    pip install mcp solana solders base58

Adding to Claude Desktop:
    Edit %APPDATA%\Claude\claude_desktop_config.json and add:

    {
      "mcpServers": {
        "solana": {
          "command": "C:/Users/Пользователь/AppData/Local/Programs/Python/Python310/python.exe",
          "args": ["C:/bots/Solana wallets creator/solana_mcp.py"]
        }
      }
    }

    Restart Claude Desktop. You can then type in chat:
      "Create 5 wallets"
      "Show balances of all wallets"
      "Send 0.01 SOL from all wallets to address XYZ"
      "Show private key of wallet 3"
      "Send 0.05 SOL from wallets 1, 3 and 5 to address ABC"
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import List

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("Install the MCP SDK: pip install mcp", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from solana_wallets_creator import SolanaWalletManager

mcp = FastMCP("Solana Wallet Manager")

# Lazy init: create the manager on first tool call, not at import time.
# This prevents a startup crash if the RPC endpoint is temporarily unreachable.
_manager: SolanaWalletManager | None = None

def get_manager() -> SolanaWalletManager:
    global _manager
    if _manager is None:
        _manager = SolanaWalletManager()
    return _manager


def _cap(func, *args, **kwargs) -> str:
    """Capture stdout of a function call and return it as a string."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        func(*args, **kwargs)
    return buf.getvalue().strip() or "Done."


def mgr() -> SolanaWalletManager:
    """Shortcut to get the lazy-loaded manager instance."""
    return get_manager()


# ------------------------------------------------------------------
# Tools exposed to the AI agent
# ------------------------------------------------------------------

@mcp.tool()
def create_wallets(count: int) -> str:
    """
    Generate N new Solana wallets and save them locally.
    Returns a list of public keys.
    count: number of wallets to create (1-1000).
    """
    if count <= 0 or count > 1000:
        return "Error: count must be between 1 and 1000."
    return _cap(mgr().create_wallets, count)


@mcp.tool()
def list_wallets() -> str:
    """
    Show all wallets with their current SOL balances.
    Fetches live balances from the blockchain before displaying.
    """
    return _cap(mgr().list_wallets)


@mcp.tool()
def get_wallet_count() -> str:
    """Return the total number of wallets stored locally."""
    return f"Total wallets: {len(mgr().wallets)}"


@mcp.tool()
def get_wallet_balance(wallet_index: int) -> str:
    """
    Check the balance of a specific wallet by its number.
    wallet_index: wallet number (starting from 1).
    """
    wallets = mgr().wallets
    if wallet_index < 1 or wallet_index > len(wallets):
        return f"Error: index {wallet_index} is out of range 1..{len(wallets)}"
    w = wallets[wallet_index - 1]
    try:
        bal = mgr().get_balance(w["public_key"])
    except RuntimeError as e:
        return (
            f"Wallet #{wallet_index}\n"
            f"  Address : {w['public_key']}\n"
            f"  Balance : ERROR — {e}\n"
            f"  Tip: the public mainnet RPC is rate-limited. "
            f"Try again in a few seconds or switch to a private RPC with: switch_network(<url>)"
        )
    return (
        f"Wallet #{wallet_index}\n"
        f"  Address : {w['public_key']}\n"
        f"  Balance : {bal:.6f} SOL"
    )


@mcp.tool()
def check_address_balance(address: str) -> str:
    """
    Check the balance of any Solana address (not limited to stored wallets).
    address: base58 public key.
    """
    try:
        bal = mgr().get_balance(address)
    except RuntimeError as e:
        return f"Address : {address}\nBalance : ERROR — {e}"
    return f"Address : {address}\nBalance : {bal:.6f} SOL"


@mcp.tool()
def show_private_key(wallet_index: int) -> str:
    """
    Show the private key of a wallet in base58 format.
    Useful for importing into Phantom, Solflare, etc.
    WARNING: Anyone with the private key has full control over the wallet.
    wallet_index: wallet number (starting from 1).
    """
    wallets = mgr().wallets
    if wallet_index < 1 or wallet_index > len(wallets):
        return f"Error: index {wallet_index} is out of range 1..{len(wallets)}"
    w = wallets[wallet_index - 1]
    return (
        f"Wallet #{wallet_index}\n"
        f"  Public key  : {w['public_key']}\n"
        f"  Private key : {w['private_key']}\n"
        f"\n  WARNING: Never share your private key with anyone!"
    )


@mcp.tool()
def show_all_private_keys() -> str:
    """
    Show the private keys of ALL wallets.
    Only use this in a secure environment.
    """
    wallets = mgr().wallets
    if not wallets:
        return "No wallets found. Create them with create_wallets."
    lines = ["All wallets and private keys:\n"]
    for w in wallets:
        lines.append(
            f"#{w['index']:>3} | {w['public_key']} | {w['private_key']}"
        )
    lines.append("\nWARNING: Never share private keys with anyone!")
    return "\n".join(lines)


@mcp.tool()
def import_wallet(private_key: str) -> str:
    """
    Import an existing Solana wallet using its base58 private key.
    Supports both 64-byte full keypairs and 32-byte seeds.
    The imported wallet is added to the local wallet list.
    private_key: base58 encoded private key (e.g. from Phantom export).
    """
    result = mgr().import_wallet(private_key)
    if result:
        return (
            f"Wallet imported as #{result['index']}\n"
            f"  Public key : {result['public_key']}"
        )
    return "Import failed. Check the private key and try again."


@mcp.tool()
def send_sol_all_wallets(recipient_address: str, amount_sol_per_wallet: float) -> str:
    """
    Send the same amount of SOL from ALL wallets to a single address.
    Wallets with insufficient balance are skipped automatically.
    recipient_address: destination address (base58).
    amount_sol_per_wallet: SOL amount to send from each wallet.
    """
    if amount_sol_per_wallet <= 0:
        return "Error: amount must be greater than 0."
    return _cap(
        mgr().send_all_wallets,
        recipient_address,
        amount_sol_per_wallet,
        skip_confirm=True,
    )


@mcp.tool()
def send_sol_selected_wallets(
    recipient_address: str,
    amount_sol: float,
    wallet_indices: List[int],
) -> str:
    """
    Send SOL from a specific set of wallets to a single address.
    recipient_address: destination address (base58).
    amount_sol: SOL amount to send from each selected wallet.
    wallet_indices: list of wallet numbers, e.g. [1, 2, 5].
    """
    if amount_sol <= 0:
        return "Error: amount must be greater than 0."
    if not wallet_indices:
        return "Error: provide at least one wallet index."
    return _cap(
        mgr().send_selected_wallets,
        recipient_address,
        amount_sol,
        wallet_indices,
        skip_confirm=True,
    )


@mcp.tool()
def switch_network(network: str) -> str:
    """
    Switch the Solana network.
    network: 'mainnet', 'devnet', or a full custom RPC URL.
    """
    from solana.rpc.api import Client

    if network == "mainnet":
        url = "https://api.mainnet-beta.solana.com"
    elif network == "devnet":
        url = "https://api.devnet.solana.com"
    else:
        url = network
    mgr().rpc_url = url
    mgr().client = Client(url)
    return f"Network switched to: {url}"


@mcp.tool()
def export_wallets(filepath: str = "") -> str:
    """
    Export all wallets to a JSON file.
    filepath: output path (optional, defaults to wallets.export.json).
    """
    return _cap(mgr().export_wallets, filepath if filepath else None)


@mcp.tool()
def get_transaction_info(signature: str) -> str:
    """
    Get the on-chain status and Solscan link for any transaction signature.
    Use this when the user asks about a transaction, wants to check if it
    landed, or asks for a Solscan link.
    signature: base58 transaction signature (88 characters).
    """
    info = mgr().get_tx_status(signature)
    lines = [
        f"Transaction : {signature}",
        f"Status      : {info['status']}",
    ]
    if info["err"]:
        lines.append(f"Error       : {info['err']}")
    if info["link"]:
        lines.append(f"Solscan     : {info['link']}")
    return "\n".join(lines)


@mcp.tool()
def get_last_transaction(wallet_index: int) -> str:
    """
    Get the Solscan link and on-chain status of the last transaction sent
    from a specific wallet. Use this when the user asks to check a recent
    transfer or get a link to the last transaction of a wallet.
    wallet_index: wallet number (starting from 1).
    """
    wallets = mgr().wallets
    if wallet_index < 1 or wallet_index > len(wallets):
        return f"Error: index {wallet_index} is out of range 1..{len(wallets)}"
    w = wallets[wallet_index - 1]
    sig = w.get("last_tx")
    if not sig:
        return f"Wallet #{wallet_index} has no recorded transactions yet."
    info = mgr().get_tx_status(sig)
    lines = [
        f"Wallet #{wallet_index} — last transaction",
        f"Signature   : {sig}",
        f"Status      : {info['status']}",
    ]
    if info["err"]:
        lines.append(f"Error       : {info['err']}")
    if info["link"]:
        lines.append(f"Solscan     : {info['link']}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
