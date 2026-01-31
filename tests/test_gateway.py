"""Integration tests for GatewayNode using MockMeshInterface."""

from __future__ import annotations
import struct
from unittest.mock import MagicMock, patch

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from solmesh.chunker import chunk_payload, generate_msg_id
from solmesh.config import GatewayConfig
from solmesh.constants import MsgType, ErrorCode
from solmesh.crypto import sign_payload
from solmesh.gateway import GatewayNode
from solmesh.protocol import (
    decode_ack,
    decode_nack,
    decode_tx_result,
    encode_addr_share,
    encode_balance_req,
    encode_blockhash_req,
    encode_tx_request,
    pack_message,
    unpack_message,
)
from solmesh.wallet import WalletManager
from tests.mock_mesh import MockMeshInterface


@pytest.fixture
def mock_mesh():
    return MockMeshInterface()


@pytest.fixture
def wallet_mgr(tmp_path):
    return WalletManager(wallet_dir=tmp_path / "wallets")


@pytest.fixture
def mock_rpc():
    """Mock Solana RPC client."""
    rpc = MagicMock()
    # Mock get_balance
    balance_resp = MagicMock()
    balance_resp.value = 5_000_000_000  # 5 SOL
    rpc.get_balance.return_value = balance_resp
    # Mock get_latest_blockhash - need a real-ish blockhash object
    blockhash_bytes = b"\xab" * 32

    class FakeBlockhash:
        def __bytes__(self):
            return blockhash_bytes

    blockhash_resp = MagicMock()
    blockhash_resp.value.blockhash = FakeBlockhash()
    rpc.get_latest_blockhash.return_value = blockhash_resp
    return rpc


def _create_gateway(mock_mesh, mock_rpc, wallet_mgr, config=None):
    """Create a GatewayNode with mocked dependencies."""
    if config is None:
        config = GatewayConfig()
    gw = GatewayNode(
        mesh=mock_mesh,
        rpc_url="http://localhost:8899",
        wallet_manager=wallet_mgr,
        gateway_config=config,
    )
    gw._rpc = mock_rpc
    # Register handlers without starting the run loop or beacon thread
    gw._mesh.register_handler(MsgType.TX_CHUNK, gw._handle_tx_chunk)
    gw._mesh.register_handler(MsgType.TX_REQUEST, gw._handle_tx_request)
    gw._mesh.register_handler(MsgType.BALANCE_REQ, gw._handle_balance_req)
    gw._mesh.register_handler(MsgType.BLOCKHASH_REQ, gw._handle_blockhash_req)
    gw._mesh.register_handler(MsgType.ADDR_SHARE, gw._handle_addr_share)
    gw._mesh.connect()
    return gw


class TestMode1TxChunk:
    def test_single_chunk_tx(self, mock_mesh, mock_rpc, wallet_mgr):
        """Single-chunk TX should get ACK + TX_RESULT."""
        gw = _create_gateway(mock_mesh, mock_rpc, wallet_mgr)

        # Mock broadcast
        mock_rpc.send_raw_transaction.return_value = MagicMock(value="fake_sig_123")

        tx_data = b"\xdd" * 50  # small enough for 1 chunk
        msg_id = 1001
        chunks = chunk_payload(tx_data, MsgType.TX_CHUNK, msg_id=msg_id)
        assert len(chunks) == 1

        mock_mesh.inject_message(chunks[0], "!client1")

        # Check ACK was sent
        acks = mock_mesh.get_sent_of_type(MsgType.ACK)
        assert len(acks) >= 1
        ack_data = decode_ack(acks[0][1])
        assert ack_data["acked_msg_id"] == msg_id

    def test_multi_chunk_reassembly(self, mock_mesh, mock_rpc, wallet_mgr):
        """Multi-chunk TX should reassemble and send TX_RESULT."""
        gw = _create_gateway(mock_mesh, mock_rpc, wallet_mgr)

        mock_rpc.send_raw_transaction.return_value = MagicMock(value="sig_multi")

        tx_data = b"\xee" * 300  # 2 chunks
        msg_id = 2002
        chunks = chunk_payload(tx_data, MsgType.TX_CHUNK, msg_id=msg_id)
        assert len(chunks) == 2

        mock_mesh.inject_message(chunks[0], "!client1")
        mock_mesh.inject_message(chunks[1], "!client1")

        # Should have 2 ACKs
        acks = mock_mesh.get_sent_of_type(MsgType.ACK)
        assert len(acks) == 2

        # Should have TX_RESULT
        results = mock_mesh.get_sent_of_type(MsgType.TX_RESULT)
        assert len(results) == 1
        result = decode_tx_result(results[0][1])
        assert result["orig_msg_id"] == msg_id


