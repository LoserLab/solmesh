"""Client node logic for SolMesh.

The offline client node signs transactions locally and sends them
over the Meshtastic mesh to a gateway for broadcasting to Solana.
"""

from __future__ import annotations
import logging
import struct
import threading
import time
from typing import Callable, Optional

from solders.hash import Hash as SolHash
from solders.pubkey import Pubkey

from solmesh.chunker import chunk_payload, generate_msg_id
from solmesh.constants import (
    ACK_TIMEOUT,
    KNOWN_TOKENS,
    LAMPORTS_PER_SOL,
    MAX_FLUSH_ATTEMPTS,
    MAX_RETRIES,
    MsgType,
    RETRY_DELAY,
)
from solmesh.crypto import sign_payload
from solmesh.mesh import MeshInterface
from solmesh.protocol import (
    SolMeshHeader,
    decode_ack,
    decode_addr_share,
    decode_balance_resp,
    decode_blockhash_resp,
    decode_gateway_beacon,
    decode_tx_result,
    encode_addr_share,
    encode_balance_req,
    encode_blockhash_req,
    encode_tx_request,
    pack_message,
)
from solmesh.spl import create_spl_transfer
from solmesh.wallet import WalletManager, create_sol_transfer

logger = logging.getLogger(__name__)


class ClientNode:
    """Offline client node that sends Solana transactions over the mesh.

    Supports Mode 1 (relay signed TX), Mode 2 (wallet-to-wallet),
    and Mode 3 (request gateway to send).
    """

    def __init__(self, mesh: MeshInterface,
                 wallet_manager: WalletManager,
                 gateway_node_id: Optional[str] = None,
                 intent_store: Optional['IntentStore'] = None,
                 auto_flush: bool = False):
        self._mesh = mesh
        self._wallet_mgr = wallet_manager
        self._gateway_id = gateway_node_id
        self._acked_chunks: dict[int, set[int]] = {}  # msg_id -> set of acked chunk_nums
        self._results: dict[int, dict] = {}  # msg_id -> result dict
        self._balances: dict[int, dict] = {}  # msg_id -> balance resp dict
        self._received_addresses: dict[str, dict] = {}  # sender_id -> addr info
        self._discovered_gateways: dict[str, dict] = {}  # sender_id -> beacon info
        self._blockhash: Optional[bytes] = None
        self._result_cond = threading.Condition()
        self._balance_cond = threading.Condition()
        self._blockhash_cond = threading.Condition()
        self._gateway_cond = threading.Condition()
        # Store-and-forward
        self._intent_store = intent_store
        self._auto_flush = auto_flush
        self._flush_lock = threading.Lock()
        self._flushing = False
        self._passphrase_cache: dict[str, str] = {}

    def connect(self) -> None:
        """Connect to mesh and register handlers."""
        self._mesh.register_handler(MsgType.ACK, self._handle_ack)
        self._mesh.register_handler(MsgType.NACK, self._handle_nack)
        self._mesh.register_handler(MsgType.TX_RESULT, self._handle_tx_result)
        self._mesh.register_handler(MsgType.BALANCE_RESP, self._handle_balance_resp)
        self._mesh.register_handler(MsgType.BLOCKHASH_RESP, self._handle_blockhash_resp)
        self._mesh.register_handler(MsgType.GATEWAY_BEACON, self._handle_gateway_beacon)
        self._mesh.register_handler(MsgType.ADDR_SHARE, self._handle_addr_share)
        self._mesh.connect()

    def close(self) -> None:
        """Close the mesh connection."""
        self._mesh.close()

    # --- Mode 1: Relay pre-signed transaction ---

    def relay_signed_tx(self, wallet_name: str, recipient: str,
                        amount_sol: float, blockhash: Optional[str] = None,
                        passphrase: str = "",
                        on_result: Optional[Callable] = None) -> int:
        """Create, sign, and relay a SOL transfer over mesh to gateway.

        If blockhash is not provided, it will be fetched from the gateway.
        Returns the msg_id for tracking.
        The private key NEVER leaves this device.
        """
        # Load keypair locally
        kp = self._wallet_mgr.load_keypair(wallet_name, passphrase=passphrase)

        # Fetch blockhash from gateway if not provided
        if blockhash is None:
            logger.info("Fetching recent blockhash from gateway...")
            bh_bytes = self.fetch_blockhash()
            if bh_bytes is None:
                raise TimeoutError("Failed to fetch blockhash from gateway")
            recent_blockhash = SolHash.from_bytes(bh_bytes)
        else:
            recent_blockhash = SolHash.from_string(blockhash)

        # Build and sign transaction
        recipient_pubkey = Pubkey.from_string(recipient)
        lamports = int(amount_sol * LAMPORTS_PER_SOL)

        tx_bytes = create_sol_transfer(kp, recipient_pubkey, lamports, recent_blockhash)
        logger.info(
            "Transaction signed locally (%d bytes): %.4f SOL -> %s",
            len(tx_bytes), amount_sol, recipient,
        )

        return self.relay_raw_tx(tx_bytes, on_result=on_result)

    def relay_signed_token_tx(self, wallet_name: str, recipient: str,
                              mint_address: str, amount: float,
                              decimals: int = 6,
                              blockhash: Optional[str] = None,
                              passphrase: str = "",
                              create_recipient_ata: bool = False,
                              on_result: Optional[Callable] = None) -> int:
        """Create, sign, and relay an SPL token transfer over mesh (Mode 1).

        Returns the msg_id for tracking.
        The private key NEVER leaves this device.
        """
        kp = self._wallet_mgr.load_keypair(wallet_name, passphrase=passphrase)

        if blockhash is None:
            logger.info("Fetching recent blockhash from gateway...")
            bh_bytes = self.fetch_blockhash()
            if bh_bytes is None:
                raise TimeoutError("Failed to fetch blockhash from gateway")
            recent_blockhash = SolHash.from_bytes(bh_bytes)
        else:
            recent_blockhash = SolHash.from_string(blockhash)

        recipient_pubkey = Pubkey.from_string(recipient)
        mint_pubkey = Pubkey.from_string(mint_address)
        base_units = int(amount * (10 ** decimals))

        tx_bytes = create_spl_transfer(
            kp, recipient_pubkey, mint_pubkey,
            base_units, decimals, recent_blockhash,
            create_recipient_ata=create_recipient_ata,
        )
        token_symbol = KNOWN_TOKENS.get(mint_address, ("TOKEN",))[0]
        logger.info(
            "Token transfer signed locally (%d bytes): %s %s -> %s",
            len(tx_bytes), amount, token_symbol, recipient,
        )

        return self.relay_raw_tx(tx_bytes, on_result=on_result)

    def relay_raw_tx(self, tx_bytes: bytes,
                     on_result: Optional[Callable] = None) -> int:
        """Send an already-serialized signed transaction over mesh.

        Returns the msg_id for tracking.
        """
        msg_id = generate_msg_id()
        chunks = chunk_payload(tx_bytes, MsgType.TX_CHUNK, msg_id=msg_id)
        logger.info(
            "Sending transaction: %d bytes in %d chunks (msg_id=%d)",
            len(tx_bytes), len(chunks), msg_id,
        )

        self._acked_chunks[msg_id] = set()
        thread = threading.Thread(
            target=self._retry_loop, args=(chunks, msg_id), daemon=True
        )
        thread.start()

        return msg_id

    # --- Mode 2: Wallet-to-wallet ---

    def share_address(self, wallet_name: str, label: str = "") -> bool:
        """Broadcast this node's Solana address over mesh with ACK retry.

        Returns True if ACK received, False if all retries exhausted.
        """
        pubkey = self._wallet_mgr.get_pubkey(wallet_name)
        payload = encode_addr_share(bytes(pubkey), label=label)
        msg_id = generate_msg_id()
        msg = pack_message(MsgType.ADDR_SHARE, msg_id, 0, 1, payload)

        self._acked_chunks[msg_id] = set()
        display_label = label or wallet_name

        for attempt in range(MAX_RETRIES + 1):
            self._mesh.send(msg)
            logger.info("Shared address '%s': %s (attempt %d)", display_label, pubkey, attempt + 1)

            time.sleep(ACK_TIMEOUT)
            if 0 in self._acked_chunks.get(msg_id, set()):
                logger.info("Address share ACK received for '%s'", display_label)
                return True

            if attempt < MAX_RETRIES:
                logger.warning("No ACK for address share, retrying...")

        logger.warning("Address share not ACKed after %d retries", MAX_RETRIES)
        return False

    def get_received_addresses(self) -> dict[str, dict]:
        """Return all addresses received from other nodes."""
        return dict(self._received_addresses)

    # --- Gateway discovery ---

    def discover_gateway(self, timeout: float = 120) -> Optional[str]:
        """Wait for a gateway beacon and auto-set gateway ID.

        Returns the gateway mesh node ID, or None on timeout.
        """
        deadline = time.time() + timeout
        with self._gateway_cond:
            while not self._discovered_gateways:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._gateway_cond.wait(timeout=remaining)

            # Pick the most recently seen gateway
            best_id = max(
                self._discovered_gateways,
                key=lambda k: self._discovered_gateways[k]["last_seen"],
            )
            if not self._gateway_id:
                self._gateway_id = best_id
                logger.info("Auto-discovered gateway: %s", best_id)
            return best_id

    def is_gateway_online(self, max_stale_seconds: float = 180) -> bool:
        """Check if the current gateway has sent a recent beacon."""
        if not self._gateway_id:
            return False
        info = self._discovered_gateways.get(self._gateway_id)
        if not info:
            return False
        return (time.time() - info["last_seen"]) < max_stale_seconds

    # --- Mode 3: Request gateway transfer ---

    def request_transfer(self, wallet_name: str, destination: str,
                         amount_sol: float, passphrase: str = "") -> int:
        """Send a signed TX_REQUEST to the gateway.

        The request is signed with the sender's keypair to prove identity
        (but the gateway's hot wallet pays for the transfer).

        Returns msg_id for tracking.
        """
        if not self._gateway_id:
            raise ValueError("Gateway node ID not set")

        kp = self._wallet_mgr.load_keypair(wallet_name, passphrase=passphrase)
        sender_pubkey_bytes = bytes(kp.pubkey())
        dest_pubkey = Pubkey.from_string(destination)
        dest_pubkey_bytes = bytes(dest_pubkey)
        lamports = int(amount_sol * LAMPORTS_PER_SOL)

        # Sign the request payload for authentication
        signed_data = sender_pubkey_bytes + dest_pubkey_bytes + struct.pack("!Q", lamports)
        sig = sign_payload(kp, signed_data)

        payload = encode_tx_request(
            sender_pubkey_bytes, dest_pubkey_bytes, lamports, sig,
        )
        msg_id = generate_msg_id()
        msg = pack_message(MsgType.TX_REQUEST, msg_id, 0, 1, payload)

        logger.info(
            "Requesting gateway transfer: %.4f SOL -> %s",
            amount_sol, destination,
        )
        self._mesh.send(msg, destination_id=self._gateway_id)
        return msg_id

    def request_token_transfer(self, wallet_name: str, destination: str,
                               mint_address: str, amount: float,
                               decimals: int = 6,
                               passphrase: str = "") -> int:
        """Send a signed TX_REQUEST for an SPL token transfer to the gateway.

        Returns msg_id for tracking.
        """
        if not self._gateway_id:
            raise ValueError("Gateway node ID not set")

        kp = self._wallet_mgr.load_keypair(wallet_name, passphrase=passphrase)
        sender_pubkey_bytes = bytes(kp.pubkey())
        dest_pubkey = Pubkey.from_string(destination)
        dest_pubkey_bytes = bytes(dest_pubkey)
        mint_pubkey = Pubkey.from_string(mint_address)
        mint_bytes = bytes(mint_pubkey)
        base_units = int(amount * (10 ** decimals))

        # Sign: sender + dest + amount(8) + flags(1) + mint(32)
        flags = 0x01  # has_mint
        signed_data = (sender_pubkey_bytes + dest_pubkey_bytes
                       + struct.pack("!Q", base_units)
                       + struct.pack("!B", flags)
                       + mint_bytes)
        sig = sign_payload(kp, signed_data)

        payload = encode_tx_request(
            sender_pubkey_bytes, dest_pubkey_bytes, base_units, sig,
            mint=mint_bytes,
        )
        msg_id = generate_msg_id()
        msg = pack_message(MsgType.TX_REQUEST, msg_id, 0, 1, payload)

        token_symbol = KNOWN_TOKENS.get(mint_address, ("TOKEN",))[0]
        logger.info(
            "Requesting gateway token transfer: %s %s -> %s",
            amount, token_symbol, destination,
        )
        self._mesh.send(msg, destination_id=self._gateway_id)
        return msg_id

    def fetch_blockhash(self, timeout: float = 60) -> Optional[bytes]:
        """Request a recent blockhash from the gateway.

        Returns 32-byte blockhash or None on timeout.
        """
        if not self._gateway_id:
            raise ValueError("Gateway node ID not set")

        payload = encode_blockhash_req()
        msg_id = generate_msg_id()
        msg = pack_message(MsgType.BLOCKHASH_REQ, msg_id, 0, 1, payload)

        with self._blockhash_cond:
            self._blockhash = None
        self._mesh.send(msg, destination_id=self._gateway_id)
        logger.info("Requested blockhash from gateway")

        deadline = time.time() + timeout
        with self._blockhash_cond:
            while self._blockhash is None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._blockhash_cond.wait(timeout=remaining)
            return self._blockhash

    def check_balance(self, address: str) -> int:
        """Request balance of a Solana address from the gateway.

        Returns the msg_id. Listen for the result via the balance event.
        """
        if not self._gateway_id:
            raise ValueError("Gateway node ID not set")

        pubkey = Pubkey.from_string(address)
        payload = encode_balance_req(bytes(pubkey))
        msg_id = generate_msg_id()
        msg = pack_message(MsgType.BALANCE_REQ, msg_id, 0, 1, payload)

        self._mesh.send(msg, destination_id=self._gateway_id)
        logger.info("Requested balance for %s", address)
        return msg_id

    def check_token_balance(self, address: str, mint_address: str) -> int:
        """Request token balance of a Solana address from the gateway.

        Returns msg_id. Listen for the result via wait_for_balance.
        """
        if not self._gateway_id:
            raise ValueError("Gateway node ID not set")

        pubkey = Pubkey.from_string(address)
        mint_pubkey = Pubkey.from_string(mint_address)
        payload = encode_balance_req(bytes(pubkey), mint=bytes(mint_pubkey))
        msg_id = generate_msg_id()
        msg = pack_message(MsgType.BALANCE_REQ, msg_id, 0, 1, payload)

        self._mesh.send(msg, destination_id=self._gateway_id)
        logger.info("Requested token balance for %s", address)
        return msg_id

    def wait_for_result(self, msg_id: int, timeout: float = 120) -> Optional[dict]:
        """Block until a TX_RESULT is received for the given msg_id."""
        deadline = time.time() + timeout
        with self._result_cond:
            while msg_id not in self._results:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._result_cond.wait(timeout=remaining)
            return self._results.pop(msg_id)

    def wait_for_balance(self, timeout: float = 60) -> Optional[dict]:
        """Block until a BALANCE_RESP is received."""
        deadline = time.time() + timeout
        with self._balance_cond:
            while not self._balances:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._balance_cond.wait(timeout=remaining)
            key = max(self._balances.keys())
            return self._balances.pop(key)

    # --- Store-and-forward ---

    def cache_passphrase(self, wallet_name: str, passphrase: str) -> None:
        """Cache a wallet passphrase in memory (never written to disk)."""
        self._passphrase_cache[wallet_name] = passphrase

    def queue_intent(self, mode: int, wallet_name: str, recipient: str,
                     amount: float, token_mint: Optional[str] = None,
                     token_decimals: int = 0,
                     passphrase: Optional[str] = None):
        """Queue a transaction intent for later sending.

        Validates that the wallet exists. If passphrase is provided,
        validates it by attempting to load the keypair.
        Does NOT require a mesh connection or gateway.

        Returns the created Intent.
        """
        from solmesh.store import Intent

        if self._intent_store is None:
            raise RuntimeError("IntentStore not configured")
        if mode not in (1, 3):
            raise ValueError(f"Invalid mode: {mode}. Must be 1 or 3.")

        self._wallet_mgr.get_pubkey(wallet_name)

        if passphrase is not None:
            self._wallet_mgr.load_keypair(wallet_name, passphrase=passphrase)
            self._passphrase_cache[wallet_name] = passphrase

        intent = Intent(
            mode=mode,
            wallet_name=wallet_name,
            recipient=recipient,
            amount=amount,
            token_mint=token_mint,
            token_decimals=token_decimals,
            max_attempts=MAX_FLUSH_ATTEMPTS,
        )
        return self._intent_store.add(intent)

    def flush_intent(self, intent: dict, passphrase: str) -> dict:
        """Flush a single intent: send it over mesh and wait for result.

        Sets status to SENDING, dispatches via the appropriate mode,
        waits for result, updates status to SENT or increments attempts.

        InvalidTag (wrong passphrase) does NOT increment attempts.
        """
        from solmesh.store import IntentStatus

        if self._intent_store is None:
            raise RuntimeError("IntentStore not configured")

        intent_id = intent["id"]
        self._intent_store.update_status(intent_id, IntentStatus.SENDING.value)

        try:
            if intent["mode"] == 3:
                if intent.get("token_mint"):
                    msg_id = self.request_token_transfer(
                        wallet_name=intent["wallet_name"],
                        destination=intent["recipient"],
                        mint_address=intent["token_mint"],
                        amount=intent["amount"],
                        decimals=intent.get("token_decimals", 6),
                        passphrase=passphrase,
                    )
                else:
                    msg_id = self.request_transfer(
                        wallet_name=intent["wallet_name"],
                        destination=intent["recipient"],
                        amount_sol=intent["amount"],
                        passphrase=passphrase,
                    )
            elif intent["mode"] == 1:
                if intent.get("token_mint"):
                    msg_id = self.relay_signed_token_tx(
                        wallet_name=intent["wallet_name"],
                        recipient=intent["recipient"],
                        mint_address=intent["token_mint"],
                        amount=intent["amount"],
                        decimals=intent.get("token_decimals", 6),
                        passphrase=passphrase,
                    )
                else:
                    msg_id = self.relay_signed_tx(
                        wallet_name=intent["wallet_name"],
                        recipient=intent["recipient"],
                        amount_sol=intent["amount"],
                        passphrase=passphrase,
                    )
            else:
                raise ValueError(f"Unsupported intent mode: {intent['mode']}")

        except Exception as e:
            if "InvalidTag" in type(e).__name__:
                self._intent_store.update_status(
                    intent_id, IntentStatus.PENDING.value,
                    error=f"Wrong passphrase: {e}",
                )
                raise
            self._intent_store.increment_attempts(intent_id)
            current = self._intent_store.get(intent_id)
            status = current.get("status", IntentStatus.PENDING.value) if current else IntentStatus.PENDING.value
            self._intent_store.update_status(intent_id, status, error=str(e))
            return {"success": False, "error": str(e)}

        self._intent_store.increment_attempts(intent_id)

        result = self.wait_for_result(msg_id, timeout=ACK_TIMEOUT)
        if result and result.get("success"):
            self._intent_store.update_status(
                intent_id, IntentStatus.SENT.value,
                tx_hash=result.get("signature"),
            )
        elif result:
            current = self._intent_store.get(intent_id)
            if current and current.get("status") != IntentStatus.FAILED.value:
                self._intent_store.update_status(
                    intent_id, IntentStatus.PENDING.value,
                    error=result.get("error", "Unknown error"),
                )
        else:
            current = self._intent_store.get(intent_id)
            if current and current.get("status") != IntentStatus.FAILED.value:
                self._intent_store.update_status(
                    intent_id, IntentStatus.PENDING.value,
                    error="Timeout waiting for result",
                )

        return result or {"success": False, "error": "Timeout"}

    def flush_all_pending(self, passphrase_map: Optional[dict] = None,
                          wallet_filter: Optional[str] = None) -> list:
        """Flush all pending intents sequentially.

        Args:
            passphrase_map: dict of wallet_name -> passphrase (overrides cache)
            wallet_filter: if set, only flush intents for this wallet name

        Returns list of result dicts.
        """
        if self._intent_store is None:
            raise RuntimeError("IntentStore not configured")

        passphrase_map = passphrase_map or {}
        results = []

        with self._intent_store.flush_lock():
            pending = self._intent_store.pending_intents()

            for intent in pending:
                if wallet_filter and intent.get("wallet_name") != wallet_filter:
                    continue

                wallet_name = intent["wallet_name"]
                passphrase = passphrase_map.get(wallet_name)
                if passphrase is None:
                    passphrase = self._passphrase_cache.get(wallet_name)
                if passphrase is None:
                    logger.warning(
                        "Skipping intent %s: no passphrase for wallet '%s'",
                        intent["id"], wallet_name,
                    )
                    continue

                if not self.is_gateway_online():
                    logger.warning("Gateway offline, stopping flush")
                    break

                try:
                    result = self.flush_intent(intent, passphrase)
                    results.append({"intent_id": intent["id"], **result})
                except Exception as e:
                    logger.error("Failed to flush intent %s: %s", intent["id"], e)
                    results.append({
                        "intent_id": intent["id"],
                        "success": False,
                        "error": str(e),
                    })

        return results

    def _auto_flush_pending(self) -> None:
        """Background auto-flush triggered by beacon. Non-blocking lock."""
        if not self._flush_lock.acquire(blocking=False):
            logger.debug("Auto-flush skipped: already flushing")
            return
        try:
            self._flushing = True
            self.flush_all_pending()
        finally:
            self._flushing = False
            self._flush_lock.release()

    # --- Handlers ---

    def _handle_ack(self, header: SolMeshHeader, payload: bytes,
                    sender_id: str) -> None:
        """Track which chunks have been acknowledged."""
        ack = decode_ack(payload)
        msg_id = ack["acked_msg_id"]
        chunk_num = ack["acked_chunk"]

        if msg_id in self._acked_chunks:
            self._acked_chunks[msg_id].add(chunk_num)
            logger.debug("ACK received: msg_id=%d chunk=%d", msg_id, chunk_num)

    def _handle_nack(self, header: SolMeshHeader, payload: bytes,
                     sender_id: str) -> None:
        """Handle NACK from gateway."""
        from solmesh.protocol import decode_nack
        nack = decode_nack(payload)
        logger.warning(
            "NACK received: msg_id=%d error=0x%02x %s",
            nack["nacked_msg_id"], nack["error_code"], nack["error_msg"],
        )
        with self._result_cond:
            self._results[nack["nacked_msg_id"]] = {
                "success": False,
                "error": nack["error_msg"],
            }
            self._result_cond.notify_all()

    def _handle_tx_result(self, header: SolMeshHeader, payload: bytes,
                          sender_id: str) -> None:
        """Process transaction result from gateway."""
        result = decode_tx_result(payload)
        msg_id = result["orig_msg_id"]

        with self._result_cond:
            if result["success"]:
                sig = result["data"].decode("utf-8", errors="replace")
                logger.info("Transaction confirmed: %s", sig)
                self._results[msg_id] = {"success": True, "signature": sig}
            else:
                error = result["data"].decode("utf-8", errors="replace")
                logger.error("Transaction failed: %s", error)
                self._results[msg_id] = {"success": False, "error": error}

            self._result_cond.notify_all()

    def _handle_balance_resp(self, header: SolMeshHeader, payload: bytes,
                             sender_id: str) -> None:
        """Process balance response from gateway."""
        resp = decode_balance_resp(payload)
        pubkey = Pubkey.from_bytes(resp["pubkey"])
        mint_bytes = resp.get("mint", b"")

        if mint_bytes:
            mint_pubkey = Pubkey.from_bytes(mint_bytes)
            mint_str = str(mint_pubkey)
            token_info = KNOWN_TOKENS.get(mint_str, ("TOKEN", 6))
            symbol, decimals = token_info
            human_amount = resp["amount"] / (10 ** decimals)
            logger.info("Token balance for %s: %s %s", pubkey, human_amount, symbol)
            with self._balance_cond:
                self._balances[header.msg_id] = {
                    "pubkey": str(pubkey),
                    "amount": resp["amount"],
                    "human_amount": human_amount,
                    "mint": mint_str,
                    "token_symbol": symbol,
                    "decimals": decimals,
                    "lamports": resp["amount"],
                    "sol": resp["amount"] / LAMPORTS_PER_SOL,
                }
                self._balance_cond.notify_all()
        else:
            sol = resp["lamports"] / LAMPORTS_PER_SOL
            logger.info("Balance for %s: %.9f SOL", pubkey, sol)
            with self._balance_cond:
                self._balances[header.msg_id] = {
                    "pubkey": str(pubkey),
                    "lamports": resp["lamports"],
                    "sol": sol,
                }
                self._balance_cond.notify_all()

    def _handle_blockhash_resp(self, header: SolMeshHeader, payload: bytes,
                               sender_id: str) -> None:
        """Process blockhash response from gateway."""
        resp = decode_blockhash_resp(payload)
        logger.info("Received blockhash from gateway")
        with self._blockhash_cond:
            self._blockhash = resp["blockhash"]
            self._blockhash_cond.notify_all()

    def _handle_gateway_beacon(self, header: SolMeshHeader, payload: bytes,
                               sender_id: str) -> None:
        """Process gateway beacon and track discovered gateways."""
        beacon = decode_gateway_beacon(payload)
        with self._gateway_cond:
            self._discovered_gateways[sender_id] = {
                **beacon,
                "last_seen": time.time(),
            }
            # Auto-set gateway if none configured
            if not self._gateway_id:
                self._gateway_id = sender_id
                logger.info("Auto-discovered gateway: %s", sender_id)
            self._gateway_cond.notify_all()
        logger.debug(
            "Gateway beacon from %s: v%d caps=0x%02x uptime=%ds",
            sender_id, beacon["version"], beacon["capabilities"],
            beacon["uptime_seconds"],
        )
        # Trigger auto-flush if enabled and there are pending intents
        if (self._auto_flush
                and self._intent_store is not None
                and self._intent_store.pending_intents()):
            threading.Thread(
                target=self._auto_flush_pending, daemon=True
            ).start()

    def _handle_addr_share(self, header: SolMeshHeader, payload: bytes,
                           sender_id: str) -> None:
        """Store received address associations."""
        data = decode_addr_share(payload)
        pubkey = Pubkey.from_bytes(data["pubkey"])
        self._received_addresses[sender_id] = {
            "pubkey": str(pubkey),
            "label": data["label"],
        }
        label = data["label"] or sender_id
        logger.info("Received address from %s: %s", label, pubkey)

    # --- Retry logic ---

    def _retry_loop(self, chunks: list[bytes], msg_id: int) -> None:
        """Send chunks with ACK tracking and retry logic (runs in background thread)."""
        from solmesh.protocol import unpack_message

        # Parse chunk numbers for tracking
        chunk_info = []
        for chunk_raw in chunks:
            hdr, _ = unpack_message(chunk_raw)
            chunk_info.append(hdr.chunk_num)

        # Send all chunks
        self._mesh.send_chunks(chunks, destination_id=self._gateway_id)

        # Wait for ACKs and retry unacked chunks
        for attempt in range(MAX_RETRIES):
            time.sleep(ACK_TIMEOUT)

            acked = self._acked_chunks.get(msg_id, set())
            unacked = [i for i, cn in enumerate(chunk_info) if cn not in acked]

            if not unacked:
                logger.info("All %d chunks acknowledged", len(chunks))
                return

            logger.warning(
                "Retry %d/%d: %d chunks unacked",
                attempt + 1, MAX_RETRIES, len(unacked),
            )

            for idx in unacked:
                self._mesh.send(chunks[idx], destination_id=self._gateway_id)
                time.sleep(RETRY_DELAY)

        # Final check
        acked = self._acked_chunks.get(msg_id, set())
        unacked_count = sum(1 for cn in chunk_info if cn not in acked)
        if unacked_count > 0:
            logger.error(
                "Failed to deliver %d/%d chunks after %d retries",
                unacked_count, len(chunks), MAX_RETRIES,
            )
            with self._result_cond:
                self._results[msg_id] = {
                    "success": False,
                    "error": f"Failed to deliver {unacked_count}/{len(chunks)} chunks",
                }
                self._result_cond.notify_all()
