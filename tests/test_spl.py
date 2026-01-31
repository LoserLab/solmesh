"""Tests for SPL token instruction builders."""

import struct

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from solmesh.spl import (
    find_associated_token_address,
    create_associated_token_account_instruction,
    transfer_checked_instruction,
    SPL_TOKEN_PROGRAM,
    ASSOCIATED_TOKEN_PROGRAM,
    SYSTEM_PROGRAM,
)


class TestFindATA:
    def test_deterministic(self):
        wallet = Keypair().pubkey()
        mint = Keypair().pubkey()
        ata1 = find_associated_token_address(wallet, mint)
        ata2 = find_associated_token_address(wallet, mint)
        assert ata1 == ata2

    def test_different_wallets_different_atas(self):
        mint = Keypair().pubkey()
        ata1 = find_associated_token_address(Keypair().pubkey(), mint)
        ata2 = find_associated_token_address(Keypair().pubkey(), mint)
        assert ata1 != ata2

    def test_different_mints_different_atas(self):
        wallet = Keypair().pubkey()
        ata1 = find_associated_token_address(wallet, Keypair().pubkey())
        ata2 = find_associated_token_address(wallet, Keypair().pubkey())
        assert ata1 != ata2

    def test_ata_is_32_bytes(self):
        wallet = Keypair().pubkey()
        mint = Keypair().pubkey()
        ata = find_associated_token_address(wallet, mint)
        assert len(bytes(ata)) == 32


class TestCreateATAInstruction:
    def test_accounts_layout(self):
        payer = Keypair().pubkey()
        wallet = Keypair().pubkey()
        mint = Keypair().pubkey()
        ix = create_associated_token_account_instruction(payer, wallet, mint)
        assert ix.program_id == ASSOCIATED_TOKEN_PROGRAM
        assert len(ix.accounts) == 6
        assert ix.accounts[0].pubkey == payer
        assert ix.accounts[0].is_signer is True
        assert ix.data == b""

    def test_ata_is_writable(self):
        payer = Keypair().pubkey()
        wallet = Keypair().pubkey()
        mint = Keypair().pubkey()
        ix = create_associated_token_account_instruction(payer, wallet, mint)
        assert ix.accounts[1].is_writable is True

    def test_includes_system_and_token_programs(self):
        payer = Keypair().pubkey()
        wallet = Keypair().pubkey()
        mint = Keypair().pubkey()
        ix = create_associated_token_account_instruction(payer, wallet, mint)
        assert ix.accounts[4].pubkey == SYSTEM_PROGRAM
        assert ix.accounts[5].pubkey == SPL_TOKEN_PROGRAM


class TestTransferCheckedInstruction:
    def test_instruction_data_format(self):
        source = Keypair().pubkey()
        mint = Keypair().pubkey()
        dest = Keypair().pubkey()
        owner = Keypair().pubkey()
        amount = 1_000_000  # 1 USDC
        decimals = 6

        ix = transfer_checked_instruction(source, mint, dest, owner, amount, decimals)

        # Verify instruction discriminator is 12
        assert ix.data[0] == 12
        # Verify amount (little-endian u64)
        parsed_amount = struct.unpack("<Q", ix.data[1:9])[0]
        assert parsed_amount == amount
        # Verify decimals
        assert ix.data[9] == decimals
        # Total data length: 1 + 8 + 1 = 10
        assert len(ix.data) == 10

    def test_accounts_layout(self):
        source = Keypair().pubkey()
        mint = Keypair().pubkey()
        dest = Keypair().pubkey()
        owner = Keypair().pubkey()

        ix = transfer_checked_instruction(source, mint, dest, owner, 100, 6)
        assert ix.program_id == SPL_TOKEN_PROGRAM
        assert len(ix.accounts) == 4
        assert ix.accounts[0].pubkey == source   # source ATA
        assert ix.accounts[1].pubkey == mint      # mint
        assert ix.accounts[2].pubkey == dest      # dest ATA
        assert ix.accounts[3].pubkey == owner     # owner/signer
        assert ix.accounts[3].is_signer is True

    def test_source_and_dest_writable(self):
        source = Keypair().pubkey()
        mint = Keypair().pubkey()
        dest = Keypair().pubkey()
        owner = Keypair().pubkey()

        ix = transfer_checked_instruction(source, mint, dest, owner, 100, 6)
        assert ix.accounts[0].is_writable is True
        assert ix.accounts[2].is_writable is True
        assert ix.accounts[1].is_writable is False  # mint is read-only

    def test_large_amount(self):
        source = Keypair().pubkey()
        mint = Keypair().pubkey()
        dest = Keypair().pubkey()
        owner = Keypair().pubkey()
        amount = 10**18  # very large amount

        ix = transfer_checked_instruction(source, mint, dest, owner, amount, 9)
        parsed = struct.unpack("<Q", ix.data[1:9])[0]
        assert parsed == amount
        assert ix.data[9] == 9
