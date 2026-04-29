#!/usr/bin/env python3
"""
Solana Multi-Wallet Manager
Can be used as a skill in OpenClaw / Claude Code AI agents.

Commands:
  create <N>                   -- generate N Solana wallets
  list                         -- show all wallets with balances
  balance <pubkey>             -- check balance of any external address
  show_key <N>                 -- show public + private key of wallet #N
  show_all_keys                -- show keys of all wallets
  send_all <addr> <amount>     -- send SOL from ALL wallets
  send_selected <addr> <amt> <1,2,3> -- send from selected wallets
  send_manual <addr>           -- manually enter amount for each wallet
  export [filepath]            -- export wallets to JSON
  delete_all                   -- delete all wallets from storage
  rpc <url|devnet|mainnet>     -- switch RPC endpoint
  help                         -- show this help
  quit                         -- exit
"""

import os
import sys
import json
import struct
import time
import base58
from pathlib import Path
from typing import List, Optional

try:
    from solana.rpc.api import Client
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.signature import Signature as SoldersSignature
    from solders.system_program import transfer, TransferParams
    from solders.instruction import Instruction
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
except ImportError:
    print("Missing dependencies. Install them with:")
    print("   pip install solana solders base58")
    sys.exit(1)


# ==================== CONFIGURATION ====================

WALLETS_FILE = Path(__file__).parent / "wallets.json"
CONFIG_FILE  = Path(__file__).parent / "config.json"

# RPC URL is loaded from config.json (never hardcoded here).
# Create config.json in the project folder:
#   { "rpc_url": "https://mainnet.helius-rpc.com/?api-key=YOUR_KEY" }
_DEFAULT_RPC = "https://api.mainnet-beta.solana.com"

def _load_rpc_url() -> str:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            url = data.get("rpc_url", "").strip()
            if url:
                return url
        except Exception:
            pass
    return _DEFAULT_RPC

RPC_URL = _load_rpc_url()

# Compute Budget program ID (constant, will never change)
COMPUTE_BUDGET_PROGRAM_ID = Pubkey.from_string("ComputeBudget111111111111111111111111111111")

# SOL reserved per transaction to cover the base signature fee (5000 lamports)
# plus headroom for the priority fee added below.
FEE_BUFFER_SOL: float = 0.000_010  # 10 000 lamports — safe upper bound


# ------------------------------------------------------------------
# Solscan helpers
# ------------------------------------------------------------------

def solscan_link(signature: str, rpc_url: str) -> str:
    """Return the Solscan URL for a transaction signature."""
    cluster = "?cluster=devnet" if "devnet" in rpc_url else ""
    return f"https://solscan.io/tx/{signature}{cluster}"


# ------------------------------------------------------------------
# Compute Budget helpers (no external package required)
# ------------------------------------------------------------------

def _set_compute_unit_limit(units: int) -> Instruction:
    """Build a SetComputeUnitLimit instruction (discriminant byte = 2)."""
    data = bytes([2]) + struct.pack("<I", units)
    return Instruction(program_id=COMPUTE_BUDGET_PROGRAM_ID, accounts=[], data=data)


def _set_compute_unit_price(micro_lamports: int) -> Instruction:
    """Build a SetComputeUnitPrice instruction (discriminant byte = 3)."""
    data = bytes([3]) + struct.pack("<Q", micro_lamports)
    return Instruction(program_id=COMPUTE_BUDGET_PROGRAM_ID, accounts=[], data=data)


# ==================== WALLET MANAGER ====================

