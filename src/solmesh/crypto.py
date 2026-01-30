"""Payload signing and verification for SolMesh message authentication.

Uses Ed25519 signatures via the Solana keypair to authenticate protocol
messages (e.g., proving a TX_REQUEST came from a known node).
This is distinct from Solana transaction signing.
"""

import hashlib

from solders.keypair import Keypair
from solders.pubkey import Pubkey


def sign_payload(keypair: Keypair, payload: bytes) -> bytes:
    """Sign a protocol payload with a Solana Ed25519 keypair.

    Returns a 64-byte signature.
    """
    sig = keypair.sign_message(payload)
    return bytes(sig)


def verify_payload(pubkey: Pubkey, payload: bytes, signature: bytes) -> bool:
    """Verify that a payload was signed by the holder of the given pubkey."""
    from solders.signature import Signature

    try:
        sig = Signature.from_bytes(signature)
        return sig.verify(pubkey, payload)
    except Exception:
        return False


def compute_payload_hash(payload: bytes) -> bytes:
    """SHA-256 hash of a payload for compact referencing."""
    return hashlib.sha256(payload).digest()
