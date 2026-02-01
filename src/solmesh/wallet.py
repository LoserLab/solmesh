"""Solana wallet/keypair management.

Private keys are stored locally on disk, optionally encrypted with a
passphrase. Keys are NEVER transmitted over the air.
"""

from __future__ import annotations
import hashlib
import hmac
import json
import logging
import os
import struct
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.hash import Hash as SolHash
from solders.system_program import TransferParams, transfer
from solders.message import MessageV0
from solders.transaction import VersionedTransaction

logger = logging.getLogger(__name__)

DEFAULT_WALLET_DIR = Path.home() / ".solmesh" / "wallets"
PBKDF2_ITERATIONS = 480_000
SALT_SIZE = 16
NONCE_SIZE = 12


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from a passphrase using PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def _encrypt_secret(secret_bytes: bytes, passphrase: str) -> dict:
    """Encrypt secret key bytes with AES-256-GCM."""
    salt = os.urandom(SALT_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    key = _derive_key(passphrase, salt)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, secret_bytes, None)
    return {
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "ciphertext": ciphertext.hex(),
    }


def _decrypt_secret(enc_data: dict, passphrase: str) -> bytes:
    """Decrypt secret key bytes from AES-256-GCM."""
    salt = bytes.fromhex(enc_data["salt"])
    nonce = bytes.fromhex(enc_data["nonce"])
    ciphertext = bytes.fromhex(enc_data["ciphertext"])
    key = _derive_key(passphrase, salt)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, None)


# Solana BIP44 derivation path: m/44'/501'/0'/0'
SOLANA_DERIVATION_PATH = [44, 501, 0, 0]


def _derive_solana_keypair_from_mnemonic(mnemonic_phrase: str,
                                         bip39_passphrase: str = "") -> Keypair:
    """Derive a Solana keypair from a BIP39 mnemonic using SLIP-0010.

    Uses derivation path m/44'/501'/0'/0' (Solana standard).
    """
    from mnemonic import Mnemonic

    m = Mnemonic("english")
    if not m.check(mnemonic_phrase):
        raise ValueError("Invalid BIP39 mnemonic phrase")

    # BIP39 seed derivation
    seed = hashlib.pbkdf2_hmac(
        "sha512",
        mnemonic_phrase.encode("utf-8"),
        ("mnemonic" + bip39_passphrase).encode("utf-8"),
        2048,
    )

    # SLIP-0010 master key derivation for Ed25519
    I = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    key = I[:32]
    chain_code = I[32:]

    # Derive child keys along the path (all hardened)
    for index in SOLANA_DERIVATION_PATH:
        hardened_index = 0x80000000 + index
        data = b"\x00" + key + struct.pack(">I", hardened_index)
        I = hmac.new(chain_code, data, hashlib.sha512).digest()
        key = I[:32]
        chain_code = I[32:]

    # key is the 32-byte Ed25519 private key seed
    return Keypair.from_seed(key)


