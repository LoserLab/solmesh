"""Integration tests for store-and-forward (deferred) flows."""

from __future__ import annotations

import threading
import time

import pytest
from solders.keypair import Keypair

from solmesh.client import ClientNode
from solmesh.chunker import generate_msg_id
from solmesh.constants import MAX_FLUSH_ATTEMPTS, MsgType, PROTOCOL_VERSION
from solmesh.protocol import (
    BEACON_CAP_RELAY,
    BEACON_CAP_BALANCE,
    encode_blockhash_resp,
    encode_gateway_beacon,
    encode_tx_result,
    pack_message,
)
from solmesh.store import Intent, IntentStatus, IntentStore
from solmesh.wallet import WalletManager
from tests.mock_mesh import MockMeshInterface


@pytest.fixture
def mock_mesh():
    return MockMeshInterface()


@pytest.fixture
def wallet_mgr(tmp_path):
    return WalletManager(wallet_dir=tmp_path / "wallets")


@pytest.fixture
def intent_store(tmp_path):
    return IntentStore(queue_file=tmp_path / "queue.json")


@pytest.fixture
def client(mock_mesh, wallet_mgr, intent_store):
    c = ClientNode(
        mesh=mock_mesh,
        wallet_manager=wallet_mgr,
        gateway_node_id="!gateway01",
        intent_store=intent_store,
    )
    c.connect()
    return c


def _inject_beacon(mock_mesh, sender="!gateway01"):
    """Inject a gateway beacon message."""
    caps = BEACON_CAP_RELAY | BEACON_CAP_BALANCE
    payload = encode_gateway_beacon(PROTOCOL_VERSION, caps, uptime_seconds=60)
    msg = pack_message(MsgType.GATEWAY_BEACON, generate_msg_id(), 0, 1, payload)
    mock_mesh.inject_message(msg, sender)


class TestQueueIntent:
    def test_queue_mode3(self, client, wallet_mgr, intent_store):
        """Queueing a mode 3 intent should persist it as PENDING."""
        wallet_mgr.create_wallet("alice", passphrase="pass")
        dest = str(Keypair().pubkey())

        intent = client.queue_intent(
            mode=3, wallet_name="alice", recipient=dest,
            amount=0.5, passphrase="pass",
        )
        assert intent.status == IntentStatus.PENDING.value
        assert intent.mode == 3
        assert intent.wallet_name == "alice"

        stored = intent_store.pending_intents()
        assert len(stored) == 1
        assert stored[0]["wallet_name"] == "alice"

    def test_queue_mode1(self, client, wallet_mgr, intent_store):
        """Queueing a mode 1 intent should persist it as PENDING."""
        wallet_mgr.create_wallet("bob", passphrase="pass")
        dest = str(Keypair().pubkey())

        intent = client.queue_intent(
            mode=1, wallet_name="bob", recipient=dest,
            amount=1.0, passphrase="pass",
        )
        assert intent.mode == 1
        assert intent_store.pending_intents()[0]["mode"] == 1

    def test_queue_validates_wallet(self, client):
        """Queueing for a nonexistent wallet should raise."""
        with pytest.raises(FileNotFoundError):
            client.queue_intent(
                mode=3, wallet_name="nonexistent",
                recipient=str(Keypair().pubkey()), amount=1.0,
            )

    def test_queue_validates_passphrase(self, client, wallet_mgr):
        """Queueing with a wrong passphrase should raise."""
        wallet_mgr.create_wallet("carol", passphrase="right")
        with pytest.raises(Exception):
            client.queue_intent(
                mode=3, wallet_name="carol",
                recipient=str(Keypair().pubkey()), amount=1.0,
                passphrase="wrong",
            )


