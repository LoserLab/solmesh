"""Click CLI for SolMesh."""

import logging
import sys
from pathlib import Path
from typing import Optional

import click

from solmesh.config import SolMeshConfig, get_rpc_url, load_config
from solmesh.constants import LAMPORTS_PER_SOL
from solmesh.mesh import MeshInterface
from solmesh.wallet import WalletManager


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def build_mesh(config: SolMeshConfig) -> MeshInterface:
    return MeshInterface(
        connection_type=config.mesh.connection_type,
        device_path=config.mesh.device_path,
        hostname=config.mesh.hostname,
    )


@click.group()
@click.option("--config", "-c", "config_path", type=click.Path(exists=True),
              default=None, help="Path to config YAML file")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx, config_path, verbose):
    """SolMesh: Solana transactions over Meshtastic mesh networks."""
    ctx.ensure_object(dict)

    if config_path:
        config = load_config(Path(config_path))
    else:
        config = SolMeshConfig()

    if verbose:
        config.log_level = "DEBUG"

    setup_logging(config.log_level)
    ctx.obj["config"] = config


# --- Gateway command ---

@cli.command()
@click.option("--rpc-url", help="Solana RPC URL (overrides config)")
@click.option("--hot-wallet", help="Wallet name for Mode 3 gateway transfers")
@click.option("--passphrase", prompt=False, hide_input=True, default="",
              help="Passphrase for hot wallet (prompted if hot-wallet is set)")
@click.option("--beacon-interval", type=int, default=None,
              help="Beacon broadcast interval in seconds (default: 60)")
@click.pass_context
def gateway(ctx, rpc_url, hot_wallet, passphrase, beacon_interval):
    """Run as a gateway node (internet-connected, relays to Solana)."""
    from solmesh.gateway import GatewayNode

    config = ctx.obj["config"]

    if rpc_url:
        config.solana.rpc_url = rpc_url
    if hot_wallet:
        config.gateway.hot_wallet = hot_wallet
    if beacon_interval is not None:
        config.gateway.beacon_interval = beacon_interval

    # Prompt for passphrase if hot wallet is set but passphrase wasn't provided
    if config.gateway.hot_wallet and not passphrase:
        passphrase = click.prompt("Hot wallet passphrase", hide_input=True, default="")

    resolved_rpc = get_rpc_url(config.solana)
    mesh = build_mesh(config)
    wm = WalletManager()

    gw = GatewayNode(
        mesh=mesh,
        rpc_url=resolved_rpc,
        wallet_manager=wm,
        gateway_config=config.gateway,
    )

    click.echo(f"Starting gateway node...")
    click.echo(f"  Solana RPC: {resolved_rpc}")
    click.echo(f"  Network:    {config.solana.network}")
    if config.gateway.hot_wallet:
        click.echo(f"  Hot wallet: {config.gateway.hot_wallet}")
        click.echo(f"  Max transfer: {config.gateway.max_transfer_sol} SOL")
    click.echo()

    gw.start(hot_wallet_passphrase=passphrase)


# --- Send commands ---

@cli.group()
@click.pass_context
def send(ctx):
    """Send Solana transactions over the mesh."""
    pass


@send.command("relay")
@click.option("--wallet", "-w", required=True, help="Local wallet name")
@click.option("--to", "recipient", required=True, help="Recipient Solana address")
@click.option("--amount", "-a", required=True, type=float, help="Amount in SOL")
@click.option("--blockhash", default=None, help="Recent blockhash (base58). Auto-fetched from gateway if omitted.")
@click.option("--gateway-node", "-g", help="Gateway mesh node ID (e.g., !aabbccdd)")
@click.option("--auto-discover", is_flag=True, help="Auto-discover gateway via beacon")
@click.option("--passphrase", prompt=True, hide_input=True, default="",
              help="Wallet passphrase")
@click.pass_context
def send_relay(ctx, wallet, recipient, amount, blockhash, gateway_node, auto_discover, passphrase):
    """Mode 1: Sign locally and relay signed TX over mesh to gateway."""
    from solmesh.client import ClientNode

    config = ctx.obj["config"]
    mesh = build_mesh(config)
    wm = WalletManager()

    client = ClientNode(mesh=mesh, wallet_manager=wm, gateway_node_id=gateway_node)
    client.connect()

    if auto_discover and not gateway_node:
        click.echo("Discovering gateway via beacon...")
        gw = client.discover_gateway(timeout=120)
        if not gw:
            click.echo("No gateway found. Specify --gateway-node or try again.", err=True)
            client.close()
            sys.exit(1)
        click.echo(f"  Found gateway: {gw}")

    click.echo(f"Signing transaction locally...")
    click.echo(f"  From:   {wallet}")
    click.echo(f"  To:     {recipient}")
    click.echo(f"  Amount: {amount} SOL")
    if not blockhash:
        click.echo(f"  Blockhash: auto-fetching from gateway...")
    click.echo()

    try:
        msg_id = client.relay_signed_tx(
            wallet_name=wallet,
            recipient=recipient,
            amount_sol=amount,
            blockhash=blockhash,
            passphrase=passphrase,
        )
        click.echo(f"Transaction sent (msg_id={msg_id}). Waiting for result...")

        result = client.wait_for_result(msg_id)
        if result and result.get("success"):
            click.echo(f"Success! TX signature: {result['signature']}")
        elif result:
            click.echo(f"Failed: {result.get('error', 'Unknown error')}")
        else:
            click.echo("Timed out waiting for result from gateway.")
    finally:
        client.close()