class WalletManager:
    """Manages Solana keypairs stored locally on disk."""

    def __init__(self, wallet_dir: Path = DEFAULT_WALLET_DIR):
        self._wallet_dir = wallet_dir
        self._wallet_dir.mkdir(parents=True, exist_ok=True)
        # Restrict wallet directory to owner-only access
        os.chmod(self._wallet_dir, 0o700)

    def _wallet_path(self, name: str) -> Path:
        return self._wallet_dir / f"{name}.json"

    def create_wallet(self, name: str, passphrase: str = "") -> Pubkey:
        """Generate a new Solana keypair and save to disk."""
        path = self._wallet_path(name)
        if path.exists():
            raise FileExistsError(f"Wallet '{name}' already exists")

        kp = Keypair()
        self._save_keypair(name, kp, passphrase)
        logger.info("Created wallet '%s': %s", name, kp.pubkey())
        return kp.pubkey()

    def create_wallet_with_mnemonic(self, name: str,
                                     passphrase: str = "") -> tuple[Pubkey, str]:
        """Generate a new wallet backed by a BIP39 mnemonic.

        Returns (pubkey, mnemonic_phrase). The mnemonic is NOT stored --
        display it once and tell the user to write it down.
        """
        from mnemonic import Mnemonic

        path = self._wallet_path(name)
        if path.exists():
            raise FileExistsError(f"Wallet '{name}' already exists")

        m = Mnemonic("english")
        mnemonic_phrase = m.generate(strength=256)  # 24 words
        kp = _derive_solana_keypair_from_mnemonic(mnemonic_phrase)
        self._save_keypair(name, kp, passphrase)
        logger.info("Created mnemonic-backed wallet '%s': %s", name, kp.pubkey())
        return kp.pubkey(), mnemonic_phrase

    def recover_wallet(self, name: str, mnemonic_phrase: str,
                       passphrase: str = "") -> Pubkey:
        """Recover a wallet from a BIP39 mnemonic phrase."""
        path = self._wallet_path(name)
        if path.exists():
            raise FileExistsError(f"Wallet '{name}' already exists")

        kp = _derive_solana_keypair_from_mnemonic(mnemonic_phrase)
        self._save_keypair(name, kp, passphrase)
        logger.info("Recovered wallet '%s': %s", name, kp.pubkey())
        return kp.pubkey()

    def import_wallet(self, name: str, secret_key: bytes,
                      passphrase: str = "") -> Pubkey:
        """Import an existing keypair from raw 64-byte secret key."""
        path = self._wallet_path(name)
        if path.exists():
            raise FileExistsError(f"Wallet '{name}' already exists")

        kp = Keypair.from_bytes(secret_key)
        self._save_keypair(name, kp, passphrase)
        logger.info("Imported wallet '%s': %s", name, kp.pubkey())
        return kp.pubkey()

    def load_keypair(self, name: str, passphrase: str = "") -> Keypair:
        """Load a full keypair from disk. Decrypts the encrypted wallet file."""
        path = self._wallet_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Wallet '{name}' not found")

        with open(path) as f:
            data = json.load(f)

        if not data.get("encrypted"):
            raise ValueError(
                f"Wallet '{name}' is not encrypted. "
                "Re-create it with a passphrase using: "
                "solmesh wallet create --name <name>"
            )
        secret_bytes = _decrypt_secret(data["secret"], passphrase)

        return Keypair.from_bytes(secret_bytes)

    def get_pubkey(self, name: str) -> Pubkey:
        """Load only the public key (no passphrase needed)."""
        path = self._wallet_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Wallet '{name}' not found")

        with open(path) as f:
            data = json.load(f)

        return Pubkey.from_string(data["pubkey"])

    def list_wallets(self) -> list[dict]:
        """List all wallet names and their public keys."""
        wallets = []
        for path in sorted(self._wallet_dir.glob("*.json")):
            with open(path) as f:
                data = json.load(f)
            wallets.append({
                "name": path.stem,
                "pubkey": data["pubkey"],
                "encrypted": data.get("encrypted", False),
            })
        return wallets

    def delete_wallet(self, name: str) -> None:
        """Delete a wallet file."""
        path = self._wallet_path(name)
        if not path.exists():
            raise FileNotFoundError(f"Wallet '{name}' not found")
        path.unlink()
        logger.info("Deleted wallet '%s'", name)

    def _save_keypair(self, name: str, kp: Keypair, passphrase: str) -> None:
        """Save a keypair to disk. Always encrypted -- passphrase is required."""
        if not passphrase:
            raise ValueError(
                "A passphrase is required to protect your private key. "
                "Wallet files are never stored unencrypted."
            )

        secret_bytes = bytes(kp)
        data = {
            "pubkey": str(kp.pubkey()),
            "encrypted": True,
            "secret": _encrypt_secret(secret_bytes, passphrase),
        }

        path = self._wallet_path(name)
        # Write with restrictive permissions: create file as owner-only
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)


def create_sol_transfer(sender_keypair: Keypair, recipient_pubkey: Pubkey,
                        lamports: int, recent_blockhash: SolHash) -> bytes:
    """Create, sign, and serialize a SOL transfer transaction.

    Returns raw serialized bytes ready for chunking.
    Private key never leaves this function.
    """
    ix = transfer(TransferParams(
        from_pubkey=sender_keypair.pubkey(),
        to_pubkey=recipient_pubkey,
        lamports=lamports,
    ))
    msg = MessageV0.try_compile(
        payer=sender_keypair.pubkey(),
        instructions=[ix],
        address_lookup_table_accounts=[],
        recent_blockhash=recent_blockhash,
    )
    tx = VersionedTransaction(msg, [sender_keypair])
    return bytes(tx)


def deserialize_transaction(raw: bytes) -> VersionedTransaction:
    """Deserialize a transaction from raw bytes."""
    return VersionedTransaction.from_bytes(raw)
