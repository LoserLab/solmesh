"""Tests for the HTTP API layer."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")

from solders.keypair import Keypair

from solmesh.config import GatewayConfig
from solmesh.gateway import GatewayNode
from solmesh.http_api import create_api
from solmesh.wallet import WalletManager
from tests.mock_mesh import MockMeshInterface


# --- Fixtures ---


@pytest.fixture
def mock_rpc():
    """Mock Solana RPC client."""
    rpc = MagicMock()

    balance_resp = MagicMock()
    balance_resp.value = 5_000_000_000  # 5 SOL
    rpc.get_balance.return_value = balance_resp

    class _FakeBlockhash:
        def __bytes__(self):
            return b"\xab" * 32

        def __str__(self):
            return "FakeBlockhash1111111111111111111111111111111"

    blockhash_resp = MagicMock()
    blockhash_resp.value.blockhash = _FakeBlockhash()
    blockhash_resp.value.last_valid_block_height = 12345
    rpc.get_latest_blockhash.return_value = blockhash_resp

    slot_resp = MagicMock()
    slot_resp.value = 999999
    rpc.get_slot.return_value = slot_resp

    return rpc


@pytest.fixture
def gateway_with_http(mock_rpc, tmp_path):
    """GatewayNode with HTTP config and a hot wallet."""
    wm = WalletManager(wallet_dir=tmp_path / "wallets")
    wm.create_wallet("hot", passphrase="hotpass")
    hot_kp = wm.load_keypair("hot", passphrase="hotpass")

    config = GatewayConfig(
        hot_wallet="hot",
        http_port=8080,
        api_key="test-api-key-12345",
        max_transfer_sol=1.0,
        max_transfer_usdc=10.0,
    )
    mesh = MockMeshInterface()
    mesh.connect()
    gw = GatewayNode(
        mesh=mesh,
        rpc_url="http://localhost:8899",
        wallet_manager=wm,
        gateway_config=config,
    )
    gw._rpc = mock_rpc
    gw._hot_keypair = hot_kp
    gw._start_time = 1_000_000.0
    return gw


@pytest.fixture
def client(gateway_with_http):
    """FastAPI TestClient wrapping the gateway's HTTP API."""
    from starlette.testclient import TestClient

    app = create_api(gateway_with_http)
    return TestClient(app)


API_KEY = {"X-API-Key": "test-api-key-12345"}


# --- Auth tests ---


