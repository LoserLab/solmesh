"""Tests for SolMesh CLI: token resolution, wallet commands, and help output."""

import pytest
from unittest.mock import patch
from pathlib import Path

import click
from click.testing import CliRunner

from solmesh.cli import _resolve_token, cli
from solmesh.wallet import WalletManager
from solmesh.constants import (
    USDC_MINT_MAINNET,
    USDC_MINT_DEVNET,
    USDC_DECIMALS,
    FXN_MINT_MAINNET,
    FXN_DECIMALS,
)


# --- Token resolution tests ---


class TestResolveToken:
    """Tests for the _resolve_token() helper."""

    def test_native_sol_default(self):
        mint, decimals, symbol = _resolve_token(None, usdc=False, fxn=False)
        assert mint is None
        assert decimals == 9
        assert symbol == "SOL"

    def test_usdc_devnet(self):
        mint, decimals, symbol = _resolve_token(None, usdc=True, fxn=False, network="devnet")
        assert mint == USDC_MINT_DEVNET
        assert decimals == USDC_DECIMALS
        assert symbol == "USDC"

    def test_usdc_mainnet(self):
        mint, decimals, symbol = _resolve_token(None, usdc=True, fxn=False, network="mainnet-beta")
        assert mint == USDC_MINT_MAINNET
        assert decimals == USDC_DECIMALS
        assert symbol == "USDC"

    def test_usdc_testnet(self):
        mint, decimals, symbol = _resolve_token(None, usdc=True, fxn=False, network="testnet")
        assert mint == USDC_MINT_DEVNET
        assert decimals == USDC_DECIMALS

    def test_usdc_unknown_network(self):
        with pytest.raises(click.UsageError, match="No USDC mint configured"):
            _resolve_token(None, usdc=True, fxn=False, network="unknown-net")

    def test_fxn_mainnet(self):
        mint, decimals, symbol = _resolve_token(None, usdc=False, fxn=True, network="mainnet-beta")
        assert mint == FXN_MINT_MAINNET
        assert decimals == FXN_DECIMALS
        assert symbol == "FXN"

    def test_fxn_not_on_devnet(self):
        with pytest.raises(click.UsageError, match="only available on mainnet-beta"):
            _resolve_token(None, usdc=False, fxn=True, network="devnet")

    def test_token_string_usdc(self):
        mint, decimals, symbol = _resolve_token("USDC", usdc=False, fxn=False, network="devnet")
        assert mint == USDC_MINT_DEVNET
        assert symbol == "USDC"

    def test_token_string_fxn(self):
        mint, decimals, symbol = _resolve_token("FXN", usdc=False, fxn=False, network="mainnet-beta")
        assert mint == FXN_MINT_MAINNET
        assert symbol == "FXN"

    def test_custom_mint_address(self):
        custom = "SomeMintAddress1111111111111111111111111111"
        mint, decimals, symbol = _resolve_token(custom, usdc=False, fxn=False)
        assert mint == custom
        assert decimals == 6
        assert "TOKEN" in symbol

    def test_usdc_and_fxn_conflict(self):
        with pytest.raises(click.UsageError, match="Cannot use"):
            _resolve_token(None, usdc=True, fxn=True, network="mainnet-beta")

    def test_usdc_and_token_conflict(self):
        with pytest.raises(click.UsageError, match="Cannot use"):
            _resolve_token("SomeMint", usdc=True, fxn=False)

    def test_fxn_and_token_conflict(self):
        with pytest.raises(click.UsageError, match="Cannot use"):
            _resolve_token("SomeMint", usdc=False, fxn=True)

    def test_all_three_conflict(self):
        with pytest.raises(click.UsageError, match="Cannot use"):
            _resolve_token("SomeMint", usdc=True, fxn=True)


# --- Wallet CLI tests ---


@pytest.fixture()
def isolated_wallets(tmp_path):
    """Patch WalletManager so all instances use a temp directory."""
    wallet_dir = tmp_path / "wallets"
    wallet_dir.mkdir()
    _original_init = WalletManager.__init__

    def _patched_init(self, wallet_dir_arg=None):
        _original_init(self, wallet_dir=wallet_dir)

    with patch.object(WalletManager, "__init__", _patched_init):
        yield wallet_dir


class TestWalletCLI:
    """Tests for wallet CLI commands using Click's CliRunner."""

    def test_wallet_create(self, isolated_wallets):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["wallet", "create", "--name", "testwallet", "--no-mnemonic"],
            input="testpass\ntestpass\n",
        )
        assert result.exit_code == 0, result.output
        assert "Wallet created: testwallet" in result.output

    def test_wallet_create_duplicate(self, isolated_wallets):
        runner = CliRunner()
        runner.invoke(
            cli,
            ["wallet", "create", "--name", "dup", "--no-mnemonic"],
            input="testpass\ntestpass\n",
        )
        result = runner.invoke(
            cli,
            ["wallet", "create", "--name", "dup", "--no-mnemonic"],
            input="testpass\ntestpass\n",
        )
        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_wallet_list_empty(self, isolated_wallets):
        runner = CliRunner()
        result = runner.invoke(cli, ["wallet", "list"])
        assert result.exit_code == 0
        assert "No wallets found" in result.output

    def test_wallet_list_with_wallet(self, isolated_wallets):
        runner = CliRunner()
        runner.invoke(
            cli,
            ["wallet", "create", "--name", "listedwallet", "--no-mnemonic"],
            input="testpass\ntestpass\n",
        )
        result = runner.invoke(cli, ["wallet", "list"])
        assert result.exit_code == 0
        assert "listedwallet" in result.output

    def test_wallet_delete(self, isolated_wallets):
        runner = CliRunner()
        runner.invoke(
            cli,
            ["wallet", "create", "--name", "todelete", "--no-mnemonic"],
            input="testpass\ntestpass\n",
        )
        result = runner.invoke(
            cli, ["wallet", "delete", "--name", "todelete", "--yes"]
        )
        assert result.exit_code == 0
        assert "deleted" in result.output

    def test_wallet_delete_nonexistent(self, isolated_wallets):
        runner = CliRunner()
        result = runner.invoke(
            cli, ["wallet", "delete", "--name", "ghost", "--yes"]
        )
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_wallet_create_with_mnemonic(self, isolated_wallets):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["wallet", "create", "--name", "mnemonicwallet"],
            input="testpass\ntestpass\n",
        )
        assert result.exit_code == 0, result.output
        assert "RECOVERY PHRASE" in result.output
        assert "mnemonicwallet" in result.output


# --- Help output tests ---


class TestHelpOutput:
    """Verify --usdc and --fxn flags appear in help for all send commands and balance."""

    def test_send_relay_has_usdc_and_fxn(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["send", "relay", "--help"])
        assert "--usdc" in result.output
        assert "--fxn" in result.output

    def test_send_request_has_usdc_and_fxn(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["send", "request", "--help"])
        assert "--usdc" in result.output
        assert "--fxn" in result.output

    def test_send_deferred_has_usdc_and_fxn(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["send", "deferred", "--help"])
        assert "--usdc" in result.output
        assert "--fxn" in result.output

    def test_balance_has_usdc_and_fxn(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["balance", "--help"])
        assert "--usdc" in result.output
        assert "--fxn" in result.output
