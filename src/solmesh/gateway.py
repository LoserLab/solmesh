"""Gateway node logic for SolMesh.

The internet-connected gateway listens for SolMesh messages on the mesh,
reassembles chunked transactions, and broadcasts them to Solana via RPC.
Supports all three operating modes.
"""

from __future__ import annotations
import logging
import struct
import threading
import time
from typing import Optional

from solana.rpc.api import Client as SolanaClient
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from solmesh.chunker import ChunkReassembler, chunk_payload, generate_msg_id
from solmesh.config import GatewayConfig
from solmesh.constants import (
    KNOWN_TOKENS,
    LAMPORTS_PER_SOL,
    PROTOCOL_VERSION,
    MsgType,
    ErrorCode,
)
from solmesh.rate_limiter import RateLimiter
from solmesh.crypto import verify_payload
from solmesh.mesh import MeshInterface
from solmesh.protocol import (
    BEACON_CAP_BALANCE,
    BEACON_CAP_BLOCKHASH,
    BEACON_CAP_HOT_WALLET,
    BEACON_CAP_RELAY,
    BEACON_CAP_SPL_TOKEN,
    SolMeshHeader,
    decode_balance_req,
    decode_blockhash_req,
    decode_tx_request,
    decode_addr_share,
    encode_ack,
    encode_balance_resp,
    encode_blockhash_resp,
    encode_gateway_beacon,
    encode_nack,
    encode_tx_result,
    pack_message,
)
from solmesh.spl import create_spl_transfer, find_associated_token_address
from solmesh.wallet import WalletManager, create_sol_transfer, deserialize_transaction

logger = logging.getLogger(__name__)


