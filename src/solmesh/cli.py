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


def _resolve_token(token_str: str, network: str = "devnet") -> tuple[str, int, str]:
    """Resolve a token identifier to (mint_address, decimals, symbol).

    Accepts 'USDC', 'FXN' (case-insensitive) or a full base58 mint address.
    """
    from solmesh.constants import (
        USDC_MINT_MAINNET, USDC_MINT_DEVNET,
        FXN_MINT_MAINNET, FXN_DECIMALS,
        KNOWN_TOKENS, USDC_DECIMALS,
    )

    upper = token_str.upper()
    if upper == "USDC":
        if network == "mainnet-beta":
            return USDC_MINT_MAINNET, USDC_DECIMALS, "USDC"
        else:
            return USDC_MINT_DEVNET, USDC_DECIMALS, "USDC"

    if upper == "FXN":
        if network != "mainnet-beta":
            raise click.ClickException("FXN is only available on mainnet-beta")
        return FXN_MINT_MAINNET, FXN_DECIMALS, "FXN"

    # Assume it's a raw mint address
    info = KNOWN_TOKENS.get(token_str)
    if info:
        symbol, decimals = info
        return token_str, decimals, symbol

    # Unknown token -- default to 6 decimals
    return token_str, 6, f"TOKEN({token_str[:8]}...)"


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
@click.option("--http-port", type=int, default=None,
              help="Enable HTTP API on this port (requires solmesh[http])")
@click.option("--api-key", default=None,
              help="API key for HTTP API authentication")
@click.pass_context
def gateway(ctx, rpc_url, hot_wallet, passphrase, beacon_interval,
            http_port, api_key):
    """Run as a gateway node (internet-connected, relays to Solana)."""
    from solmesh.gateway import GatewayNode

    config = ctx.obj["config"]

    if rpc_url:
        config.solana.rpc_url = rpc_url
    if hot_wallet:
        config.gateway.hot_wallet = hot_wallet
    if beacon_interval is not None:
        config.gateway.beacon_interval = beacon_interval
    if http_port is not None:
        config.gateway.http_port = http_port
    if api_key is not None:
        config.gateway.api_key = api_key

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
        click.echo(f"  Max transfer: {config.gateway.max_transfer_sol} SOL / {config.gateway.max_transfer_usdc} USDC")
    if config.gateway.http_port:
        click.echo(f"  HTTP API:   port {config.gateway.http_port}")
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
@click.option("--amount", "-a", required=True, type=float, help="Amount (SOL or token units)")
@click.option("--token", default=None, help="Token to send: 'USDC' or a mint address (default: SOL)")
@click.option("--create-ata", is_flag=True, help="Create recipient token account if needed")
@click.option("--blockhash", default=None, help="Recent blockhash (base58). Auto-fetched from gateway if omitted.")
@click.option("--gateway-node", "-g", help="Gateway mesh node ID (e.g., !aabbccdd)")
@click.option("--auto-discover", is_flag=True, help="Auto-discover gateway via beacon")
@click.option("--passphrase", prompt=True, hide_input=True, default="",
              help="Wallet passphrase")
@click.pass_context
def send_relay(ctx, wallet, recipient, amount, token, create_ata, blockhash,
               gateway_node, auto_discover, passphrase):
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

    try:
        if token:
            mint_address, decimals, symbol = _resolve_token(token, config.solana.network)
            click.echo(f"Signing token transaction locally...")
            click.echo(f"  From:   {wallet}")
            click.echo(f"  To:     {recipient}")
            click.echo(f"  Amount: {amount} {symbol}")
            click.echo(f"  Token:  {mint_address}")
            if create_ata:
                click.echo(f"  Creating recipient token account if needed")
            if not blockhash:
                click.echo(f"  Blockhash: auto-fetching from gateway...")
            click.echo()

            msg_id = client.relay_signed_token_tx(
                wallet_name=wallet,
                recipient=recipient,
                mint_address=mint_address,
                amount=amount,
                decimals=decimals,
                blockhash=blockhash,
                passphrase=passphrase,
                create_recipient_ata=create_ata,
            )
        else:
            click.echo(f"Signing transaction locally...")
            click.echo(f"  From:   {wallet}")
            click.echo(f"  To:     {recipient}")
            click.echo(f"  Amount: {amount} SOL")
            if not blockhash:
                click.echo(f"  Blockhash: auto-fetching from gateway...")
            click.echo()

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
@click.option("--amount", "-a", required=True, type=float, help="Amount (SOL or token units)")
@click.option("--token", default=None, help="Token to send: 'USDC' or a mint address (default: SOL)")
@click.option("--gateway-node", "-g", default=None, help="Gateway mesh node ID")
@click.option("--auto-discover", is_flag=True, help="Auto-discover gateway via beacon")
@click.option("--passphrase", prompt=True, hide_input=True, default="",
              help="Wallet passphrase")
