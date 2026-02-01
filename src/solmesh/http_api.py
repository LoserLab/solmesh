"""FastAPI HTTP API for the SolMesh gateway.

Exposes gateway blockchain operations as REST endpoints. The API is
optional — install with ``pip install solmesh[http]``.

Uses a factory pattern: ``create_api(gateway)`` returns a FastAPI app
with all endpoints bound to the gateway instance via closures.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


# --- Pydantic models ---

class StatusResponse(BaseModel):
    status: str = "ok"
    hot_wallet: Optional[str] = None
    uptime_seconds: int
    mesh_connected: bool
    rate_limit: dict


class BalanceResponse(BaseModel):
    address: str
    token: Optional[str] = None
    balance_raw: int
    balance_human: float
    decimals: int
    symbol: str


class BlockhashResponse(BaseModel):
    blockhash: str
    last_valid_block_height: int


class SlotResponse(BaseModel):
    slot: int


class TransferRequest(BaseModel):
    destination: str
    amount: float
    token: Optional[str] = None


class TransferResponse(BaseModel):
    success: bool
    signature: Optional[str] = None
    error: Optional[str] = None


class ErrorResponse(BaseModel):
    detail: str


# --- Helpers ---

def _resolve_token_mint(token_input: str) -> str:
    """Resolve 'USDC'/'FXN' shorthand or return raw mint address."""
    from solmesh.constants import USDC_MINT_DEVNET, FXN_MINT_MAINNET

    upper = token_input.upper()
    if upper == "USDC":
        return USDC_MINT_DEVNET
    if upper == "FXN":
        return FXN_MINT_MAINNET
    return token_input


# --- App factory ---

def create_api(gateway) -> "FastAPI":
    """Create a FastAPI application bound to the given gateway.

    All endpoints share the gateway's RPC client, rate limiter,
    and hot wallet keypair. Dependencies are closures that capture
    the gateway reference — no module-level singletons.
    """
    from fastapi import Depends, FastAPI, Header, HTTPException

    app = FastAPI(
        title="SolMesh Gateway API",
        version="0.1.0",
        description="REST API for the SolMesh gateway (Solana over LoRa).",
    )

    # --- Auth / rate-limit dependencies ---

    def verify_api_key(x_api_key: str = Header(...)) -> str:
        """Validate X-API-Key header against gateway config."""
        expected = gateway._config.api_key
        if not expected or x_api_key != expected:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return x_api_key

    def check_rate_limit(x_api_key: str = Depends(verify_api_key)) -> str:
        """Check per-caller rate limit using the gateway's rate limiter."""
        key_prefix = x_api_key[:8] if len(x_api_key) >= 8 else x_api_key
        bucket_id = f"http:{key_prefix}"
        if not gateway._rate_limiter.is_allowed(bucket_id):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        return x_api_key

    # --- Endpoints ---

    @app.get("/v1/status", response_model=StatusResponse)
    def get_status(_key: str = Depends(check_rate_limit)):
        hot_pub = (
            str(gateway._hot_keypair.pubkey())
            if gateway._hot_keypair
            else None
        )
        uptime = int(time.time() - gateway._start_time)
        return StatusResponse(
            status="ok",
            hot_wallet=hot_pub,
            uptime_seconds=uptime,
            mesh_connected=gateway._mesh.connected,
            rate_limit={
                "max_per_minute": gateway._config.max_requests_per_minute,
                "burst": gateway._config.rate_limit_burst,
            },
        )

    @app.get("/v1/balance/{address}", response_model=BalanceResponse)
    def get_balance(
        address: str,
        token: Optional[str] = None,
        _key: str = Depends(check_rate_limit),
    ):
        from solders.pubkey import Pubkey
        from solmesh.constants import KNOWN_TOKENS, LAMPORTS_PER_SOL
        from solmesh.spl import find_associated_token_address

        try:
            pubkey = Pubkey.from_string(address)
        except Exception:
            raise HTTPException(
                status_code=400, detail=f"Invalid Solana address: {address}"
            )

        if token:
            mint_str = _resolve_token_mint(token)
            token_info = KNOWN_TOKENS.get(mint_str)
            if token_info is None:
                raise HTTPException(
                    status_code=400, detail=f"Unknown token: {mint_str}"
                )
            symbol, decimals = token_info
            mint_pubkey = Pubkey.from_string(mint_str)
            ata = find_associated_token_address(pubkey, mint_pubkey)
            try:
                resp = gateway._rpc.get_token_account_balance(ata)
                raw_amount = int(resp.value.amount)
            except Exception as e:
                raise HTTPException(
                    status_code=502, detail=f"RPC error: {e}"
                )
            return BalanceResponse(
                address=address,
                token=mint_str,
                balance_raw=raw_amount,
                balance_human=raw_amount / (10 ** decimals),
                decimals=decimals,
                symbol=symbol,
            )

        # SOL balance
        try:
            resp = gateway._rpc.get_balance(pubkey)
            lamports = resp.value
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"RPC error: {e}")

        return BalanceResponse(
            address=address,
            token=None,
            balance_raw=lamports,
            balance_human=lamports / LAMPORTS_PER_SOL,
            decimals=9,
            symbol="SOL",
        )

    @app.get("/v1/blockhash", response_model=BlockhashResponse)
    def get_blockhash(_key: str = Depends(check_rate_limit)):
        try:
            resp = gateway._rpc.get_latest_blockhash()
            blockhash_str = str(resp.value.blockhash)
            height = resp.value.last_valid_block_height
        except Exception as e:
            raise HTTPException(
                status_code=502, detail=f"RPC error: {e}"
            )
        return BlockhashResponse(
            blockhash=blockhash_str,
            last_valid_block_height=height,
        )

    @app.get("/v1/slot", response_model=SlotResponse)
    def get_slot(_key: str = Depends(check_rate_limit)):
        try:
            resp = gateway._rpc.get_slot()
            return SlotResponse(slot=resp.value)
        except Exception as e:
            raise HTTPException(
                status_code=502, detail=f"RPC error: {e}"
            )

    @app.post("/v1/transfer", response_model=TransferResponse)
    def submit_transfer(
        req: TransferRequest,
        _key: str = Depends(check_rate_limit),
    ):
        from solders.pubkey import Pubkey
        from solmesh.constants import KNOWN_TOKENS, LAMPORTS_PER_SOL
        from solmesh.spl import create_spl_transfer, find_associated_token_address
        from solmesh.wallet import create_sol_transfer

        # 1. Verify hot wallet exists
        if not gateway._hot_keypair:
            raise HTTPException(
                status_code=503,
                detail="No hot wallet configured on this gateway",
            )

        # 2. Validate destination address
        try:
            dest_pubkey = Pubkey.from_string(req.destination)
        except Exception:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid destination address: {req.destination}",
            )

        # 3. Enforce transfer limits
        if req.token:
            mint_str = _resolve_token_mint(req.token)
            token_info = KNOWN_TOKENS.get(mint_str)
            if token_info is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unsupported token: {mint_str}",
                )
            symbol, decimals = token_info
            max_amount = gateway._config.get_max_transfer_token(mint_str)
            if max_amount is not None and req.amount > max_amount:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Amount {req.amount} exceeds max "
                        f"{max_amount} {symbol}"
                    ),
                )
            base_units = int(req.amount * (10 ** decimals))
        else:
            if req.amount > gateway._config.max_transfer_sol:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Amount {req.amount} SOL exceeds max "
                        f"{gateway._config.max_transfer_sol} SOL"
                    ),
                )
            lamports = int(req.amount * LAMPORTS_PER_SOL)

        # 4. Get recent blockhash
        try:
            bh_resp = gateway._rpc.get_latest_blockhash()
            recent_blockhash = bh_resp.value.blockhash
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"RPC error fetching blockhash: {e}",
            )

        # 5. Build, sign, send transaction
        try:
            if req.token:
                mint_pubkey = Pubkey.from_string(mint_str)
                recipient_ata = find_associated_token_address(
                    dest_pubkey, mint_pubkey
                )
                create_ata = False
                try:
                    ata_info = gateway._rpc.get_account_info(recipient_ata)
                    if ata_info.value is None:
                        create_ata = True
                except Exception:
                    create_ata = True
                tx_bytes = create_spl_transfer(
                    gateway._hot_keypair,
                    dest_pubkey,
                    mint_pubkey,
                    base_units,
                    decimals,
                    recent_blockhash,
                    create_recipient_ata=create_ata,
                )
            else:
                tx_bytes = create_sol_transfer(
                    gateway._hot_keypair,
                    dest_pubkey,
                    lamports,
                    recent_blockhash,
                )
        except Exception as e:
            return TransferResponse(
                success=False, error=f"TX creation failed: {e}"
            )

        # 6. Broadcast
        success, result = gateway._broadcast_to_solana(tx_bytes)
        if success:
            return TransferResponse(success=True, signature=result)
        return TransferResponse(success=False, error=result)

    return app