@send.command("request")
@click.option("--wallet", "-w", required=True, help="Your wallet name (for auth)")
@click.option("--to", "recipient", required=True, help="Recipient Solana address")
@click.option("--amount", "-a", required=True, type=float, help="Amount in SOL")
@click.option("--gateway-node", "-g", default=None, help="Gateway mesh node ID")
@click.option("--auto-discover", is_flag=True, help="Auto-discover gateway via beacon")
@click.option("--passphrase", prompt=True, hide_input=True, default="",
              help="Wallet passphrase")
@click.pass_context
def send_request(ctx, wallet, recipient, amount, gateway_node, auto_discover, passphrase):
    """Mode 3: Request gateway to send SOL from its hot wallet."""
    from solmesh.client import ClientNode

    config = ctx.obj["config"]
    mesh = build_mesh(config)
    wm = WalletManager()

    client = ClientNode(mesh=mesh, wallet_manager=wm, gateway_node_id=gateway_node)
    client.connect()

    if auto_discover and not gateway_node:
        click.echo("Discovering gateway via beacon...")
        gw = client.discover_gateway(timeout=120)
        if not gw:
            click.echo("No gateway found. Specify --gateway-node or try again.", err=True)
            client.close()
            sys.exit(1)
        click.echo(f"  Found gateway: {gw}")

    click.echo(f"Requesting gateway transfer...")
    click.echo(f"  To:     {recipient}")
    click.echo(f"  Amount: {amount} SOL")
    click.echo()

    try:
        msg_id = client.request_transfer(
            wallet_name=wallet,
            destination=recipient,
            amount_sol=amount,
            passphrase=passphrase,
        )
        click.echo(f"Request sent (msg_id={msg_id}). Waiting for result...")

        result = client.wait_for_result(msg_id)
        if result and result.get("success"):
            click.echo(f"Success! TX signature: {result['signature']}")
        elif result:
            click.echo(f"Failed: {result.get('error', 'Unknown error')}")
        else:
            click.echo("Timed out waiting for result from gateway.")
    finally:
        client.close()


# --- Address sharing ---

@cli.command("share-address")
@click.option("--wallet", "-w", required=True, help="Wallet name")
@click.option("--label", "-l", default="", help="Optional label for your address")
@click.pass_context
def share_address(ctx, wallet, label):
    """Mode 2: Share your Solana address over mesh."""
    from solmesh.client import ClientNode

    config = ctx.obj["config"]
    mesh = build_mesh(config)
    wm = WalletManager()

    client = ClientNode(mesh=mesh, wallet_manager=wm)
    client.connect()

    try:
        client.share_address(wallet_name=wallet, label=label)
        pubkey = wm.get_pubkey(wallet)
        click.echo(f"Address broadcast: {pubkey}")
    finally:
        client.close()


# --- Balance check ---

@cli.command("balance")
@click.option("--address", "-a", required=True, help="Solana address to check")
@click.option("--gateway-node", "-g", default=None, help="Gateway mesh node ID")
@click.option("--auto-discover", is_flag=True, help="Auto-discover gateway via beacon")
@click.pass_context
def check_balance(ctx, address, gateway_node, auto_discover):
    """Query SOL balance via gateway."""
    from solmesh.client import ClientNode

    config = ctx.obj["config"]
    mesh = build_mesh(config)
    wm = WalletManager()

    client = ClientNode(mesh=mesh, wallet_manager=wm, gateway_node_id=gateway_node)
    client.connect()

    if auto_discover and not gateway_node:
        click.echo("Discovering gateway via beacon...")
        gw = client.discover_gateway(timeout=120)
        if not gw:
            click.echo("No gateway found. Specify --gateway-node or try again.", err=True)
            client.close()
            sys.exit(1)
        click.echo(f"  Found gateway: {gw}")

    try:
        client.check_balance(address)
        click.echo(f"Balance request sent. Waiting for response...")

        result = client.wait_for_balance(timeout=60)
        if result:
            click.echo(f"Address: {result['pubkey']}")
            click.echo(f"Balance: {result['sol']:.9f} SOL ({result['lamports']} lamports)")
        else:
            click.echo("Timed out waiting for balance response.")
    finally:
        client.close()