@click.pass_context
def send_request(ctx, wallet, recipient, amount, token, gateway_node, auto_discover, passphrase):
    """Mode 3: Request gateway to send from its hot wallet."""
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
        if token:
            mint_address, decimals, symbol = _resolve_token(token, config.solana.network)
            click.echo(f"Requesting gateway token transfer...")
            click.echo(f"  To:     {recipient}")
            click.echo(f"  Amount: {amount} {symbol}")
            click.echo(f"  Token:  {mint_address}")
            click.echo()

            msg_id = client.request_token_transfer(
                wallet_name=wallet,
                destination=recipient,
                mint_address=mint_address,
                amount=amount,
                decimals=decimals,
                passphrase=passphrase,
            )
        else:
            click.echo(f"Requesting gateway transfer...")
            click.echo(f"  To:     {recipient}")
            click.echo(f"  Amount: {amount} SOL")
            click.echo()

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


@send.command("deferred")
@click.option("--wallet", "-w", required=True, help="Local wallet name")
@click.option("--to", "recipient", required=True, help="Recipient Solana address")
@click.option("--amount", "-a", required=True, type=float, help="Amount (SOL or token units)")
@click.option("--token", default=None, help="Token: 'USDC' or mint address (default: SOL)")
@click.option("--mode", "send_mode", type=click.Choice(["1", "3"]), default="3",
              help="Send mode: 1=sign+relay, 3=gateway request (default: 3)")
@click.option("--passphrase", prompt=True, hide_input=True, default="",
              help="Wallet passphrase (validates ownership)")
