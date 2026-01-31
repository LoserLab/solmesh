"""YAML configuration loading for SolMesh."""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from solmesh.constants import (
    DEFAULT_RPC_URL,
    DEVNET_RPC_URL,
    TESTNET_RPC_URL,
    USDC_MINT_MAINNET,
    USDC_MINT_DEVNET,
)

logger = logging.getLogger(__name__)

RPC_URLS = {
    "mainnet-beta": DEFAULT_RPC_URL,
    "devnet": DEVNET_RPC_URL,
    "testnet": TESTNET_RPC_URL,
}


@dataclass
class MeshConfig:
    connection_type: str = "serial"
    device_path: Optional[str] = None
    hostname: Optional[str] = None


@dataclass
class SolanaConfig:
    network: str = "devnet"
    rpc_url: Optional[str] = None


@dataclass
class GatewayConfig:
    hot_wallet: Optional[str] = None
    allowed_requesters: list[str] = field(default_factory=list)
    max_transfer_sol: float = 0.1
    max_transfer_usdc: float = 10.0
    token_limits: dict[str, float] = field(default_factory=dict)
    max_requests_per_minute: float = 10.0
    rate_limit_burst: int = 3
    beacon_interval: int = 60
    http_port: Optional[int] = None
    api_key: Optional[str] = None

    def get_max_transfer_token(self, mint_address: str) -> Optional[float]:
        """Get the max transfer amount for a token by its mint address.

        Returns None if the token has no configured limit.
        """
        if mint_address in self.token_limits:
            return self.token_limits[mint_address]
        if mint_address in (USDC_MINT_MAINNET, USDC_MINT_DEVNET):
            return self.max_transfer_usdc
        return None


@dataclass
class SolMeshConfig:
    mesh: MeshConfig = field(default_factory=MeshConfig)
    solana: SolanaConfig = field(default_factory=SolanaConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    log_level: str = "INFO"


def load_config(path: Path) -> SolMeshConfig:
    """Load configuration from a YAML file."""
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    config = SolMeshConfig()

    if "mesh" in raw:
        m = raw["mesh"]
        config.mesh = MeshConfig(
            connection_type=m.get("connection_type", "serial"),
            device_path=m.get("device_path"),
            hostname=m.get("hostname"),
        )

    if "solana" in raw:
        s = raw["solana"]
        config.solana = SolanaConfig(
            network=s.get("network", "devnet"),
            rpc_url=s.get("rpc_url"),
        )

    if "gateway" in raw:
        g = raw["gateway"]
        config.gateway = GatewayConfig(
            hot_wallet=g.get("hot_wallet"),
            allowed_requesters=g.get("allowed_requesters", []),
            max_transfer_sol=g.get("max_transfer_sol", 0.1),
            max_transfer_usdc=g.get("max_transfer_usdc", 10.0),
            token_limits=g.get("token_limits", {}),
            max_requests_per_minute=g.get("max_requests_per_minute", 10.0),
            rate_limit_burst=g.get("rate_limit_burst", 3),
            beacon_interval=g.get("beacon_interval", 60),
            http_port=g.get("http_port"),
            api_key=g.get("api_key"),
        )

    config.log_level = raw.get("log_level", "INFO")
    return config


def get_rpc_url(config: SolanaConfig) -> str:
    """Resolve RPC URL from config (custom URL or network name)."""
    if config.rpc_url:
        return config.rpc_url
    return RPC_URLS.get(config.network, DEVNET_RPC_URL)