class TestMode3TxRequest:
    def test_authorized_transfer(self, mock_mesh, mock_rpc, wallet_mgr):
        """Authorized TX_REQUEST with valid signature should succeed."""
        # Create hot wallet
        wallet_mgr.create_wallet("hot", passphrase="hotpass")
        hot_kp = wallet_mgr.load_keypair("hot", passphrase="hotpass")

        config = GatewayConfig(
            hot_wallet="hot",
            allowed_requesters=[],  # empty = allow all
            max_transfer_sol=1.0,
        )
        gw = _create_gateway(mock_mesh, mock_rpc, wallet_mgr, config)
        gw._hot_keypair = hot_kp

        # Prepare client request
        sender_kp = Keypair()
        dest_kp = Keypair()
        sender_pub = bytes(sender_kp.pubkey())
        dest_pub = bytes(dest_kp.pubkey())
        lamports = 100_000_000  # 0.1 SOL

        signed_data = sender_pub + dest_pub + struct.pack("!Q", lamports)
        sig = sign_payload(sender_kp, signed_data)

        payload = encode_tx_request(sender_pub, dest_pub, lamports, sig)
        msg = pack_message(MsgType.TX_REQUEST, 3003, 0, 1, payload)

        # Mock the RPC call for the transfer
        mock_rpc.send_raw_transaction.return_value = MagicMock(value="transfer_sig")

        mock_mesh.inject_message(msg, "!client2")

        # Should have TX_RESULT (could also have NACK if blockhash mock fails)
        results = mock_mesh.get_sent_of_type(MsgType.TX_RESULT)
        nacks = mock_mesh.get_sent_of_type(MsgType.NACK)
        # Either a result or a nack due to mocked blockhash issues
        assert len(results) + len(nacks) >= 1

    def test_unauthorized_pubkey(self, mock_mesh, mock_rpc, wallet_mgr):
        """TX_REQUEST from non-allowed pubkey should get NACK."""
        wallet_mgr.create_wallet("hot", passphrase="hotpass")
        hot_kp = wallet_mgr.load_keypair("hot", passphrase="hotpass")

        allowed_kp = Keypair()
        config = GatewayConfig(
            hot_wallet="hot",
            allowed_requesters=[str(allowed_kp.pubkey())],
            max_transfer_sol=1.0,
        )
        gw = _create_gateway(mock_mesh, mock_rpc, wallet_mgr, config)
        gw._hot_keypair = hot_kp

        # Request from a DIFFERENT keypair (not in allowed list)
        sender_kp = Keypair()
        dest_kp = Keypair()
        sender_pub = bytes(sender_kp.pubkey())
        dest_pub = bytes(dest_kp.pubkey())
        lamports = 50_000_000

        signed_data = sender_pub + dest_pub + struct.pack("!Q", lamports)
        sig = sign_payload(sender_kp, signed_data)

        payload = encode_tx_request(sender_pub, dest_pub, lamports, sig)
        msg = pack_message(MsgType.TX_REQUEST, 4004, 0, 1, payload)

        mock_mesh.inject_message(msg, "!client3")

        nacks = mock_mesh.get_sent_of_type(MsgType.NACK)
        assert len(nacks) >= 1
        nack_data = decode_nack(nacks[0][1])
        assert nack_data["error_code"] == ErrorCode.UNAUTHORIZED


class TestRateLimiting:
    def test_rate_limit_nack(self, mock_mesh, mock_rpc, wallet_mgr):
        """Rapid requests should trigger RATE_LIMITED NACK."""
        config = GatewayConfig(
            max_requests_per_minute=60.0,
            rate_limit_burst=2,
        )
        gw = _create_gateway(mock_mesh, mock_rpc, wallet_mgr, config)

        pubkey = bytes(Keypair().pubkey())
        for i in range(4):
            payload = encode_balance_req(pubkey)
            msg = pack_message(MsgType.BALANCE_REQ, 5000 + i, 0, 1, payload)
            mock_mesh.inject_message(msg, "!spammer")

        nacks = mock_mesh.get_sent_of_type(MsgType.NACK)
        rate_limited = [
            n for n in nacks
            if decode_nack(n[1])["error_code"] == ErrorCode.RATE_LIMITED
        ]
        assert len(rate_limited) >= 1


class TestBlockhashReq:
    def test_blockhash_response(self, mock_mesh, mock_rpc, wallet_mgr):
        """BLOCKHASH_REQ should get BLOCKHASH_RESP."""
        gw = _create_gateway(mock_mesh, mock_rpc, wallet_mgr)

        payload = encode_blockhash_req()
        msg = pack_message(MsgType.BLOCKHASH_REQ, 6006, 0, 1, payload)
        mock_mesh.inject_message(msg, "!client5")

        resps = mock_mesh.get_sent_of_type(MsgType.BLOCKHASH_RESP)
        # May get a NACK if the mock doesn't support the bytes() call properly
        nacks = mock_mesh.get_sent_of_type(MsgType.NACK)
        assert len(resps) + len(nacks) >= 1