@click.pass_context
def send_deferred(ctx, wallet, recipient, amount, token, send_mode, passphrase):
    """Queue a transaction intent for later sending (no mesh needed)."""
    from solmesh.client import ClientNode
    from solmesh.store import IntentStore

    config = ctx.obj["config"]
    wm = WalletManager()
    store = IntentStore()

    client = ClientNode(
        mesh=None,
        wallet_manager=wm,
        intent_store=store,
    )

    int_mode = int(send_mode)
    token_mint = None
    token_decimals = 0
    symbol = "SOL"
    if token:
        token_mint, token_decimals, symbol = _resolve_token(token, config.solana.network)

    try:
        intent = client.queue_intent(
            mode=int_mode,
            wallet_name=wallet,
            recipient=recipient,
            amount=amount,
            token_mint=token_mint,
            token_decimals=token_decimals,
            passphrase=passphrase if passphrase else None,
        )
        click.echo(f"Intent queued: {intent.id}")
        click.echo(f"  Mode:   {int_mode}")
        click.echo(f"  From:   {wallet}")
        click.echo(f"  To:     {recipient}")
        click.echo(f"  Amount: {amount} {symbol}")
        click.echo()
        click.echo("Flush with: solmesh queue flush --passphrase ...")
    except FileNotFoundError:
        click.echo(f"Error: Wallet '{wallet}' not found.", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        if "InvalidTag" in type(e).__name__:
            click.echo("Error: Wrong passphrase.", err=True)
            sys.exit(1)
        raise


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
@click.option("--token", default=None, help="Token: 'USDC' or mint address (default: SOL)")
@click.option("--gateway-node", "-g", default=None, help="Gateway mesh node ID")
@click.option("--auto-discover", is_flag=True, help="Auto-discover gateway via beacon")
@click.pass_context
def check_balance(ctx, address, token, gateway_node, auto_discover):
    """Query balance via gateway (SOL or token)."""
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
        if token:
            mint_address, decimals, symbol = _resolve_token(token, config.solana.network)
            client.check_token_balance(address, mint_address)
            click.echo(f"Token balance request sent ({symbol}). Waiting for response...")

            result = client.wait_for_balance(timeout=60)
            if result:
                human = result.get("human_amount", result.get("amount", 0) / (10 ** decimals))
                click.echo(f"Address: {result['pubkey']}")
                click.echo(f"Balance: {human:.{decimals}f} {symbol} ({result.get('amount', 0)} base units)")
            else:
                click.echo("Timed out waiting for balance response.")
        else:
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


# --- Queue management ---

@cli.group()
@click.pass_context
def queue(ctx):
    """Manage the deferred transaction queue."""
    pass


@queue.command("list")
@click.option("--status", type=click.Choice(["pending", "sending", "sent", "failed"]),
              default=None, help="Filter by status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def queue_list(ctx, status, as_json):
    """List queued transaction intents."""
    import json as json_mod
    from solmesh.store import IntentStore

    store = IntentStore()
    intents = store.list_intents(status=status)

    if as_json:
        click.echo(json_mod.dumps(intents, indent=2))
        return

    if not intents:
        click.echo("No intents found.")
        return

    click.echo(f"{'ID':<14} {'Status':<10} {'Mode':<6} {'Wallet':<15} {'Amount':<12} {'Recipient':<44} {'Attempts'}")
    click.echo("-" * 110)
    for i in intents:
        token_label = ""
        if i.get("token_mint"):
            from solmesh.constants import KNOWN_TOKENS
            info = KNOWN_TOKENS.get(i["token_mint"])
            token_label = f" {info[0]}" if info else f" TOKEN"
        else:
            token_label = " SOL"
        click.echo(
            f"{i['id']:<14} {i['status']:<10} {i.get('mode', '?'):<6} "
            f"{i['wallet_name']:<15} {i['amount']:<12}{token_label:<6} "
            f"{i['recipient'][:44]:<44} "
            f"{i.get('attempts', 0)}/{i.get('max_attempts', 3)}"
        )


@queue.command("flush")
@click.option("--passphrase", prompt=True, hide_input=True, default="",
              help="Wallet passphrase")
@click.option("--wallet", "-w", default=None, help="Only flush intents for this wallet")
@click.option("--gateway-node", "-g", default=None, help="Gateway mesh node ID")
@click.option("--auto-discover", is_flag=True, help="Auto-discover gateway via beacon")
@click.pass_context
def queue_flush(ctx, passphrase, wallet, gateway_node, auto_discover):
    """Connect to mesh, discover gateway, and flush pending intents."""
    from solmesh.client import ClientNode
    from solmesh.store import IntentStore

    config = ctx.obj["config"]
    store = IntentStore()

    pending = store.pending_intents()
    if not pending:
        click.echo("No pending intents to flush.")
        return

    mesh = build_mesh(config)
    wm = WalletManager()

    client = ClientNode(
        mesh=mesh,
        wallet_manager=wm,
        gateway_node_id=gateway_node,
        intent_store=store,
    )
    client.connect()

    if auto_discover and not gateway_node:
        click.echo("Discovering gateway via beacon...")
        gw = client.discover_gateway(timeout=120)
        if not gw:
            click.echo("No gateway found.", err=True)
            client.close()
            sys.exit(1)
        click.echo(f"  Found gateway: {gw}")

    wallet_names = set(i["wallet_name"] for i in pending)
    if wallet:
        wallet_names = {wallet}
    passphrase_map = {w: passphrase for w in wallet_names}

    try:
        click.echo(f"Flushing {len(pending)} pending intent(s)...")
        results = client.flush_all_pending(
            passphrase_map=passphrase_map,
            wallet_filter=wallet,
        )
        for r in results:
            intent_id = r.get("intent_id", "?")
            if r.get("success"):
                click.echo(f"  {intent_id}: OK (tx: {r.get('signature', '?')})")
            else:
                click.echo(f"  {intent_id}: FAILED ({r.get('error', '?')})")
    finally:
        client.close()


@queue.command("clear")
@click.option("--status", type=click.Choice(["pending", "sending", "sent", "failed"]),
              default=None, help="Only clear intents with this status (default: all)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def queue_clear(ctx, status, yes):
    """Remove intents from the queue."""
    from solmesh.store import IntentStore

    store = IntentStore()
    label = status or "all"
    if not yes:
        click.confirm(f"Remove {label} intents?", abort=True)
    count = store.clear(status=status)
    click.echo(f"Removed {count} intent(s).")


@queue.command("remove")
@click.argument("intent_id")
@click.pass_context
def queue_remove(ctx, intent_id):
    """Remove a single intent by ID."""
    from solmesh.store import IntentStore

    store = IntentStore()
    if store.remove(intent_id):
        click.echo(f"Removed intent {intent_id}.")
    else:
        click.echo(f"Intent '{intent_id}' not found.", err=True)
        sys.exit(1)


# --- Listen daemon ---

@cli.command("listen")
@click.option("--wallet", "-w", required=True, help="Wallet name for auto-flush")
@click.option("--passphrase", prompt=True, hide_input=True, default="",
              help="Wallet passphrase (cached in memory)")
@click.option("--gateway-node", "-g", default=None, help="Gateway mesh node ID")
@click.option("--auto-discover", is_flag=True, help="Auto-discover gateway via beacon")
@click.pass_context
def listen(ctx, wallet, passphrase, gateway_node, auto_discover):
    """Long-running daemon that auto-flushes queued intents on gateway beacon."""
    import time as time_mod
    from solmesh.client import ClientNode
    from solmesh.store import IntentStore

    config = ctx.obj["config"]
    mesh = build_mesh(config)
    wm = WalletManager()
    store = IntentStore()

    try:
        wm.load_keypair(wallet, passphrase=passphrase)
    except FileNotFoundError:
        click.echo(f"Error: Wallet '{wallet}' not found.", err=True)
        sys.exit(1)
    except Exception as e:
        if "InvalidTag" in type(e).__name__:
            click.echo("Error: Wrong passphrase.", err=True)
            sys.exit(1)
        raise

    client = ClientNode(
        mesh=mesh,
        wallet_manager=wm,
        gateway_node_id=gateway_node,
        intent_store=store,
        auto_flush=True,
    )
    client.cache_passphrase(wallet, passphrase)
    client.connect()

    if auto_discover and not gateway_node:
        click.echo("Discovering gateway via beacon...")
        gw = client.discover_gateway(timeout=120)
        if not gw:
            click.echo("No gateway found.", err=True)
            client.close()
            sys.exit(1)
        click.echo(f"  Found gateway: {gw}")

    click.echo("Listening for beacons. Queued intents will auto-flush.")
    click.echo("Press Ctrl+C to stop.")

    try:
        while True:
            time_mod.sleep(1)
    except KeyboardInterrupt:
        click.echo("\nStopping...")
    finally:
        client.close()


def main():
    cli(obj={})


if __name__ == "__main__":
    main()
