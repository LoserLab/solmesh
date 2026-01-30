"""Tests for payload signing and verification."""

from solders.keypair import Keypair

from solmesh.crypto import sign_payload, verify_payload, compute_payload_hash


class TestSignVerify:
    def test_round_trip(self):
        kp = Keypair()
        payload = b"transfer 1 SOL to abc123"
        sig = sign_payload(kp, payload)
        assert len(sig) == 64
        assert verify_payload(kp.pubkey(), payload, sig) is True

    def test_wrong_pubkey(self):
        kp1 = Keypair()
        kp2 = Keypair()
        payload = b"test"
        sig = sign_payload(kp1, payload)
        assert verify_payload(kp2.pubkey(), payload, sig) is False

    def test_tampered_payload(self):
        kp = Keypair()
        payload = b"original"
        sig = sign_payload(kp, payload)
        assert verify_payload(kp.pubkey(), b"tampered", sig) is False

    def test_tampered_signature(self):
        kp = Keypair()
        payload = b"data"
        sig = sign_payload(kp, payload)
        bad_sig = bytes([sig[0] ^ 0xFF]) + sig[1:]
        assert verify_payload(kp.pubkey(), payload, bad_sig) is False

    def test_empty_payload(self):
        kp = Keypair()
        sig = sign_payload(kp, b"")
        assert len(sig) == 64
        assert verify_payload(kp.pubkey(), b"", sig) is True


class TestPayloadHash:
    def test_deterministic(self):
        data = b"hello"
        assert compute_payload_hash(data) == compute_payload_hash(data)

    def test_length(self):
        h = compute_payload_hash(b"test")
        assert len(h) == 32

    def test_different_data(self):
        h1 = compute_payload_hash(b"abc")
        h2 = compute_payload_hash(b"abd")
        assert h1 != h2
