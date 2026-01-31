"""Integration tests for ClientNode using MockMeshInterface."""

from __future__ import annotations
import threading
import time

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from solmesh.client import ClientNode
from solmesh.chunker import generate_msg_id
from solmesh.constants import MsgType, PROTOCOL_VERSION
from solmesh.protocol import (
    encode_ack,
    encode_balance_resp,
    encode_blockhash_resp,
    encode_gateway_beacon,
    encode_tx_result,
    pack_message,
    unpack_message,
    BEACON_CAP_RELAY,
    BEACON_CAP_BALANCE,
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
def client(mock_mesh, wallet_mgr):
    c = ClientNode(
        mesh=mock_mesh,
        wallet_manager=wallet_mgr,
        gateway_node_id="!gateway01",
    )
    c.connect()
    return c


class TestRelayRawTx:
    def test_sends_tx_chunks(self, client, mock_mesh):
        """relay_raw_tx should send TX_CHUNK messages."""
        tx_bytes = b"\xaa" * 300  # 2 chunks
        msg_id = client.relay_raw_tx(tx_bytes)
        assert isinstance(msg_id, int)

        # Give the background thread a moment to send
        time.sleep(0.1)

        chunks = mock_mesh.get_sent_of_type(MsgType.TX_CHUNK)
        assert len(chunks) == 2

    def test_wait_for_result_success(self, client, mock_mesh):
        """Injecting TX_RESULT should unblock wait_for_result."""
        tx_bytes = b"\xbb" * 100
        msg_id = client.relay_raw_tx(tx_bytes)

        # Simulate gateway sending TX_RESULT
        sig = b"fake_signature_string"
        result_payload = encode_tx_result(msg_id, True, sig)
        result_msg = pack_message(MsgType.TX_RESULT, generate_msg_id(), 0, 1, result_payload)

        def inject():
            time.sleep(0.1)
            mock_mesh.inject_message(result_msg, "!gateway01")

        t = threading.Thread(target=inject)
        t.start()

        result = client.wait_for_result(msg_id, timeout=5)
        t.join()

        assert result is not None
        assert result["success"] is True
        assert result["signature"] == "fake_signature_string"

    def test_wait_for_result_timeout(self, client):
        """wait_for_result should return None on timeout."""
        result = client.wait_for_result(99999, timeout=0.1)
        assert result is None


class TestShareAddress:
    def test_sends_addr_share(self, client, wallet_mgr, mock_mesh):
        """share_address should send ADDR_SHARE message."""
        wallet_mgr.create_wallet("test", passphrase="pass")
        # share_address now blocks waiting for ACK, so run in thread with short timeout
        # We won't ACK, so it will retry and return False
        def run():
            return client.share_address("test")

        # Patch ACK_TIMEOUT to be very short for testing
        import solmesh.client as client_mod
        orig_timeout = client_mod.ACK_TIMEOUT
        client_mod.ACK_TIMEOUT = 0.05
        orig_retries = client_mod.MAX_RETRIES
        client_mod.MAX_RETRIES = 0

        try:
            result = run()
            assert result is False  # No ACK received
            shares = mock_mesh.get_sent_of_type(MsgType.ADDR_SHARE)
            assert len(shares) >= 1
        finally:
            client_mod.ACK_TIMEOUT = orig_timeout
            client_mod.MAX_RETRIES = orig_retries


class TestRequestTransfer:
    def test_sends_tx_request(self, client, wallet_mgr, mock_mesh):
        """request_transfer should send TX_REQUEST message."""
        wallet_mgr.create_wallet("sender", passphrase="pass")
        dest = str(Keypair().pubkey())

        msg_id = client.request_transfer(
            wallet_name="sender",
            destination=dest,
            amount_sol=0.01,
            passphrase="pass",
        )
        assert isinstance(msg_id, int)
        requests = mock_mesh.get_sent_of_type(MsgType.TX_REQUEST)
        assert len(requests) == 1


class TestFetchBlockhash:
    def test_fetch_blockhash(self, client, mock_mesh):
        """fetch_blockhash should send BLOCKHASH_REQ and return response."""
        fake_hash = b"\xcc" * 32

        def inject():
            time.sleep(0.1)
            resp_payload = encode_blockhash_resp(fake_hash)
            resp_msg = pack_message(MsgType.BLOCKHASH_RESP, generate_msg_id(), 0, 1, resp_payload)
            mock_mesh.inject_message(resp_msg, "!gateway01")

        t = threading.Thread(target=inject)
        t.start()

        result = client.fetch_blockhash(timeout=5)
        t.join()

        assert result == fake_hash

        reqs = mock_mesh.get_sent_of_type(MsgType.BLOCKHASH_REQ)
        assert len(reqs) == 1


class TestCheckBalance:
    def test_check_balance(self, client, mock_mesh):
        """check_balance should send BALANCE_REQ and wait_for_balance should return response."""
        pubkey = Keypair().pubkey()
        address = str(pubkey)

        def inject():
            time.sleep(0.1)
            resp_payload = encode_balance_resp(bytes(pubkey), 1_500_000_000)
            resp_msg = pack_message(MsgType.BALANCE_RESP, generate_msg_id(), 0, 1, resp_payload)
            mock_mesh.inject_message(resp_msg, "!gateway01")

        t = threading.Thread(target=inject)
        t.start()

        client.check_balance(address)
        result = client.wait_for_balance(timeout=5)
        t.join()

        assert result is not None
        assert result["lamports"] == 1_500_000_000
        assert result["sol"] == 1.5


class TestGatewayDiscovery:
    def test_auto_discover(self, mock_mesh, wallet_mgr):
        """discover_gateway should set gateway_id from beacon."""
        client = ClientNode(mesh=mock_mesh, wallet_manager=wallet_mgr)
        client.connect()

        caps = BEACON_CAP_RELAY | BEACON_CAP_BALANCE
        beacon_payload = encode_gateway_beacon(PROTOCOL_VERSION, caps, uptime_seconds=60)
        beacon_msg = pack_message(MsgType.GATEWAY_BEACON, generate_msg_id(), 0, 1, beacon_payload)

        def inject():
            time.sleep(0.1)
            mock_mesh.inject_message(beacon_msg, "!gw_auto")

        t = threading.Thread(target=inject)
        t.start()

        gw_id = client.discover_gateway(timeout=5)
        t.join()

        assert gw_id == "!gw_auto"
        assert client._gateway_id == "!gw_auto"

    def test_is_gateway_online(self, mock_mesh, wallet_mgr):
        """is_gateway_online should reflect beacon freshness."""
        client = ClientNode(mesh=mock_mesh, wallet_manager=wallet_mgr, gateway_node_id="!gw1")
        client.connect()

        assert client.is_gateway_online() is False

        # Inject a beacon
        caps = BEACON_CAP_RELAY
        beacon_payload = encode_gateway_beacon(PROTOCOL_VERSION, caps, uptime_seconds=10)
        beacon_msg = pack_message(MsgType.GATEWAY_BEACON, generate_msg_id(), 0, 1, beacon_payload)
        mock_mesh.inject_message(beacon_msg, "!gw1")

        assert client.is_gateway_online() is True


class TestConditionRace:
    def test_result_set_from_another_thread(self, client, mock_mesh):
        """Result set from handler thread should wake up wait_for_result."""
        fake_msg_id = 12345

        def inject():
            time.sleep(0.2)
            result_payload = encode_tx_result(fake_msg_id, False, b"simulated error")
            result_msg = pack_message(MsgType.TX_RESULT, generate_msg_id(), 0, 1, result_payload)
            mock_mesh.inject_message(result_msg, "!gateway01")

        t = threading.Thread(target=inject)
        t.start()

        result = client.wait_for_result(fake_msg_id, timeout=5)
        t.join()

        assert result is not None
        assert result["success"] is False
        assert "simulated error" in result["error"]


class TestRequestTokenTransfer:
    def test_sends_token_tx_request(self, client, wallet_mgr, mock_mesh):
        """request_token_transfer should send TX_REQUEST with mint."""
        from solmesh.constants import USDC_MINT_DEVNET
        wallet_mgr.create_wallet("sender", passphrase="pass")
        dest = str(Keypair().pubkey())

        msg_id = client.request_token_transfer(
            wallet_name="sender", destination=dest,
            mint_address=USDC_MINT_DEVNET, amount=1.0,
            decimals=6, passphrase="pass",
        )
        assert isinstance(msg_id, int)
        requests = mock_mesh.get_sent_of_type(MsgType.TX_REQUEST)
        assert len(requests) == 1

        # Verify the payload contains the mint
        from solmesh.protocol import decode_tx_request
        req = decode_tx_request(requests[0][1])
        assert req["flags"] & 0x01
        assert len(req["mint"]) == 32
        assert req["amount"] == 1_000_000  # 1 USDC in base units

    def test_requires_gateway(self, mock_mesh, wallet_mgr):
        """request_token_transfer without gateway should raise."""
        from solmesh.constants import USDC_MINT_DEVNET
        client = ClientNode(mesh=mock_mesh, wallet_manager=wallet_mgr)
        client.connect()
        wallet_mgr.create_wallet("sender2", passphrase="pass")
        with pytest.raises(ValueError, match="Gateway node ID not set"):
            client.request_token_transfer(
                wallet_name="sender2", destination=str(Keypair().pubkey()),
                mint_address=USDC_MINT_DEVNET, amount=1.0, passphrase="pass",
            )


class TestCheckTokenBalance:
    def test_sends_token_balance_req(self, client, mock_mesh):
        """check_token_balance should send BALANCE_REQ with mint."""
        from solmesh.constants import USDC_MINT_DEVNET
        pubkey = Keypair().pubkey()
        address = str(pubkey)

        msg_id = client.check_token_balance(address, USDC_MINT_DEVNET)
        assert isinstance(msg_id, int)
        reqs = mock_mesh.get_sent_of_type(MsgType.BALANCE_REQ)
        assert len(reqs) == 1

        from solmesh.protocol import decode_balance_req
        req = decode_balance_req(reqs[0][1])
        assert len(req["mint"]) == 32

    def test_requires_gateway(self, mock_mesh, wallet_mgr):
        """check_token_balance without gateway should raise."""
        from solmesh.constants import USDC_MINT_DEVNET
        client = ClientNode(mesh=mock_mesh, wallet_manager=wallet_mgr)
        client.connect()
        with pytest.raises(ValueError, match="Gateway node ID not set"):
            client.check_token_balance(str(Keypair().pubkey()), USDC_MINT_DEVNET)