class SolanaWalletManager:
    """Manages a collection of Solana wallets stored locally."""

    def __init__(self, rpc_url: str = RPC_URL):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)
        self.wallets_file = WALLETS_FILE
        self.wallets: List[dict] = []
        self._load_wallets()

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def _load_wallets(self):
        if self.wallets_file.exists():
            with open(self.wallets_file, "r") as f:
                self.wallets = json.load(f)
        else:
            self.wallets = []

    def _save_wallets(self):
        # Write to a temp file first, then atomically replace the target.
        # This prevents data loss if the process is killed mid-write.
        tmp = self.wallets_file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self.wallets, f, indent=2)
        os.replace(tmp, self.wallets_file)

    # ------------------------------------------------------------------
    # Wallet generation
    # ------------------------------------------------------------------

    def create_wallets(self, count: int) -> List[dict]:
        """Generate N new Solana wallets and persist them to disk."""
        new_wallets = []
        start_index = len(self.wallets)
        print(f"\nGenerating {count} wallets...\n")

        for i in range(count):
            keypair = Keypair()
            wallet_info = {
                "index": start_index + i + 1,
                "public_key": str(keypair.pubkey()),
                # Store the full 64-byte keypair (seed + pubkey)
                "private_key": base58.b58encode(bytes(keypair)).decode(),
                "balance": 0.0,
            }
            new_wallets.append(wallet_info)
            self.wallets.append(wallet_info)

        self._save_wallets()

        print(f"Created {count} wallets:")
        for w in new_wallets:
            print(f"  [{w['index']}] {w['public_key']}")

        return new_wallets

    # ------------------------------------------------------------------
    # Balances
    # ------------------------------------------------------------------

    def get_balance(self, pubkey_str: str, retries: int = 3) -> float:
        """
        Return SOL balance for a public key.
        Retries up to `retries` times with exponential back-off on RPC errors.
        Raises RuntimeError if all attempts fail so callers can surface the
        real error instead of silently reporting 0.
        """
        pubkey = Pubkey.from_string(pubkey_str)
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt in range(retries):
            try:
                resp = self.client.get_balance(pubkey)
                if resp.value is None:
                    return 0.0
                return resp.value / 1_000_000_000
            except Exception as e:
                last_exc = e
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)  # 1 s, 2 s
        raise RuntimeError(
            f"RPC get_balance failed after {retries} attempts: {last_exc}"
        )

    def update_all_balances(self):
        """Fetch and update balances for all wallets from the blockchain."""
        print("\nFetching balances...")
        for wallet in self.wallets:
            try:
                bal = self.get_balance(wallet["public_key"])
                wallet["balance"] = bal
            except RuntimeError as e:
                print(f"  #{wallet['index']} balance error: {e}")
                # Keep previous cached value — don't overwrite with 0
        self._save_wallets()

    def list_wallets(self):
        """Print a table of all wallets with live balances."""
        if not self.wallets:
            print("No wallets found. Run: create <N>")
            return

        self.update_all_balances()

        print("\n" + "=" * 80)
        print(f"{'#':<4} {'Public Key':<52} {'Balance (SOL)':>12}")
        print("=" * 80)

        total = 0.0
        for w in self.wallets:
            pk_short = w["public_key"][:30] + "..." + w["public_key"][-15:]
            bal = w["balance"]
            total += max(bal, 0.0)
            icon = "[+]" if bal > 0 else "[ ]"
            print(f"{icon} {w['index']:<3} {pk_short:<52} {bal:>12.6f}")

        print("=" * 80)
        print(f"{'Total SOL:':<57} {total:.6f}")
        print()

    def check_external_balance(self, pubkey_str: str):
        """Check the balance of any Solana address."""
        try:
            bal = self.get_balance(pubkey_str)
            print(f"\nBalance of {pubkey_str[:20]}... = {bal:.6f} SOL")
        except RuntimeError as e:
            print(f"\nCould not fetch balance: {e}")

    def show_key(self, wallet_index: int):
        """Display the public and private key of a specific wallet."""
        if wallet_index < 1 or wallet_index > len(self.wallets):
            print(f"Invalid index. Available: 1..{len(self.wallets)}")
            return
        w = self.wallets[wallet_index - 1]
        print(f"\nWallet #{wallet_index}")
        print(f"  Public key  : {w['public_key']}")
        print(f"  Private key : {w['private_key']}")
        print("  WARNING: Anyone with the private key has full access to this wallet!\n")

    def show_all_keys(self):
        """Display keys for all wallets."""
        if not self.wallets:
            print("No wallets. Create them with: create <N>")
            return
        print("\n" + "=" * 100)
        print(f"{'#':<4} {'Public Key':<46} {'Private Key'}")
        print("=" * 100)
        for w in self.wallets:
            print(f"  {w['index']:<3} {w['public_key']:<46} {w['private_key']}")
        print("=" * 100)
        print("WARNING: Private keys give full access to wallets. Keep them secret!\n")

    # ------------------------------------------------------------------
    # Sending SOL
    # ------------------------------------------------------------------

    def _get_keypair(self, wallet_index: int) -> Keypair:
        """Return a Keypair object for the wallet at the given index (1-based)."""
        if wallet_index < 1 or wallet_index > len(self.wallets):
            raise ValueError(f"Invalid index. Available: 1..{len(self.wallets)}")
        w = self.wallets[wallet_index - 1]
        # Decode the full 64-byte keypair
        secret_bytes = base58.b58decode(w["private_key"])
        return Keypair.from_bytes(secret_bytes)

    def _wait_for_confirmation(self, signature, max_retries: int = 40) -> bool:
        """
        Poll get_signature_statuses until the transaction is confirmed or
        the timeout is reached (~40 s).  Raises on an on-chain execution error.
        """
        for _ in range(max_retries):
            time.sleep(1)
            resp = self.client.get_signature_statuses([signature])
            status = resp.value[0]
            if status is not None:
                if status.err:
                    raise Exception(f"Transaction failed on-chain: {status.err}")
                return True  # any non-None, non-error status means it landed
        return False  # confirmation timeout — tx may still land eventually

    def send_from_wallet(
        self, wallet_index: int, recipient: str, amount_sol: float
    ) -> Optional[str]:
        """
        Send SOL from a single wallet to a recipient address.
        Adds a priority fee (1 000 µL/CU) to improve landing during congestion.
        Polls for confirmation before returning.
        Returns the transaction signature string, or None on failure.
        """
        try:
            sender_kp = self._get_keypair(wallet_index)
            recipient_pk = Pubkey.from_string(recipient)
            amount_lamports = int(amount_sol * 1_000_000_000)

            latest = self.client.get_latest_blockhash()
            recent_blockhash = latest.value.blockhash

            # A simple SOL transfer consumes ~300 CUs; cap at 200 000 to be safe.
            # Priority fee: 1 000 micro-lamports/CU → ~0.2 lamports extra total.
            instructions = [
                _set_compute_unit_limit(200_000),
                _set_compute_unit_price(1_000),
                transfer(
                    TransferParams(
                        from_pubkey=sender_kp.pubkey(),
                        to_pubkey=recipient_pk,
                        lamports=amount_lamports,
                    )
                ),
            ]

            message = MessageV0.try_compile(
                payer=sender_kp.pubkey(),
                instructions=instructions,
                address_lookup_table_accounts=[],
                recent_blockhash=recent_blockhash,
            )

            tx = VersionedTransaction(message, [sender_kp])
            sig_resp = self.client.send_transaction(tx)
            signature = sig_resp.value  # solders Signature object

            confirmed = self._wait_for_confirmation(signature)
            sig_str = str(signature)

            # Persist last tx so the user can look it up later
            self.wallets[wallet_index - 1]["last_tx"] = sig_str
            self._save_wallets()

            link = solscan_link(sig_str, self.rpc_url)
            if confirmed:
                print(f"  OK  #{wallet_index} -> {sig_str[:30]}...")
                print(f"       Solscan: {link}")
            else:
                print(f"  TIMEOUT #{wallet_index} -> {sig_str[:30]}... (may still land)")
                print(f"       Solscan: {link}")
            return sig_str

        except Exception as e:
            print(f"  FAIL #{wallet_index}: {e}")
            return None

    def send_all_wallets(
        self, recipient: str, amount_sol: float, skip_confirm: bool = False
    ) -> List[Optional[str]]:
        """Send the same amount of SOL from ALL wallets to one address."""
        if not self.wallets:
            print("No wallets available.")
            return []

        self.update_all_balances()

        print(f"\nBulk send from all {len(self.wallets)} wallets:")
        print(f"   Recipient : {recipient}")
        print(f"   Amount    : {amount_sol} SOL per wallet\n")

        if not skip_confirm:
            confirm = input("Confirm? (y/n): ").strip().lower()
            if confirm != "y":
                print("Cancelled.")
                return []

        signatures = []
        for wallet in self.wallets:
            idx = wallet["index"]
            bal = wallet["balance"]
            required = amount_sol + FEE_BUFFER_SOL
            if bal < required:
                print(f"  #{idx}: balance {bal:.6f} < {required:.6f} SOL (amount + fees) -- skipping")
                signatures.append(None)
                continue
            sig = self.send_from_wallet(idx, recipient, amount_sol)
            signatures.append(sig)

        success = sum(1 for s in signatures if s is not None)
        print(f"\nResult: {success}/{len(self.wallets)} transactions succeeded.")
        return signatures

    def send_selected_wallets(
        self, recipient: str, amount_sol: float, indices: List[int],
        skip_confirm: bool = False
    ) -> List[Optional[str]]:
        """Send SOL from a specific subset of wallets."""
        if not self.wallets:
            print("No wallets available.")
            return []

        self.update_all_balances()

        valid = [i for i in indices if 1 <= i <= len(self.wallets)]
        invalid = [i for i in indices if i not in valid]
        if invalid:
            print(f"  Wallets not found: {invalid} -- skipping")

        if not valid:
            print("No valid wallet indices provided.")
            return []

        print(f"\nSending from {len(valid)} wallets -> {recipient}")
        print(f"   Amount: {amount_sol} SOL per wallet\n")

        if not skip_confirm:
            confirm = input("Confirm? (y/n): ").strip().lower()
            if confirm != "y":
                print("Cancelled.")
                return []

        signatures = []
        for idx in valid:
            bal = self.wallets[idx - 1]["balance"]
            required = amount_sol + FEE_BUFFER_SOL
            if bal < required:
                print(f"  #{idx}: balance {bal:.6f} < {required:.6f} SOL (amount + fees) -- skipping")
                signatures.append(None)
                continue
            sig = self.send_from_wallet(idx, recipient, amount_sol)
            signatures.append(sig)

        success = sum(1 for s in signatures if s is not None)
        print(f"\nResult: {success}/{len(valid)} transactions succeeded.")
        return signatures

    def send_manual_wallets(self, recipient: str) -> List[Optional[str]]:
        """Prompt the user to enter a custom send amount for each wallet."""
        if not self.wallets:
            print("No wallets available.")
            return []

        self.update_all_balances()

        print(f"\nManual send -> {recipient}")
        print("(Enter 0 or press Enter to skip a wallet)\n")

        amounts = []
        for wallet in self.wallets:
            idx = wallet["index"]
            bal = wallet["balance"]
            print(f"  #{idx}: {wallet['public_key'][:25]}...  balance = {bal:.6f} SOL")
            while True:
                try:
                    val = input(f"    Amount for #{idx} (SOL): ").strip()
                    amount = float(val) if val else 0.0
                    if amount < 0:
                        print("    Amount cannot be negative.")
                        continue
                    if amount > 0 and amount > bal:
                        print(f"    Insufficient balance. Maximum = {bal:.6f}")
                        continue
                    amounts.append(amount)
                    break
                except ValueError:
                    print("    Please enter a number.")

        print()
        signatures = []
        for i, wallet in enumerate(self.wallets):
            idx = wallet["index"]
            amount = amounts[i]
            if amount <= 0:
                print(f"  #{idx} -- skipped")
                signatures.append(None)
                continue
            sig = self.send_from_wallet(idx, recipient, amount)
            signatures.append(sig)

        success = sum(1 for s in signatures if s is not None)
        print(f"\nResult: {success}/{len(self.wallets)} transactions succeeded.")
        return signatures

    # ------------------------------------------------------------------
    # Transaction lookup
    # ------------------------------------------------------------------

    def get_tx_status(self, signature_str: str) -> dict:
        """
        Fetch the on-chain status of any transaction by its base58 signature.
        Returns a dict: {status, err, link}.
        """
        link = solscan_link(signature_str, self.rpc_url)
        try:
            sig = SoldersSignature.from_string(signature_str)
        except Exception:
            return {"status": "invalid_signature", "err": "Not a valid base58 signature", "link": ""}

        try:
            resp = self.client.get_signature_statuses([sig], search_transaction_history=True)
            val = resp.value[0]
        except Exception as e:
            return {"status": "rpc_error", "err": str(e), "link": link}

        if val is None:
            return {"status": "not found (too old or never broadcast)", "err": None, "link": link}

        if val.err:
            return {"status": "failed", "err": str(val.err), "link": link}

        conf = str(val.confirmation_status).split(".")[-1].lower() if val.confirmation_status else "processed"
        return {"status": conf, "err": None, "link": link}

    def show_tx(self, signature_str: str):
        """Print status and Solscan link for a transaction signature."""
        info = self.get_tx_status(signature_str)
        print(f"\nTransaction : {signature_str}")
        print(f"  Status    : {info['status']}")
        if info["err"]:
            print(f"  Error     : {info['err']}")
        if info["link"]:
            print(f"  Solscan   : {info['link']}")
        print()

    def get_last_tx(self, wallet_index: int):
        """Show the last transaction sent from wallet #N."""
        if wallet_index < 1 or wallet_index > len(self.wallets):
            print(f"Invalid index. Available: 1..{len(self.wallets)}")
            return
        w = self.wallets[wallet_index - 1]
        sig = w.get("last_tx")
        if not sig:
            print(f"  Wallet #{wallet_index} has no recorded transactions yet.")
            return
        self.show_tx(sig)

    # ------------------------------------------------------------------
    # Export / Delete
    # ------------------------------------------------------------------

    def import_wallet(self, private_key_b58: str) -> Optional[dict]:
        """
        Import a wallet from a base58 private key (88-char format from Phantom etc).
        Both 64-byte full keypairs and 32-byte seeds are supported.
        """
        try:
            raw = base58.b58decode(private_key_b58)
        except Exception:
            print("Error: invalid base58 string.")
            return None

        try:
            if len(raw) == 64:
                keypair = Keypair.from_bytes(raw)
            elif len(raw) == 32:
                keypair = Keypair.from_seed(raw)
            else:
                print(f"Error: unexpected key length ({len(raw)} bytes). Expected 32 or 64.")
                return None
        except Exception as e:
            print(f"Error loading keypair: {e}")
            return None

        pubkey = str(keypair.pubkey())

        # Check for duplicates
        for w in self.wallets:
            if w["public_key"] == pubkey:
                print(f"Wallet already exists as #{w['index']}: {pubkey}")
                return None

        wallet_info = {
            "index": len(self.wallets) + 1,
            "public_key": pubkey,
            "private_key": base58.b58encode(bytes(keypair)).decode(),
            "balance": 0.0,
        }
        self.wallets.append(wallet_info)
        self._save_wallets()

        print(f"Wallet imported as #{wallet_info['index']}: {pubkey}")
        return wallet_info

    def export_wallets(self, filepath: Optional[str] = None):
        """Export all wallets to a JSON file."""
        path = Path(filepath) if filepath else WALLETS_FILE.with_suffix(".export.json")
        with open(path, "w") as f:
            json.dump(self.wallets, f, indent=2)
        print(f"Wallets exported to: {path}")

    def delete_all_wallets(self):
        """Permanently delete all wallets from local storage."""
        confirm = input(
            "Delete ALL wallets? This is irreversible! Run export first. Type YES to confirm: "
        ).strip()
        if confirm == "YES":
            self.wallets = []
            self._save_wallets()
            print("All wallets deleted.")
        else:
            print("Cancelled.")