class TestFlushIntent:
    def test_flush_mode3_sends_tx_request(self, client, wallet_mgr, mock_mesh,
                                           intent_store):
        """Flushing a mode 3 intent should send TX_REQUEST and set SENT."""
        wallet_mgr.create_wallet("alice", passphrase="pass")
        dest = str(Keypair().pubkey())

        intent = client.queue_intent(
            mode=3, wallet_name="alice", recipient=dest,
            amount=0.01, passphrase="pass",
        )

        # Ensure gateway is "online" (beacon seen recently)
        _inject_beacon(mock_mesh)

        # Inject TX_RESULT in background thread
        def inject_result():
            time.sleep(0.2)
            # Find the msg_id from the sent TX_REQUEST
            reqs = mock_mesh.get_sent_of_type(MsgType.TX_REQUEST)
            if reqs:
                msg_id = reqs[0][0].msg_id
                result_payload = encode_tx_result(msg_id, True, b"sig_abc123")
                result_msg = pack_message(
                    MsgType.TX_RESULT, generate_msg_id(), 0, 1, result_payload,
                )
                mock_mesh.inject_message(result_msg, "!gateway01")

        t = threading.Thread(target=inject_result)
        t.start()

        result = client.flush_intent(intent_store.get(intent.id), "pass")
        t.join()

        assert result["success"] is True
        stored = intent_store.get(intent.id)
        assert stored["status"] == IntentStatus.SENT.value
        assert stored["result_tx_hash"] == "sig_abc123"

        reqs = mock_mesh.get_sent_of_type(MsgType.TX_REQUEST)
        assert len(reqs) >= 1

    def test_flush_mode1_fetches_blockhash(self, client, wallet_mgr, mock_mesh,
                                            intent_store):
        """Flushing a mode 1 intent should fetch blockhash then send TX_CHUNK."""
        wallet_mgr.create_wallet("bob", passphrase="pass")
        dest = str(Keypair().pubkey())

        intent = client.queue_intent(
            mode=1, wallet_name="bob", recipient=dest,
            amount=0.001, passphrase="pass",
        )

        _inject_beacon(mock_mesh)

        def inject_responses():
            time.sleep(0.1)
            # Inject blockhash response
            fake_hash = b"\xcc" * 32
            resp_payload = encode_blockhash_resp(fake_hash)
            resp_msg = pack_message(
                MsgType.BLOCKHASH_RESP, generate_msg_id(), 0, 1, resp_payload,
            )
            mock_mesh.inject_message(resp_msg, "!gateway01")

            # Poll for TX_CHUNK to be sent, then inject TX_RESULT
            chunks = []
            for _ in range(50):  # poll up to 5 seconds
                time.sleep(0.1)
                chunks = mock_mesh.get_sent_of_type(MsgType.TX_CHUNK)
                if chunks:
                    break
            if chunks:
                from solmesh.protocol import unpack_message
                hdr, _ = unpack_message(mock_mesh.sent_messages[-1]["data"])
                msg_id = hdr.msg_id
                # Find the actual msg_id from the chunks
                for ch_hdr, _ in chunks:
                    msg_id = ch_hdr.msg_id
                    break
                result_payload = encode_tx_result(msg_id, True, b"sig_mode1")
                result_msg = pack_message(
                    MsgType.TX_RESULT, generate_msg_id(), 0, 1, result_payload,
                )
                mock_mesh.inject_message(result_msg, "!gateway01")

        t = threading.Thread(target=inject_responses)
        t.start()

        result = client.flush_intent(intent_store.get(intent.id), "pass")
        t.join()

        # Check blockhash was requested
        bh_reqs = mock_mesh.get_sent_of_type(MsgType.BLOCKHASH_REQ)
        assert len(bh_reqs) >= 1

        # Check TX_CHUNK was sent
        chunks = mock_mesh.get_sent_of_type(MsgType.TX_CHUNK)
        assert len(chunks) >= 1

    def test_wallet_filter(self, client, wallet_mgr, intent_store, mock_mesh):
        """flush_all_pending with wallet_filter should only flush matching intents."""
        wallet_mgr.create_wallet("alice", passphrase="pass")
        wallet_mgr.create_wallet("bob", passphrase="pass2")
        dest = str(Keypair().pubkey())

        client.queue_intent(mode=3, wallet_name="alice", recipient=dest,
                            amount=0.01, passphrase="pass")
        client.queue_intent(mode=3, wallet_name="bob", recipient=str(Keypair().pubkey()),
                            amount=0.02, passphrase="pass2")

        _inject_beacon(mock_mesh)

        # Flush only alice's intents -- bob should be skipped
        results = client.flush_all_pending(
            passphrase_map={"alice": "pass", "bob": "pass2"},
            wallet_filter="alice",
        )

        # Only alice's intent should have been attempted
        reqs = mock_mesh.get_sent_of_type(MsgType.TX_REQUEST)
        assert len(reqs) == 1

    def test_max_attempts_reaches_failed(self, client, wallet_mgr, intent_store,
                                          mock_mesh):
        """An intent should reach FAILED after MAX_FLUSH_ATTEMPTS timeouts."""
        wallet_mgr.create_wallet("alice", passphrase="pass")
        dest = str(Keypair().pubkey())

        intent = client.queue_intent(
            mode=3, wallet_name="alice", recipient=dest,
            amount=0.01, passphrase="pass",
        )

        _inject_beacon(mock_mesh)

        # Flush repeatedly with short timeout (no TX_RESULT injected = timeout)
        import solmesh.client as client_mod
        orig_timeout = client_mod.ACK_TIMEOUT
        client_mod.ACK_TIMEOUT = 0.01

        try:
            for _ in range(MAX_FLUSH_ATTEMPTS):
                stored = intent_store.get(intent.id)
                if stored["status"] == IntentStatus.FAILED.value:
                    break
                client.flush_intent(stored, "pass")
        finally:
            client_mod.ACK_TIMEOUT = orig_timeout

        stored = intent_store.get(intent.id)
        assert stored["status"] == IntentStatus.FAILED.value
        assert stored["attempts"] >= MAX_FLUSH_ATTEMPTS


