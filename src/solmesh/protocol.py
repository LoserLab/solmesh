"""Binary message protocol for SolMesh.

Defines the 10-byte header format, CRC-8 integrity check, and
payload encoders/decoders for all message types.
"""

from __future__ import annotations
import struct
from dataclasses import dataclass

from solmesh.constants import (
    MAGIC,
    PROTOCOL_VERSION,
    HEADER_SIZE,
    HEADER_FORMAT,
    MAX_CHUNK_DATA,
    MsgType,
)


@dataclass
class SolMeshHeader:
    """10-byte binary header for all SolMesh messages."""

    msg_type: int
    msg_id: int
    chunk_num: int
    total_chunks: int
    payload_len: int
    checksum: int = 0


def crc8(data: bytes) -> int:
    """CRC-8/MAXIM checksum (polynomial 0x31)."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = (crc << 1) ^ 0x31
            else:
                crc <<= 1
            crc &= 0xFF
    return crc


def pack_message(msg_type: int, msg_id: int, chunk_num: int,
                 total_chunks: int, payload: bytes) -> bytes:
    """Pack a complete SolMesh message (header + payload).

    Computes the CRC-8 checksum over the header (minus checksum byte) + payload.
    """
    if len(payload) > MAX_CHUNK_DATA:
        raise ValueError(f"Payload too large: {len(payload)} > {MAX_CHUNK_DATA}")

    # Pack header without checksum first
    header_no_crc = struct.pack(
        "!2sBBHBBB",
        MAGIC,
        PROTOCOL_VERSION,
        msg_type,
        msg_id,
        chunk_num,
        total_chunks,
        len(payload),
    )
    # CRC over header bytes + payload
    check = crc8(header_no_crc + payload)
    return header_no_crc + struct.pack("!B", check) + payload


def unpack_message(raw: bytes) -> tuple[SolMeshHeader, bytes]:
    """Unpack raw bytes into (header, payload).

    Validates magic, version, and checksum.
    Raises ValueError on any validation failure.
    """
    if len(raw) < HEADER_SIZE:
        raise ValueError(f"Message too short: {len(raw)} < {HEADER_SIZE}")

    magic = raw[0:2]
    if magic != MAGIC:
        raise ValueError(f"Invalid magic: {magic!r}")

    version = raw[2]
    if version != PROTOCOL_VERSION:
        raise ValueError(f"Unsupported protocol version: {version}")

    msg_type = raw[3]
    msg_id = struct.unpack("!H", raw[4:6])[0]
    chunk_num = raw[6]
    total_chunks = raw[7]
    payload_len = raw[8]
    checksum = raw[9]

    payload = raw[HEADER_SIZE : HEADER_SIZE + payload_len]
    if len(payload) != payload_len:
        raise ValueError(
            f"Payload length mismatch: expected {payload_len}, got {len(payload)}"
        )

    # Verify checksum
    header_no_crc = raw[0:9]
    expected_crc = crc8(header_no_crc + payload)
    if checksum != expected_crc:
        raise ValueError(
            f"Checksum mismatch: got 0x{checksum:02x}, expected 0x{expected_crc:02x}"
        )

    header = SolMeshHeader(
        msg_type=msg_type,
        msg_id=msg_id,
        chunk_num=chunk_num,
        total_chunks=total_chunks,
        payload_len=payload_len,
        checksum=checksum,
    )
    return header, payload


# --- Payload encoders/decoders ---


def encode_tx_request(sender_pubkey: bytes, dest_pubkey: bytes,
                      lamports: int, signature: bytes,
                      memo: str = "") -> bytes:
    """Encode TX_REQUEST payload.

    Layout: signature(64) + sender_pubkey(32) + dest_pubkey(32) + lamports(8) + memo
    """
    if len(signature) != 64:
        raise ValueError("Signature must be 64 bytes")
    if len(sender_pubkey) != 32:
        raise ValueError("Sender pubkey must be 32 bytes")
    if len(dest_pubkey) != 32:
        raise ValueError("Destination pubkey must be 32 bytes")

    payload = signature + sender_pubkey + dest_pubkey + struct.pack("!Q", lamports)
    if memo:
        memo_bytes = memo.encode("utf-8")
        payload += memo_bytes
    return payload


def decode_tx_request(payload: bytes) -> dict:
    """Decode TX_REQUEST payload."""
    if len(payload) < 136:
        raise ValueError(f"TX_REQUEST too short: {len(payload)} < 136")

    signature = payload[0:64]
    sender_pubkey = payload[64:96]
    dest_pubkey = payload[96:128]
    lamports = struct.unpack("!Q", payload[128:136])[0]
    memo = payload[136:].decode("utf-8") if len(payload) > 136 else ""

    return {
        "signature": signature,
        "sender_pubkey": sender_pubkey,
        "dest_pubkey": dest_pubkey,
        "lamports": lamports,
        "memo": memo,
    }


def encode_addr_share(pubkey: bytes, label: str = "") -> bytes:
    """Encode ADDR_SHARE payload: 32-byte pubkey + optional label."""
    if len(pubkey) != 32:
        raise ValueError("Pubkey must be 32 bytes")
    payload = pubkey
    if label:
        payload += label.encode("utf-8")
    return payload


def decode_addr_share(payload: bytes) -> dict:
    """Decode ADDR_SHARE payload."""
    if len(payload) < 32:
        raise ValueError(f"ADDR_SHARE too short: {len(payload)} < 32")
    pubkey = payload[0:32]
    label = payload[32:].decode("utf-8") if len(payload) > 32 else ""
    return {"pubkey": pubkey, "label": label}


def encode_ack(acked_msg_id: int, acked_chunk: int = 0xFF,
               status: int = 0) -> bytes:
    """Encode ACK payload: msg_id(2) + chunk(1) + status(1)."""
    return struct.pack("!HBB", acked_msg_id, acked_chunk, status)


def decode_ack(payload: bytes) -> dict:
    """Decode ACK payload."""
    if len(payload) < 4:
        raise ValueError(f"ACK too short: {len(payload)} < 4")
    acked_msg_id, acked_chunk, status = struct.unpack("!HBB", payload[0:4])
    return {
        "acked_msg_id": acked_msg_id,
        "acked_chunk": acked_chunk,
        "status": status,
    }


def encode_nack(nacked_msg_id: int, error_code: int,
                error_msg: str = "") -> bytes:
    """Encode NACK payload: msg_id(2) + error_code(1) + error_msg."""
    payload = struct.pack("!HB", nacked_msg_id, error_code)
    if error_msg:
        payload += error_msg.encode("utf-8")
    return payload


def decode_nack(payload: bytes) -> dict:
    """Decode NACK payload."""
    if len(payload) < 3:
        raise ValueError(f"NACK too short: {len(payload)} < 3")
    nacked_msg_id, error_code = struct.unpack("!HB", payload[0:3])
    error_msg = payload[3:].decode("utf-8") if len(payload) > 3 else ""
    return {
        "nacked_msg_id": nacked_msg_id,
        "error_code": error_code,
        "error_msg": error_msg,
    }


def encode_balance_req(pubkey: bytes) -> bytes:
    """Encode BALANCE_REQ payload: 32-byte pubkey."""
    if len(pubkey) != 32:
        raise ValueError("Pubkey must be 32 bytes")
    return pubkey


def decode_balance_req(payload: bytes) -> dict:
    """Decode BALANCE_REQ payload."""
    if len(payload) < 32:
        raise ValueError(f"BALANCE_REQ too short: {len(payload)} < 32")
    return {"pubkey": payload[0:32]}


def encode_balance_resp(pubkey: bytes, lamports: int) -> bytes:
    """Encode BALANCE_RESP: pubkey(32) + lamports(8)."""
    if len(pubkey) != 32:
        raise ValueError("Pubkey must be 32 bytes")
    return pubkey + struct.pack("!Q", lamports)


def decode_balance_resp(payload: bytes) -> dict:
    """Decode BALANCE_RESP payload."""
    if len(payload) < 40:
        raise ValueError(f"BALANCE_RESP too short: {len(payload)} < 40")
    pubkey = payload[0:32]
    lamports = struct.unpack("!Q", payload[32:40])[0]
    return {"pubkey": pubkey, "lamports": lamports}


def encode_blockhash_req() -> bytes:
    """Encode BLOCKHASH_REQ payload (empty)."""
    return b""


def decode_blockhash_req(payload: bytes) -> dict:
    """Decode BLOCKHASH_REQ payload."""
    return {}


def encode_blockhash_resp(blockhash: bytes) -> bytes:
    """Encode BLOCKHASH_RESP: 32-byte blockhash."""
    if len(blockhash) != 32:
        raise ValueError("Blockhash must be 32 bytes")
    return blockhash


def decode_blockhash_resp(payload: bytes) -> dict:
    """Decode BLOCKHASH_RESP payload."""
    if len(payload) < 32:
        raise ValueError(f"BLOCKHASH_RESP too short: {len(payload)} < 32")
    return {"blockhash": payload[0:32]}


# Gateway beacon capability flags
BEACON_CAP_RELAY = 0x01      # Can relay signed transactions (Mode 1)
BEACON_CAP_HOT_WALLET = 0x02 # Has hot wallet for transfers (Mode 3)
BEACON_CAP_BALANCE = 0x04    # Can query balances
BEACON_CAP_BLOCKHASH = 0x08  # Can provide recent blockhash


def encode_gateway_beacon(version: int, capabilities: int,
                          hot_wallet_pubkey: bytes = b"",
                          uptime_seconds: int = 0) -> bytes:
    """Encode GATEWAY_BEACON: version(1) + caps(1) + uptime(4) + pubkey(0 or 32)."""
    payload = struct.pack("!BBI", version, capabilities, uptime_seconds)
    if hot_wallet_pubkey:
        if len(hot_wallet_pubkey) != 32:
            raise ValueError("Hot wallet pubkey must be 32 bytes")
        payload += hot_wallet_pubkey
    return payload


def decode_gateway_beacon(payload: bytes) -> dict:
    """Decode GATEWAY_BEACON payload."""
    if len(payload) < 6:
        raise ValueError(f"GATEWAY_BEACON too short: {len(payload)} < 6")
    version, capabilities, uptime = struct.unpack("!BBI", payload[0:6])
    hot_wallet_pubkey = payload[6:38] if len(payload) >= 38 else b""
    return {
        "version": version,
        "capabilities": capabilities,
        "uptime_seconds": uptime,
        "hot_wallet_pubkey": hot_wallet_pubkey,
    }


def encode_tx_result(orig_msg_id: int, success: bool,
                     data: bytes) -> bytes:
    """Encode TX_RESULT: orig_msg_id(2) + success(1) + signature_or_error."""
    return struct.pack("!HB", orig_msg_id, 1 if success else 0) + data


def decode_tx_result(payload: bytes) -> dict:
    """Decode TX_RESULT payload."""
    if len(payload) < 3:
        raise ValueError(f"TX_RESULT too short: {len(payload)} < 3")
    orig_msg_id, success_byte = struct.unpack("!HB", payload[0:3])
    success = success_byte == 1
    data = payload[3:]
    return {
        "orig_msg_id": orig_msg_id,
        "success": success,
        "data": data,
    }