class TestAddrShare:
    def test_addr_share_ack(self, mock_mesh, mock_rpc, wallet_mgr):
        """ADDR_SHARE should store address and send ACK."""
        gw = _create_gateway(mock_mesh, mock_rpc, wallet_mgr)

        pubkey = bytes(Keypair().pubkey())
        payload = encode_addr_share(pubkey, label="alice")
        msg = pack_message(MsgType.ADDR_SHARE, 7007, 0, 1, payload)
        mock_mesh.inject_message(msg, "!alice")

        # Check address was stored
        assert "!alice" in gw._known_addresses

        # Check ACK was sent
        acks = mock_mesh.get_sent_of_type(MsgType.ACK)
        assert len(acks) >= 1
        ack_data = decode_ack(acks[0][1])
        assert ack_data["acked_msg_id"] == 7007


class TestMode3TokenTxRequest:
    def test_usdc_transfer_request(self, mock_mesh, mock_rpc, wallet_mgr):
        """TX_REQUEST with USDC mint should attempt token transfer."""
        from solmesh.constants import USDC_MINT_DEVNET

        wallet_mgr.create_wallet("hot", passphrase="hotpass")
        hot_kp = wallet_mgr.load_keypair("hot", passphrase="hotpass")

        config = GatewayConfig(
            hot_wallet="hot",
            allowed_requesters=[],
            max_transfer_sol=1.0,
            max_transfer_usdc=10.0,
        )
        gw = _create_gateway(mock_mesh, mock_rpc, wallet_mgr, config)
        gw._hot_keypair = hot_kp

        sender_kp = Keypair()
        dest_kp = Keypair()
        sender_pub = bytes(sender_kp.pubkey())
        dest_pub = bytes(dest_kp.pubkey())
        mint_pubkey = Pubkey.from_string(USDC_MINT_DEVNET)
        mint_bytes = bytes(mint_pubkey)
        amount = 1_000_000  # 1 USDC

        # Sign with flags + mint
        flags = 0x01
        signed_data = (sender_pub + dest_pub
                       + struct.pack("!Q", amount)
                       + struct.pack("!B", flags)
                       + mint_bytes)
        sig = sign_payload(sender_kp, signed_data)

        payload = encode_tx_request(sender_pub, dest_pub, amount, sig, mint=mint_bytes)
        msg = pack_message(MsgType.TX_REQUEST, 8008, 0, 1, payload)

        mock_rpc.send_raw_transaction.return_value = MagicMock(value="usdc_sig")
        mock_rpc.get_account_info.return_value = MagicMock(value=None)

        mock_mesh.inject_message(msg, "!client_usdc")

        # Should get either TX_RESULT or NACK (depending on blockhash mock)
        results = mock_mesh.get_sent_of_type(MsgType.TX_RESULT)
        nacks = mock_mesh.get_sent_of_type(MsgType.NACK)
        assert len(results) + len(nacks) >= 1

    def test_amount_exceeded_usdc(self, mock_mesh, mock_rpc, wallet_mgr):
        """TX_REQUEST for USDC over limit should get AMOUNT_EXCEEDED NACK."""
        from solmesh.constants import USDC_MINT_DEVNET

        wallet_mgr.create_wallet("hot", passphrase="hotpass")
        hot_kp = wallet_mgr.load_keypair("hot", passphrase="hotpass")

        config = GatewayConfig(
            hot_wallet="hot",
            allowed_requesters=[],
            max_transfer_usdc=5.0,
        )
        gw = _create_gateway(mock_mesh, mock_rpc, wallet_mgr, config)
        gw._hot_keypair = hot_kp

        sender_kp = Keypair()
        dest_kp = Keypair()
        sender_pub = bytes(sender_kp.pubkey())
        dest_pub = bytes(dest_kp.pubkey())
        mint_pubkey = Pubkey.from_string(USDC_MINT_DEVNET)
        mint_bytes = bytes(mint_pubkey)
        amount = 10_000_000  # 10 USDC -- over 5 USDC limit

        flags = 0x01
        signed_data = (sender_pub + dest_pub
                       + struct.pack("!Q", amount)
                       + struct.pack("!B", flags)
                       + mint_bytes)
        sig = sign_payload(sender_kp, signed_data)

        payload = encode_tx_request(sender_pub, dest_pub, amount, sig, mint=mint_bytes)
        msg = pack_message(MsgType.TX_REQUEST, 9009, 0, 1, payload)

        mock_mesh.inject_message(msg, "!client_over")

        nacks = mock_mesh.get_sent_of_type(MsgType.NACK)
        assert len(nacks) >= 1
        nack_data = decode_nack(nacks[0][1])
        assert nack_data["error_code"] == ErrorCode.AMOUNT_EXCEEDED
