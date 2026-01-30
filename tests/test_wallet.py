"""Tests for wallet management."""

import os
import pytest
from pathlib import Path

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from solmesh.wallet import WalletManager


@pytest.fixture
def wallet_dir(tmp_path):
    return tmp_path / "wallets"


@pytest.fixture
def wm(wallet_dir):
    return WalletManager(wallet_dir=wallet_dir)


class TestWalletCreate:
    def test_create_encrypted(self, wm):
        pubkey = wm.create_wallet("secure", passphrase="mypass123")
        assert isinstance(pubkey, Pubkey)
        wallets = wm.list_wallets()
        assert len(wallets) == 1
        assert wallets[0]["name"] == "secure"
        assert wallets[0]["encrypted"] is True

    def test_create_requires_passphrase(self, wm):
        with pytest.raises(ValueError, match="passphrase is required"):
            wm.create_wallet("test")

    def test_create_duplicate(self, wm):
        wm.create_wallet("dupe", passphrase="pass")
        with pytest.raises(FileExistsError):
            wm.create_wallet("dupe", passphrase="pass")

    def test_file_permissions(self, wm, wallet_dir):
        wm.create_wallet("perms", passphrase="pass")
        path = wallet_dir / "perms.json"
        mode = oct(path.stat().st_mode & 0o777)
        assert mode == "0o600"


class TestWalletLoad:
    def test_load_encrypted(self, wm):
        pubkey = wm.create_wallet("secure", passphrase="pass")
        kp = wm.load_keypair("secure", passphrase="pass")
        assert kp.pubkey() == pubkey

    def test_load_wrong_passphrase(self, wm):
        wm.create_wallet("secure", passphrase="correct")
        with pytest.raises(Exception):
            wm.load_keypair("secure", passphrase="wrong")

    def test_load_nonexistent(self, wm):
        with pytest.raises(FileNotFoundError):
            wm.load_keypair("nope")


class TestWalletPubkey:
    def test_get_pubkey(self, wm):
        expected = wm.create_wallet("test", passphrase="pass")
        pubkey = wm.get_pubkey("test")
        assert pubkey == expected

    def test_get_pubkey_nonexistent(self, wm):
        with pytest.raises(FileNotFoundError):
            wm.get_pubkey("nope")


class TestWalletImport:
    def test_import(self, wm):
        kp = Keypair()
        secret = bytes(kp)
        pubkey = wm.import_wallet("imported", secret, passphrase="pw")
        assert pubkey == kp.pubkey()

        loaded = wm.load_keypair("imported", passphrase="pw")
        assert loaded.pubkey() == kp.pubkey()

    def test_import_requires_passphrase(self, wm):
        kp = Keypair()
        secret = bytes(kp)
        with pytest.raises(ValueError, match="passphrase is required"):
            wm.import_wallet("imported", secret)


class TestWalletMnemonic:
    def test_create_with_mnemonic(self, wm):
        pubkey, mnemonic = wm.create_wallet_with_mnemonic("mnem", passphrase="pass")
        assert isinstance(pubkey, Pubkey)
        words = mnemonic.split()
        assert len(words) == 24

    def test_recover_produces_same_pubkey(self, wm):
        pubkey1, mnemonic = wm.create_wallet_with_mnemonic("original", passphrase="pass")
        recovered_pubkey = wm.recover_wallet("recovered", mnemonic, passphrase="pass2")
        assert recovered_pubkey == pubkey1

    def test_deterministic_derivation(self, wm):
        """Same mnemonic always produces same keypair."""
        from solmesh.wallet import _derive_solana_keypair_from_mnemonic
        from mnemonic import Mnemonic
        m = Mnemonic("english")
        phrase = m.generate(strength=256)
        kp1 = _derive_solana_keypair_from_mnemonic(phrase)
        kp2 = _derive_solana_keypair_from_mnemonic(phrase)
        assert kp1.pubkey() == kp2.pubkey()

    def test_invalid_mnemonic(self, wm):
        with pytest.raises(ValueError, match="Invalid BIP39"):
            wm.recover_wallet("bad", "not a valid mnemonic phrase", passphrase="pass")

    def test_recover_requires_passphrase(self, wm):
        from mnemonic import Mnemonic
        m = Mnemonic("english")
        phrase = m.generate(strength=256)
        with pytest.raises(ValueError, match="passphrase is required"):
            wm.recover_wallet("test", phrase)


class TestWalletList:
    def test_empty(self, wm):
        assert wm.list_wallets() == []

    def test_multiple(self, wm):
        wm.create_wallet("alice", passphrase="pass1")
        wm.create_wallet("bob", passphrase="pass2")
        wallets = wm.list_wallets()
        names = [w["name"] for w in wallets]
        assert "alice" in names
        assert "bob" in names


class TestWalletDelete:
    def test_delete(self, wm):
        wm.create_wallet("temp", passphrase="pass")
        wm.delete_wallet("temp")
        assert wm.list_wallets() == []

    def test_delete_nonexistent(self, wm):
        with pytest.raises(FileNotFoundError):
            wm.delete_wallet("nope")