class TestAutoFlush:
    def test_auto_flush_on_beacon(self, mock_mesh, wallet_mgr, intent_store):
        """Auto-flush should trigger when a beacon arrives with pending intents."""
        wallet_mgr.create_wallet("alice", passphrase="pass")
        dest = str(Keypair().pubkey())

        client = ClientNode(
            mesh=mock_mesh,
            wallet_manager=wallet_mgr,
            gateway_node_id="!gateway01",
            intent_store=intent_store,
            auto_flush=True,
        )
        client.cache_passphrase("alice", "pass")
        client.connect()

        intent = client.queue_intent(
            mode=3, wallet_name="alice", recipient=dest,
            amount=0.01, passphrase="pass",
        )

        # Inject beacon — should trigger auto-flush in background
        _inject_beacon(mock_mesh)

        # Give the auto-flush thread time to start and send
        time.sleep(0.5)

        # The intent should have been attempted (TX_REQUEST sent)
        reqs = mock_mesh.get_sent_of_type(MsgType.TX_REQUEST)
        assert len(reqs) >= 1


class TestNoPassphrase:
    def test_flush_skips_without_passphrase(self, client, wallet_mgr, intent_store,
                                             mock_mesh):
        """Flushing without a passphrase should skip the intent."""
        wallet_mgr.create_wallet("alice", passphrase="pass")
        dest = str(Keypair().pubkey())

        client.queue_intent(
            mode=3, wallet_name="alice", recipient=dest,
            amount=0.01, passphrase="pass",
        )

        _inject_beacon(mock_mesh)

        # Clear the passphrase cache (queue_intent caches it on validation)
        client._passphrase_cache.clear()

        # Flush with empty passphrase map and no cache
        results = client.flush_all_pending(passphrase_map={})

        # Should be empty — intent was skipped
        assert len(results) == 0

        # No TX_REQUEST should have been sent
        reqs = mock_mesh.get_sent_of_type(MsgType.TX_REQUEST)
        assert len(reqs) == 0

        # Intent should still be PENDING
        pending = intent_store.pending_intents()
        assert len(pending) == 1