# ==================== CLI / INTERACTIVE MODE ====================

def print_usage():
    print("""
=================================================================
           Solana Multi-Wallet Manager
=================================================================
  Commands:

  create <N>                    Generate N wallets
  import <private_key>          Import wallet from base58 private key
  list                          Show wallets + balances
  balance <pubkey>              Check any external address
  show_key <N>                  Show keys of wallet #N
  show_all_keys                 Show keys of all wallets
  send_all <addr> <amount>      Send from ALL wallets
  send_selected <addr> <amt> <1,2,3>
                                Send from selected wallets
  send_manual <addr>            Enter amount per wallet manually
  tx <signature>                Check status + Solscan link for any tx
  last_tx <N>                   Check last tx of wallet #N
  export [filepath]             Export to JSON
  delete_all                    Delete all wallets
  rpc <url|devnet|mainnet>      Switch RPC endpoint
  help                          Show this help
  quit                          Exit

  Examples:
    create 5
    send_all 3Xk...abc 0.01
    send_selected 3Xk...abc 0.05 1,3,5
    tx 5UfD...Kq2Z
    last_tx 3
    rpc mainnet
=================================================================
""")


def interactive_mode(manager: SolanaWalletManager):
    """Interactive REPL -- suitable for use in OpenClaw / Claude Code."""
    print("Solana Multi-Wallet Manager -- Interactive Mode")
    print(f"   RPC     : {manager.rpc_url}")
    print(f"   Wallets : {len(manager.wallets)}")
    print("   Type 'help' for commands, 'quit' to exit.\n")

    while True:
        try:
            cmd = input("solana> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not cmd:
            continue

        parts = cmd.split()
        action = parts[0].lower()

        if action in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        elif action == "help":
            print_usage()

        elif action == "create":
            if len(parts) < 2:
                print("Usage: create <N>")
                continue
            try:
                count = int(parts[1])
                if count <= 0:
                    print("Count must be greater than 0.")
                    continue
                manager.create_wallets(count)
            except ValueError:
                print("Please enter an integer.")

        elif action == "list":
            manager.list_wallets()

        elif action == "balance":
            if len(parts) < 2:
                print("Usage: balance <pubkey>")
                continue
            manager.check_external_balance(parts[1])

        elif action == "show_key":
            if len(parts) < 2:
                print("Usage: show_key <N>")
                continue
            try:
                manager.show_key(int(parts[1]))
            except ValueError:
                print("Please enter a wallet number.")

        elif action == "show_all_keys":
            manager.show_all_keys()

        elif action == "import":
            if len(parts) < 2:
                print("Usage: import <private_key>")
                continue
            manager.import_wallet(parts[1])

        elif action == "send_all":
            if len(parts) < 3:
                print("Usage: send_all <recipient> <amount>")
                continue
            try:
                manager.send_all_wallets(parts[1], float(parts[2]))
            except ValueError:
                print("Invalid amount.")

        elif action == "send_selected":
            if len(parts) < 4:
                print("Usage: send_selected <recipient> <amount> <1,2,3>")
                continue
            try:
                indices = [int(x) for x in parts[3].split(",")]
                manager.send_selected_wallets(parts[1], float(parts[2]), indices)
            except ValueError:
                print("Invalid indices or amount. Format: 1,2,3")

        elif action == "send_manual":
            if len(parts) < 2:
                print("Usage: send_manual <recipient>")
                continue
            manager.send_manual_wallets(parts[1])

        elif action == "tx":
            if len(parts) < 2:
                print("Usage: tx <signature>")
                continue
            manager.show_tx(parts[1])

        elif action == "last_tx":
            if len(parts) < 2:
                print("Usage: last_tx <N>")
                continue
            try:
                manager.get_last_tx(int(parts[1]))
            except ValueError:
                print("Please enter a wallet number.")

        elif action == "export":
            filepath = parts[1] if len(parts) > 1 else None
            manager.export_wallets(filepath)

        elif action == "delete_all":
            manager.delete_all_wallets()

        elif action == "rpc":
            if len(parts) < 2:
                print("Usage: rpc <url|devnet|mainnet>")
                continue
            url = parts[1]
            if url == "mainnet":
                url = "https://api.mainnet-beta.solana.com"
            elif url == "devnet":
                url = "https://api.devnet.solana.com"
            manager.rpc_url = url
            manager.client = Client(url)
            print(f"RPC updated: {url}")

        else:
            print(f"Unknown command: '{action}'. Type 'help' for a list.")


# ==================== ENTRY POINT ====================

def main():
    manager = SolanaWalletManager()

    if len(sys.argv) > 1:
        args = sys.argv[1:]
        action = args[0].lower()

        if action == "create":
            manager.create_wallets(int(args[1]))
        elif action == "list":
            manager.list_wallets()
        elif action == "balance":
            manager.check_external_balance(args[1])
        elif action == "show_key":
            manager.show_key(int(args[1]))
        elif action == "show_all_keys":
            manager.show_all_keys()
        elif action == "import":
            manager.import_wallet(args[1])
        elif action == "send_all":
            manager.send_all_wallets(args[1], float(args[2]))
        elif action == "send_selected":
            indices = [int(x) for x in args[3].split(",")]
            manager.send_selected_wallets(args[1], float(args[2]), indices)
        elif action == "send_manual":
            manager.send_manual_wallets(args[1])
        elif action == "tx":
            manager.show_tx(args[1])
        elif action == "last_tx":
            manager.get_last_tx(int(args[1]))
        elif action == "export":
            manager.export_wallets(args[1] if len(args) > 1 else None)
        elif action == "help":
            print_usage()
        else:
            print(f"Unknown command: {action}")
            print_usage()
    else:
        interactive_mode(manager)


if __name__ == "__main__":
    main()