class TestAuth:
    def test_missing_api_key_returns_422(self, client):
        """Request without X-API-Key header gets 422."""
        resp = client.get("/v1/status")
        assert resp.status_code == 422

    def test_wrong_api_key_returns_401(self, client):
        """Incorrect API key gets 401."""
        resp = client.get("/v1/status", headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

    def test_valid_api_key_returns_200(self, client):
        """Valid API key passes authentication."""
        resp = client.get("/v1/status", headers=API_KEY)
        assert resp.status_code == 200


# --- Rate limit tests ---


class TestRateLimit:
    def test_rate_limit_exhaustion_returns_429(self, client):
        """Rapid requests beyond burst should return 429."""
        # Default burst is 3 from GatewayConfig
        for _ in range(10):
            resp = client.get("/v1/status", headers=API_KEY)
        assert resp.status_code == 429

    def test_rate_limit_uses_http_prefix(self, client, gateway_with_http):
        """HTTP rate limit bucket should be keyed with http: prefix."""
        client.get("/v1/status", headers=API_KEY)
        buckets = gateway_with_http._rate_limiter._buckets
        assert any(k.startswith("http:") for k in buckets)


# --- Status endpoint tests ---


class TestStatusEndpoint:
    def test_status_response_fields(self, client, gateway_with_http):
        """GET /v1/status returns expected fields."""
        resp = client.get("/v1/status", headers=API_KEY)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["hot_wallet"] is not None
        assert isinstance(data["uptime_seconds"], int)
        assert data["mesh_connected"] is True
        assert "max_per_minute" in data["rate_limit"]
        assert "burst" in data["rate_limit"]


# --- Balance endpoint tests ---


class TestBalanceEndpoint:
    def test_sol_balance(self, client):
        """GET /v1/balance/{address} returns SOL balance."""
        addr = str(Keypair().pubkey())
        resp = client.get(f"/v1/balance/{addr}", headers=API_KEY)
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "SOL"
        assert data["balance_raw"] == 5_000_000_000
        assert data["decimals"] == 9
        assert data["balance_human"] == 5.0

    def test_invalid_address_returns_400(self, client):
        """Invalid Solana address returns 400."""
        resp = client.get("/v1/balance/not-valid", headers=API_KEY)
        assert resp.status_code == 400

    def test_rpc_error_returns_502(self, client, gateway_with_http):
        """RPC failure returns 502."""
        gateway_with_http._rpc.get_balance.side_effect = Exception("RPC down")
        addr = str(Keypair().pubkey())
        resp = client.get(f"/v1/balance/{addr}", headers=API_KEY)
        assert resp.status_code == 502


# --- Blockhash endpoint tests ---


class TestBlockhashEndpoint:
    def test_blockhash_response(self, client):
        """GET /v1/blockhash returns blockhash and height."""
        resp = client.get("/v1/blockhash", headers=API_KEY)
        assert resp.status_code == 200
        data = resp.json()
        assert "blockhash" in data
        assert data["last_valid_block_height"] == 12345


# --- Slot endpoint tests ---


class TestSlotEndpoint:
    def test_slot_response(self, client):
        """GET /v1/slot returns current slot."""
        resp = client.get("/v1/slot", headers=API_KEY)
        assert resp.status_code == 200
        assert resp.json()["slot"] == 999999


# --- Transfer endpoint tests ---


class TestTransferEndpoint:
    def test_no_hot_wallet_returns_503(self, client, gateway_with_http):
        """Transfer without hot wallet returns 503."""
        gateway_with_http._hot_keypair = None
        dest = str(Keypair().pubkey())
        resp = client.post(
            "/v1/transfer",
            json={"destination": dest, "amount": 0.01},
            headers=API_KEY,
        )
        assert resp.status_code == 503

    def test_invalid_destination_returns_400(self, client):
        """Transfer to invalid address returns 400."""
        resp = client.post(
            "/v1/transfer",
            json={"destination": "not-valid", "amount": 0.01},
            headers=API_KEY,
        )
        assert resp.status_code == 400

    def test_amount_exceeded_returns_400(self, client):
        """Transfer over max_transfer_sol returns 400."""
        dest = str(Keypair().pubkey())
        resp = client.post(
            "/v1/transfer",
            json={"destination": dest, "amount": 99.0},
            headers=API_KEY,
        )
        assert resp.status_code == 400
        assert "exceeds" in resp.json()["detail"].lower()

    def test_successful_sol_transfer(self, client, gateway_with_http):
        """Successful SOL transfer returns signature."""
        send_resp = MagicMock()
        send_resp.value = "5wHGkvPcxPnFakeSignature"
        gateway_with_http._rpc.send_raw_transaction.return_value = send_resp

        dest = str(Keypair().pubkey())
        resp = client.post(
            "/v1/transfer",
            json={"destination": dest, "amount": 0.01},
            headers=API_KEY,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "success" in data


# --- OpenAPI spec test ---


class TestOpenApiSpec:
    def test_openapi_json_available(self, client):
        """OpenAPI spec should be available at /openapi.json."""
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert spec["info"]["title"] == "SolMesh Gateway API"
        paths = list(spec["paths"].keys())
        assert "/v1/status" in paths
        assert "/v1/balance/{address}" in paths
        assert "/v1/blockhash" in paths
        assert "/v1/slot" in paths
        assert "/v1/transfer" in paths
