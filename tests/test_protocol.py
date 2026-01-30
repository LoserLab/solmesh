"""Tests for the SolMesh binary message protocol."""

import struct
import pytest

from solmesh.protocol import (
    BEACON_CAP_BALANCE,
    BEACON_CAP_BLOCKHASH,
    BEACON_CAP_HOT_WALLET,
    BEACON_CAP_RELAY,
    crc8,
    pack_message,
    unpack_message,
    encode_tx_request,
    decode_tx_request,
    encode_addr_share,
    decode_addr_share,
    encode_ack,
    decode_ack,
    encode_nack,
    decode_nack,
    encode_balance_req,
    decode_balance_req,
    encode_balance_resp,
    decode_balance_resp,
    encode_blockhash_req,
    decode_blockhash_req,
    encode_blockhash_resp,
    decode_blockhash_resp,
    encode_gateway_beacon,
    decode_gateway_beacon,
    encode_tx_result,
    decode_tx_result,
)
from solmesh.constants import MAGIC, PROTOCOL_VERSION, MsgType, MAX_CHUNK_DATA


class TestCRC8:
    def test_empty(self):
        assert crc8(b"") == 0x00

    def test_deterministic(self):
        data = b"hello solmesh"
        assert crc8(data) == crc8(data)

    def test_different_data_different_crc(self):
        assert crc8(b"abc") != crc8(b"abd")

    def test_single_byte(self):
        result = crc8(b"\x00")
        assert 0 <= result <= 255


class TestPackUnpack:
    def test_round_trip_empty_payload(self):
        raw = pack_message(MsgType.ACK, 0x1234, 0, 1, b"")
        header, payload = unpack_message(raw)
        assert header.msg_type == MsgType.ACK
        assert header.msg_id == 0x1234
        assert header.chunk_num == 0
        assert header.total_chunks == 1
        assert header.payload_len == 0
        assert payload == b""

    def test_round_trip_with_payload(self):
        data = b"test payload data"
        raw = pack_message(MsgType.TX_CHUNK, 0xABCD, 2, 5, data)
        header, payload = unpack_message(raw)
        assert header.msg_type == MsgType.TX_CHUNK
        assert header.msg_id == 0xABCD
        assert header.chunk_num == 2
        assert header.total_chunks == 5
        assert payload == data

    def test_round_trip_max_payload(self):
        data = bytes(range(256)) * (MAX_CHUNK_DATA // 256) + bytes(
            range(MAX_CHUNK_DATA % 256)
        )
        data = data[:MAX_CHUNK_DATA]
        raw = pack_message(MsgType.TX_CHUNK, 1, 0, 1, data)
        header, payload = unpack_message(raw)
        assert payload == data

    def test_payload_too_large(self):
        with pytest.raises(ValueError, match="Payload too large"):
            pack_message(MsgType.TX_CHUNK, 1, 0, 1, b"\x00" * (MAX_CHUNK_DATA + 1))

    def test_invalid_magic(self):
        raw = pack_message(MsgType.ACK, 1, 0, 1, b"")
        bad = b"\x00\x00" + raw[2:]
        with pytest.raises(ValueError, match="Invalid magic"):
            unpack_message(bad)

    def test_invalid_version(self):
        raw = pack_message(MsgType.ACK, 1, 0, 1, b"")
        bad = raw[:2] + b"\xff" + raw[3:]
        with pytest.raises(ValueError, match="Unsupported protocol version"):
            unpack_message(bad)

    def test_checksum_corruption(self):
        raw = pack_message(MsgType.ACK, 1, 0, 1, b"hello")
        corrupted = raw[:9] + bytes([(raw[9] ^ 0xFF)]) + raw[10:]
        with pytest.raises(ValueError, match="Checksum mismatch"):
            unpack_message(corrupted)

    def test_truncated_message(self):
        with pytest.raises(ValueError, match="Message too short"):
            unpack_message(b"\x53\x4d\x01")

    def test_payload_length_mismatch(self):
        raw = pack_message(MsgType.ACK, 1, 0, 1, b"hello")
        # Truncate the payload
        truncated = raw[:12]
        with pytest.raises(ValueError, match="Payload length mismatch"):
            unpack_message(truncated)

    def test_all_message_types(self):
        for msg_type in [
            MsgType.TX_CHUNK, MsgType.TX_REQUEST, MsgType.ADDR_SHARE,
            MsgType.ACK, MsgType.NACK, MsgType.BALANCE_REQ,
            MsgType.BALANCE_RESP, MsgType.TX_RESULT,
        ]:
            raw = pack_message(msg_type, 42, 0, 1, b"x")
            header, _ = unpack_message(raw)
            assert header.msg_type == msg_type


class TestTxRequest:
    def test_round_trip(self):
        sig = bytes(range(64))
        sender = bytes(range(32))
        dest = bytes(range(32, 64))
        lamports = 1_000_000_000

        payload = encode_tx_request(sender, dest, lamports, sig)
        result = decode_tx_request(payload)

        assert result["signature"] == sig
        assert result["sender_pubkey"] == sender
        assert result["dest_pubkey"] == dest
        assert result["lamports"] == lamports
        assert result["memo"] == ""

    def test_with_memo(self):
        sig = b"\x00" * 64
        sender = b"\x01" * 32
        dest = b"\x02" * 32
        lamports = 500_000

        payload = encode_tx_request(sender, dest, lamports, sig, memo="coffee")
        result = decode_tx_request(payload)
        assert result["memo"] == "coffee"

    def test_invalid_signature_length(self):
        with pytest.raises(ValueError, match="Signature must be 64"):
            encode_tx_request(b"\x01" * 32, b"\x02" * 32, 100, b"\x00" * 63)

    def test_invalid_pubkey_length(self):
        with pytest.raises(ValueError, match="Sender pubkey must be 32"):
            encode_tx_request(b"\x01" * 31, b"\x02" * 32, 100, b"\x00" * 64)

    def test_too_short(self):
        with pytest.raises(ValueError, match="too short"):
            decode_tx_request(b"\x00" * 100)