# --- Wallet management ---

@cli.group()
def wallet():
    """Manage local Solana wallets."""
    pass


@wallet.command("create")
@click.option("--name", "-n", required=True, help="Wallet name")
@click.option("--no-mnemonic", is_flag=True, help="Skip mnemonic generation (random keypair)")
@click.option("--passphrase", prompt=True, hide_input=True,
              confirmation_prompt=True,
              help="Encryption passphrase (required)")
def wallet_create(name, no_mnemonic, passphrase):
    """Create a new Solana wallet (BIP39 mnemonic by default)."""
    if not passphrase:
        click.echo("Error: A passphrase is required to protect your private key.", err=True)
        sys.exit(1)
    wm = WalletManager()
    try:
        if no_mnemonic:
            pubkey = wm.create_wallet(name, passphrase=passphrase)
            click.echo(f"Wallet created: {name}")
            click.echo(f"Public key:     {pubkey}")
        else:
            pubkey, mnemonic = wm.create_wallet_with_mnemonic(name, passphrase=passphrase)
            click.echo(f"Wallet created: {name}")
            click.echo(f"Public key:     {pubkey}")
            click.echo()
            click.echo("RECOVERY PHRASE (write this down and store securely!):")
            click.echo(f"  {mnemonic}")
            click.echo()
            click.echo("WARNING: This phrase will NOT be shown again.")
            click.echo("         Anyone with this phrase can access your funds.")
    except FileExistsError:
        click.echo(f"Error: Wallet '{name}' already exists.", err=True)
        sys.exit(1)


@wallet.command("recover")
@click.option("--name", "-n", required=True, help="Wallet name")
@click.option("--mnemonic", "mnemonic_phrase", prompt=True, hide_input=True,
              help="BIP39 recovery phrase (24 words)")
@click.option("--passphrase", prompt=True, hide_input=True,
              confirmation_prompt=True,
              help="Encryption passphrase for the recovered wallet")
def wallet_recover(name, mnemonic_phrase, passphrase):
    """Recover a wallet from a BIP39 mnemonic phrase."""
    if not passphrase:
        click.echo("Error: A passphrase is required to protect your private key.", err=True)
        sys.exit(1)
    wm = WalletManager()
    try:
        pubkey = wm.recover_wallet(name, mnemonic_phrase, passphrase=passphrase)
        click.echo(f"Wallet recovered: {name}")
        click.echo(f"Public key:       {pubkey}")
    except FileExistsError:
        click.echo(f"Error: Wallet '{name}' already exists.", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@wallet.command("import")
@click.option("--name", "-n", required=True, help="Wallet name")
@click.option("--secret-key", prompt=True, hide_input=True,
              help="Base58 or hex-encoded secret key")
@click.option("--passphrase", prompt=True, hide_input=True,
              confirmation_prompt=True, default="",
              help="Encryption passphrase (press enter to skip)")
def wallet_import(name, secret_key, passphrase):
    """Import an existing Solana wallet from a private key."""
    import base58

    wm = WalletManager()
    try:
        # Try base58 first, then hex
        try:
            key_bytes = base58.b58decode(secret_key)
        except Exception:
            key_bytes = bytes.fromhex(secret_key)

        pubkey = wm.import_wallet(name, key_bytes, passphrase=passphrase)
        click.echo(f"Wallet imported: {name}")
        click.echo(f"Public key:      {pubkey}")
    except FileExistsError:
        click.echo(f"Error: Wallet '{name}' already exists.", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@wallet.command("list")
def wallet_list():
    """List all local wallets."""
    wm = WalletManager()
    wallets = wm.list_wallets()
    if not wallets:
        click.echo("No wallets found. Create one with: solmesh wallet create --name <name>")
        return

    click.echo(f"{'Name':<20} {'Public Key':<50} {'Encrypted'}")
    click.echo("-" * 80)
    for w in wallets:
        enc = "yes" if w["encrypted"] else "no"
        click.echo(f"{w['name']:<20} {w['pubkey']:<50} {enc}")


@wallet.command("delete")
@click.option("--name", "-n", required=True, help="Wallet name")
@click.confirmation_option(prompt="Are you sure you want to delete this wallet?")
def wallet_delete(name):
    """Delete a local wallet."""
    wm = WalletManager()
    try:
        wm.delete_wallet(name)
        click.echo(f"Wallet '{name}' deleted.")
    except FileNotFoundError:
        click.echo(f"Error: Wallet '{name}' not found.", err=True)
        sys.exit(1)


def main():
    cli(obj={})


if __name__ == "__main__":
    main()