class GatewayNode:
    """Internet-connected gateway that bridges mesh <-> Solana RPC.

    Supports:
    - Mode 1: Receives pre-signed TX chunks, reassembles, broadcasts to RPC
    - Mode 2: Relays wallet-to-wallet address exchanges
    - Mode 3: Holds a hot wallet, creates TXs on behalf of remote nodes
    """

    def __init__(self, mesh: MeshInterface, rpc_url: str,
                 wallet_manager: Optional[WalletManager] = None,
                 gateway_config: Optional[GatewayConfig] = None):
        self._mesh = mesh
        self._rpc = SolanaClient(rpc_url)
        self._reassembler = ChunkReassembler()
        self._wallet_mgr = wallet_manager or WalletManager()
        self._config = gateway_config or GatewayConfig()
        self._known_addresses: dict[str, bytes] = {}  # mesh_id -> solana pubkey bytes
        self._hot_keypair: Optional[Keypair] = None
        self._start_time = time.time()
        self._beacon_thread: Optional[threading.Thread] = None
        self._running = False
        self._rate_limiter = RateLimiter(
            max_per_minute=self._config.max_requests_per_minute,
            burst=self._config.rate_limit_burst,
        )

    def start(self, hot_wallet_passphrase: str = "") -> None:
        """Register handlers and start listening on mesh."""
        # Load hot wallet if configured
        if self._config.hot_wallet:
            try:
                self._hot_keypair = self._wallet_mgr.load_keypair(
                    self._config.hot_wallet, passphrase=hot_wallet_passphrase
                )
                logger.info(
                    "Hot wallet loaded: %s", self._hot_keypair.pubkey()
                )
            except Exception as e:
                logger.error("Failed to load hot wallet: %s", e)
                raise

        self._mesh.register_handler(MsgType.TX_CHUNK, self._handle_tx_chunk)
        self._mesh.register_handler(MsgType.TX_REQUEST, self._handle_tx_request)
        self._mesh.register_handler(MsgType.BALANCE_REQ, self._handle_balance_req)
        self._mesh.register_handler(MsgType.BLOCKHASH_REQ, self._handle_blockhash_req)
        self._mesh.register_handler(MsgType.ADDR_SHARE, self._handle_addr_share)
        self._mesh.connect()

        self._running = True
        self._start_beacon_thread()

        logger.info("Gateway node started. Listening for SolMesh messages...")
        self._mesh.run()

    def _start_beacon_thread(self) -> None:
        """Start the periodic beacon broadcast thread."""
        self._beacon_thread = threading.Thread(
            target=self._beacon_loop, daemon=True
        )
        self._beacon_thread.start()

    def _beacon_loop(self) -> None:
        """Periodically broadcast gateway beacon."""
        while self._running:
            self._send_beacon()
            time.sleep(self._config.beacon_interval)

    def _send_beacon(self) -> None:
        """Broadcast a gateway beacon message."""
        caps = BEACON_CAP_RELAY | BEACON_CAP_BALANCE | BEACON_CAP_BLOCKHASH | BEACON_CAP_SPL_TOKEN
        hot_wallet_pubkey = b""
        if self._hot_keypair:
            caps |= BEACON_CAP_HOT_WALLET
            hot_wallet_pubkey = bytes(self._hot_keypair.pubkey())

        uptime = int(time.time() - self._start_time)
        payload = encode_gateway_beacon(
            PROTOCOL_VERSION, caps, hot_wallet_pubkey, uptime,
        )
        msg = pack_message(
            MsgType.GATEWAY_BEACON, generate_msg_id(), 0, 1, payload
        )
        self._mesh.send(msg, want_ack=False)
        logger.debug("Beacon sent (uptime=%ds)", uptime)

    def _check_rate_limit(self, sender_id: str, msg_id: int) -> bool:
        """Check rate limit for sender. Sends NACK if rate-limited.

        Returns True if the request is allowed, False if rate-limited.
        """
        if not self._rate_limiter.is_allowed(sender_id):
            logger.warning("Rate limited: %s", sender_id)
            self._send_nack(
                msg_id, ErrorCode.RATE_LIMITED,
                "Rate limited", sender_id,
            )
            return False
        return True

    def _handle_tx_chunk(self, header: SolMeshHeader, payload: bytes,
                         sender_id: str) -> None:
        """Handle incoming TX_CHUNK messages (Mode 1).

        Reassemble chunks. When complete, broadcast to Solana RPC.
        """
        if not self._check_rate_limit(sender_id, header.msg_id):
            return

        logger.info(
            "TX_CHUNK from %s: msg_id=%d chunk=%d/%d",
            sender_id, header.msg_id, header.chunk_num + 1, header.total_chunks,
        )

        # Send ACK for this chunk
        ack_payload = encode_ack(header.msg_id, header.chunk_num)
        ack_msg = pack_message(
            MsgType.ACK, generate_msg_id(), 0, 1, ack_payload
        )
        self._mesh.send(ack_msg, destination_id=sender_id, want_ack=False)

        # Feed to reassembler (keyed by sender_id + msg_id to avoid collisions)
        complete_data = self._reassembler.receive_chunk(
            sender_id, header.msg_id, header.chunk_num, header.total_chunks, payload
        )

        if complete_data is None:
            return

        logger.info(
            "Transaction fully reassembled (%d bytes) from %s",
            len(complete_data), sender_id,
        )

        # Broadcast to Solana
        success, result = self._broadcast_to_solana(complete_data)
        self._send_tx_result(header.msg_id, success, result, sender_id)

    def _handle_tx_request(self, header: SolMeshHeader, payload: bytes,
                           sender_id: str) -> None:
        """Handle TX_REQUEST messages (Mode 3).

        Verify sender authorization, create transfer from hot wallet,
        sign and broadcast.
        """
        if not self._check_rate_limit(sender_id, header.msg_id):
            return

        logger.info("TX_REQUEST from %s", sender_id)

        if not self._hot_keypair:
            logger.warning("TX_REQUEST received but no hot wallet configured")
            self._send_nack(
                header.msg_id, ErrorCode.UNAUTHORIZED,
                "No hot wallet configured", sender_id,
            )
            return

        try:
            req = decode_tx_request(payload)
        except ValueError as e:
            logger.error("Invalid TX_REQUEST: %s", e)
            self._send_nack(
                header.msg_id, ErrorCode.INVALID_TX, str(e), sender_id,
            )
            return

        # Verify Ed25519 signature FIRST to prove keypair ownership
        sender_pubkey = Pubkey.from_bytes(req["sender_pubkey"])
        # Build signed data: sender + dest + amount(8) + [flags(1) + mint(32)]
        signed_data = (req["sender_pubkey"] + req["dest_pubkey"]
                       + struct.pack("!Q", req["amount"]))
        if req.get("flags", 0):
            signed_data += struct.pack("!B", req["flags"])
            if req["flags"] & 0x01 and req.get("mint"):
                signed_data += req["mint"]
        if not verify_payload(sender_pubkey, signed_data, req["signature"]):
            logger.warning("Invalid signature on TX_REQUEST from %s", sender_id)
            self._send_nack(
                header.msg_id, ErrorCode.UNAUTHORIZED,
                "Invalid signature", sender_id,
            )
            return

        # Authorize by Solana pubkey (not mesh ID, which is spoofable)
        if self._config.allowed_requesters:
            sender_pubkey_str = str(sender_pubkey)
            if sender_pubkey_str not in self._config.allowed_requesters:
                logger.warning(
                    "Unauthorized TX_REQUEST: pubkey %s not in allowed list",
                    sender_pubkey_str,
                )
                self._send_nack(
                    header.msg_id, ErrorCode.UNAUTHORIZED,
                    "Not in allowed requesters", sender_id,
                )
                return

        # Detect token transfer
        mint_bytes = req.get("mint", b"")
        is_token_transfer = len(mint_bytes) == 32

        # Check amount limit
        if is_token_transfer:
            mint_pubkey = Pubkey.from_bytes(mint_bytes)
            mint_str = str(mint_pubkey)
            token_info = KNOWN_TOKENS.get(mint_str)
            if token_info is None:
                self._send_nack(
                    header.msg_id, ErrorCode.UNSUPPORTED_TOKEN,
                    f"Unsupported token: {mint_str[:16]}...", sender_id,
                )
                return
            symbol, decimals = token_info
            max_amount = self._config.get_max_transfer_token(mint_str)
            if max_amount is not None:
                max_base_units = int(max_amount * (10 ** decimals))
                if req["amount"] > max_base_units:
                    self._send_nack(
                        header.msg_id, ErrorCode.AMOUNT_EXCEEDED,
                        f"Max {max_amount} {symbol}", sender_id,
                    )
                    return
        else:
            max_lamports = int(self._config.max_transfer_sol * LAMPORTS_PER_SOL)
            if req["lamports"] > max_lamports:
                self._send_nack(
                    header.msg_id, ErrorCode.AMOUNT_EXCEEDED,
                    f"Max {self._config.max_transfer_sol} SOL", sender_id,
                )
                return

        # Fetch recent blockhash
        try:
            blockhash_resp = self._rpc.get_latest_blockhash()
            recent_blockhash = blockhash_resp.value.blockhash
        except Exception as e:
            logger.error("Failed to fetch blockhash: %s", e)
            self._send_nack(
                header.msg_id, ErrorCode.RPC_ERROR,
                f"Blockhash fetch failed: {e}", sender_id,
            )
            return

        # Create and sign transfer
        dest_pubkey = Pubkey.from_bytes(req["dest_pubkey"])
        try:
            if is_token_transfer:
                mint_pubkey = Pubkey.from_bytes(mint_bytes)
                _, decimals = KNOWN_TOKENS[str(mint_pubkey)]
                # Check if recipient has an ATA; create if missing
                recipient_ata = find_associated_token_address(dest_pubkey, mint_pubkey)
                create_ata = False
                try:
                    ata_info = self._rpc.get_account_info(recipient_ata)
                    if ata_info.value is None:
                        create_ata = True
                except Exception:
                    create_ata = True
                tx_bytes = create_spl_transfer(
                    self._hot_keypair, dest_pubkey, mint_pubkey,
                    req["amount"], decimals, recent_blockhash,
                    create_recipient_ata=create_ata,
                )
            else:
                tx_bytes = create_sol_transfer(
                    self._hot_keypair, dest_pubkey,
                    req["lamports"], recent_blockhash,
                )
        except Exception as e:
            logger.error("Failed to create transaction: %s", e)
            self._send_nack(
                header.msg_id, ErrorCode.RPC_ERROR,
                f"TX creation failed: {e}", sender_id,
            )
            return

        # Broadcast
        success, result = self._broadcast_to_solana(tx_bytes)
        self._send_tx_result(header.msg_id, success, result, sender_id)

    def _handle_balance_req(self, header: SolMeshHeader, payload: bytes,
                            sender_id: str) -> None:
        """Handle BALANCE_REQ messages. Query Solana RPC and respond."""
        if not self._check_rate_limit(sender_id, header.msg_id):
            return

        logger.info("BALANCE_REQ from %s", sender_id)

        try:
            req = decode_balance_req(payload)
        except ValueError as e:
            logger.error("Invalid BALANCE_REQ: %s", e)
            return

        pubkey = Pubkey.from_bytes(req["pubkey"])
        mint_bytes = req.get("mint", b"")

        try:
            if mint_bytes:
                mint_pubkey = Pubkey.from_bytes(mint_bytes)
                ata = find_associated_token_address(pubkey, mint_pubkey)
                resp = self._rpc.get_token_account_balance(ata)
                amount = int(resp.value.amount)
            else:
                resp = self._rpc.get_balance(pubkey)
                amount = resp.value
                mint_bytes = b""
        except Exception as e:
            logger.error("Balance query failed: %s", e)
            amount = 0

        resp_payload = encode_balance_resp(req["pubkey"], amount, mint=mint_bytes)
        resp_msg = pack_message(
            MsgType.BALANCE_RESP, generate_msg_id(), 0, 1, resp_payload
        )
        self._mesh.send(resp_msg, destination_id=sender_id)

    def _handle_blockhash_req(self, header: SolMeshHeader, payload: bytes,
                              sender_id: str) -> None:
        """Handle BLOCKHASH_REQ messages. Query RPC for latest blockhash."""
        if not self._check_rate_limit(sender_id, header.msg_id):
            return

        logger.info("BLOCKHASH_REQ from %s", sender_id)

        try:
            blockhash_resp = self._rpc.get_latest_blockhash()
            blockhash_bytes = bytes(blockhash_resp.value.blockhash)
        except Exception as e:
            logger.error("Blockhash fetch failed: %s", e)
            self._send_nack(
                header.msg_id, ErrorCode.RPC_ERROR,
                f"Blockhash fetch failed: {e}", sender_id,
            )
            return

        resp_payload = encode_blockhash_resp(blockhash_bytes)
        resp_msg = pack_message(
            MsgType.BLOCKHASH_RESP, generate_msg_id(), 0, 1, resp_payload
        )
        self._mesh.send(resp_msg, destination_id=sender_id)

    def _handle_addr_share(self, header: SolMeshHeader, payload: bytes,
                           sender_id: str) -> None:
        """Handle ADDR_SHARE messages. Store mesh-to-Solana address mapping."""
        try:
            data = decode_addr_share(payload)
        except ValueError as e:
            logger.error("Invalid ADDR_SHARE: %s", e)
            return

        self._known_addresses[sender_id] = data["pubkey"]
        pubkey = Pubkey.from_bytes(data["pubkey"])
        label = data["label"] or sender_id
        logger.info("Address registered: %s -> %s", label, pubkey)

        # Send ACK back to sender
        ack_payload = encode_ack(header.msg_id, 0)
        ack_msg = pack_message(
            MsgType.ACK, generate_msg_id(), 0, 1, ack_payload
        )
        self._mesh.send(ack_msg, destination_id=sender_id, want_ack=False)

    def _broadcast_to_solana(self, tx_bytes: bytes) -> tuple[bool, str]:
        """Send raw transaction bytes to Solana RPC."""
        try:
            tx = deserialize_transaction(tx_bytes)
            resp = self._rpc.send_raw_transaction(tx_bytes)
            sig = str(resp.value)
            logger.info("Transaction broadcast success: %s", sig)
            return True, sig
        except Exception as e:
            logger.error("Transaction broadcast failed: %s", e)
            return False, str(e)

    def _send_tx_result(self, original_msg_id: int, success: bool,
                        sig_or_error: str, destination_id: str) -> None:
        """Send a TX_RESULT message back to the requesting node."""
        if success:
            data = sig_or_error.encode("utf-8")
        else:
            data = sig_or_error.encode("utf-8")

        result_payload = encode_tx_result(original_msg_id, success, data)
        result_msg = pack_message(
            MsgType.TX_RESULT, generate_msg_id(), 0, 1, result_payload
        )
        self._mesh.send(result_msg, destination_id=destination_id)

    def _send_nack(self, original_msg_id: int, error_code: int,
                   error_msg: str, destination_id: str) -> None:
        """Send a NACK message back to the requesting node."""
        nack_payload = encode_nack(original_msg_id, error_code, error_msg)
        nack_msg = pack_message(
            MsgType.NACK, generate_msg_id(), 0, 1, nack_payload
        )
        self._mesh.send(nack_msg, destination_id=destination_id)