class TestAddrShare:
    def test_round_trip(self):
        pubkey = b"\xaa" * 32
        payload = encode_addr_share(pubkey)
        result = decode_addr_share(payload)
        assert result["pubkey"] == pubkey
        assert result["label"] == ""

    def test_with_label(self):
        pubkey = b"\xbb" * 32
        payload = encode_addr_share(pubkey, label="Alice's node")
        result = decode_addr_share(payload)
        assert result["pubkey"] == pubkey
        assert result["label"] == "Alice's node"

    def test_invalid_pubkey(self):
        with pytest.raises(ValueError):
            encode_addr_share(b"\x00" * 31)


class TestAck:
    def test_round_trip(self):
        payload = encode_ack(0x1234, 3, 0)
        result = decode_ack(payload)
        assert result["acked_msg_id"] == 0x1234
        assert result["acked_chunk"] == 3
        assert result["status"] == 0

    def test_ack_all_chunks(self):
        payload = encode_ack(0xFFFF, 0xFF, 0)
        result = decode_ack(payload)
        assert result["acked_chunk"] == 0xFF

    def test_too_short(self):
        with pytest.raises(ValueError, match="too short"):
            decode_ack(b"\x00\x01")


class TestNack:
    def test_round_trip(self):
        payload = encode_nack(0x5678, 0x04, "RPC timeout")
        result = decode_nack(payload)
        assert result["nacked_msg_id"] == 0x5678
        assert result["error_code"] == 0x04
        assert result["error_msg"] == "RPC timeout"

    def test_no_message(self):
        payload = encode_nack(1, 0x01)
        result = decode_nack(payload)
        assert result["error_msg"] == ""


class TestBalanceReq:
    def test_round_trip(self):
        pubkey = b"\xcc" * 32
        payload = encode_balance_req(pubkey)
        result = decode_balance_req(payload)
        assert result["pubkey"] == pubkey

    def test_invalid_pubkey(self):
        with pytest.raises(ValueError):
            encode_balance_req(b"\x00" * 10)


class TestBalanceResp:
    def test_round_trip(self):
        pubkey = b"\xdd" * 32
        lamports = 5_000_000_000
        payload = encode_balance_resp(pubkey, lamports)
        result = decode_balance_resp(payload)
        assert result["pubkey"] == pubkey
        assert result["lamports"] == lamports

    def test_zero_balance(self):
        pubkey = b"\x00" * 32
        payload = encode_balance_resp(pubkey, 0)
        result = decode_balance_resp(payload)
        assert result["lamports"] == 0


class TestBlockhashReq:
    def test_round_trip(self):
        payload = encode_blockhash_req()
        result = decode_blockhash_req(payload)
        assert result == {}

    def test_empty_payload(self):
        assert encode_blockhash_req() == b""


class TestBlockhashResp:
    def test_round_trip(self):
        blockhash = b"\xab" * 32
        payload = encode_blockhash_resp(blockhash)
        result = decode_blockhash_resp(payload)
        assert result["blockhash"] == blockhash

    def test_invalid_length(self):
        with pytest.raises(ValueError, match="32 bytes"):
            encode_blockhash_resp(b"\x00" * 16)

    def test_too_short(self):
        with pytest.raises(ValueError, match="too short"):
            decode_blockhash_resp(b"\x00" * 31)


class TestGatewayBeacon:
    def test_round_trip_no_pubkey(self):
        caps = BEACON_CAP_RELAY | BEACON_CAP_BALANCE
        payload = encode_gateway_beacon(1, caps, uptime_seconds=120)
        result = decode_gateway_beacon(payload)
        assert result["version"] == 1
        assert result["capabilities"] == caps
        assert result["uptime_seconds"] == 120
        assert result["hot_wallet_pubkey"] == b""

    def test_round_trip_with_pubkey(self):
        caps = BEACON_CAP_RELAY | BEACON_CAP_HOT_WALLET | BEACON_CAP_BALANCE | BEACON_CAP_BLOCKHASH
        pubkey = b"\xdd" * 32
        payload = encode_gateway_beacon(1, caps, hot_wallet_pubkey=pubkey, uptime_seconds=3600)
        result = decode_gateway_beacon(payload)
        assert result["version"] == 1
        assert result["capabilities"] == caps
        assert result["uptime_seconds"] == 3600
        assert result["hot_wallet_pubkey"] == pubkey

    def test_invalid_pubkey_length(self):
        with pytest.raises(ValueError, match="32 bytes"):
            encode_gateway_beacon(1, 0x01, hot_wallet_pubkey=b"\x00" * 16)

    def test_too_short(self):
        with pytest.raises(ValueError, match="too short"):
            decode_gateway_beacon(b"\x01\x01\x00")


class TestTxResult:
    def test_success(self):
        sig = b"\xee" * 64
        payload = encode_tx_result(0x1111, True, sig)
        result = decode_tx_result(payload)
        assert result["orig_msg_id"] == 0x1111
        assert result["success"] is True
        assert result["data"] == sig

    def test_failure(self):
        error = b"Blockhash not found"
        payload = encode_tx_result(0x2222, False, error)
        result = decode_tx_result(payload)
        assert result["orig_msg_id"] == 0x2222
        assert result["success"] is False
        assert result["data"] == error

    def test_too_short(self):
        with pytest.raises(ValueError, match="too short"):
            decode_tx_result(b"\x00\x01")
