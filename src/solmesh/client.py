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
    LAMPORTS_PER_SOL,
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
from solmesh.wallet import WalletManager, create_sol_transfer

logger = logging.getLogger(__name__)


class ClientNode:
    """Offline client node that sends Solana transactions over the mesh.

    Supports Mode 1 (relay signed TX), Mode 2 (wallet-to-wallet),
    and Mode 3 (request gateway to send).
    """

    def __init__(self, mesh: MeshInterface,
                 wallet_manager: WalletManager,
                 gateway_node_id: Optional[str] = None):
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
