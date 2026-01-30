"""Tests for the chunking and reassembly logic."""

import time
from unittest.mock import patch

import pytest

from solmesh.chunker import (
    generate_msg_id,
    chunk_payload,
    ReassemblyBuffer,
    ChunkReassembler,
)
from solmesh.protocol import unpack_message
from solmesh.constants import MsgType, MAX_CHUNK_DATA


class TestGenerateMsgId:
    def test_range(self):
        for _ in range(100):
            msg_id = generate_msg_id()
            assert 0 <= msg_id <= 0xFFFF

    def test_randomness(self):
        ids = {generate_msg_id() for _ in range(50)}
        assert len(ids) > 1  # extremely unlikely all same


class TestChunkPayload:
    def test_empty_data(self):
        chunks = chunk_payload(b"", MsgType.TX_CHUNK, msg_id=1)
        assert len(chunks) == 1
        header, payload = unpack_message(chunks[0])
        assert header.total_chunks == 1
        assert header.chunk_num == 0
        assert payload == b""

    def test_single_chunk(self):
        data = b"small payload"
        chunks = chunk_payload(data, MsgType.TX_CHUNK, msg_id=100)
        assert len(chunks) == 1
        header, payload = unpack_message(chunks[0])
        assert header.total_chunks == 1
        assert payload == data

    def test_exact_one_chunk(self):
        data = b"\xaa" * MAX_CHUNK_DATA
        chunks = chunk_payload(data, MsgType.TX_CHUNK, msg_id=200)
        assert len(chunks) == 1
        _, payload = unpack_message(chunks[0])
        assert payload == data

    def test_two_chunks(self):
        data = b"\xbb" * (MAX_CHUNK_DATA + 1)
        chunks = chunk_payload(data, MsgType.TX_CHUNK, msg_id=300)
        assert len(chunks) == 2

        h0, p0 = unpack_message(chunks[0])
        h1, p1 = unpack_message(chunks[1])

        assert h0.chunk_num == 0
        assert h0.total_chunks == 2
        assert h1.chunk_num == 1
        assert h1.total_chunks == 2
        assert h0.msg_id == h1.msg_id == 300
        assert p0 + p1 == data

    def test_solana_tx_size(self):
        """A typical Solana SOL transfer is ~215 bytes -> 2 chunks."""
        data = b"\xcc" * 215
        chunks = chunk_payload(data, MsgType.TX_CHUNK, msg_id=400)
        assert len(chunks) == 2

        reassembled = b""
        for chunk in chunks:
            _, payload = unpack_message(chunk)
            reassembled += payload
        assert reassembled == data

    def test_large_tx(self):
        """Max Solana TX is 1232 bytes -> 6 chunks."""
        data = b"\xdd" * 1232
        chunks = chunk_payload(data, MsgType.TX_CHUNK, msg_id=500)
        assert len(chunks) == 6

    def test_auto_msg_id(self):
        chunks = chunk_payload(b"test", MsgType.TX_CHUNK)
        header, _ = unpack_message(chunks[0])
        assert 0 <= header.msg_id <= 0xFFFF

    def test_too_large(self):
        data = b"\x00" * (256 * MAX_CHUNK_DATA)
        with pytest.raises(ValueError, match="too large"):
            chunk_payload(data, MsgType.TX_CHUNK)

    def test_preserves_msg_type(self):
        chunks = chunk_payload(b"data", MsgType.TX_REQUEST, msg_id=1)
        header, _ = unpack_message(chunks[0])
        assert header.msg_type == MsgType.TX_REQUEST


class TestReassemblyBuffer:
    def test_single_chunk(self):
        buf = ReassemblyBuffer(msg_id=1, total_chunks=1)
        assert buf.add_chunk(0, b"hello") is True
        assert buf.is_complete
        assert buf.reassemble() == b"hello"

    def test_multi_chunk_in_order(self):
        buf = ReassemblyBuffer(msg_id=2, total_chunks=3)
        assert buf.add_chunk(0, b"aaa") is False
        assert buf.add_chunk(1, b"bbb") is False
        assert buf.add_chunk(2, b"ccc") is True
        assert buf.reassemble() == b"aaabbbccc"

    def test_multi_chunk_out_of_order(self):
        buf = ReassemblyBuffer(msg_id=3, total_chunks=3)
        buf.add_chunk(2, b"ccc")
        buf.add_chunk(0, b"aaa")
        assert buf.add_chunk(1, b"bbb") is True
        assert buf.reassemble() == b"aaabbbccc"

    def test_missing_chunks(self):
        buf = ReassemblyBuffer(msg_id=4, total_chunks=4)
        buf.add_chunk(0, b"a")
        buf.add_chunk(3, b"d")
        assert buf.missing_chunks() == [1, 2]

    def test_duplicate_chunk(self):
        buf = ReassemblyBuffer(msg_id=5, total_chunks=2)
        buf.add_chunk(0, b"first")
        buf.add_chunk(0, b"duplicate")  # overwrites
        buf.add_chunk(1, b"second")
        assert buf.reassemble() == b"duplicatesecond"

    def test_reassemble_incomplete(self):
        buf = ReassemblyBuffer(msg_id=6, total_chunks=2)
        buf.add_chunk(0, b"only one")
        with pytest.raises(ValueError, match="missing chunks"):
            buf.reassemble()

    def test_expiration(self):
        buf = ReassemblyBuffer(msg_id=7, total_chunks=1)
        buf.created_at = time.time() - 200  # expired
        assert buf.is_expired


class TestChunkReassembler:
    def test_single_message(self):
        r = ChunkReassembler()
        result = r.receive_chunk("!sender1", 1, 0, 1, b"complete")
        assert result == b"complete"

    def test_multi_chunk_message(self):
        r = ChunkReassembler()
        assert r.receive_chunk("!sender1", 10, 0, 3, b"aaa") is None
        assert r.receive_chunk("!sender1", 10, 1, 3, b"bbb") is None
        result = r.receive_chunk("!sender1", 10, 2, 3, b"ccc")
        assert result == b"aaabbbccc"

    def test_interleaved_messages(self):
        r = ChunkReassembler()
        r.receive_chunk("!sender1", 1, 0, 2, b"1a")
        r.receive_chunk("!sender1", 2, 0, 2, b"2a")
        r.receive_chunk("!sender1", 1, 1, 2, b"1b")
        result2 = r.receive_chunk("!sender1", 2, 1, 2, b"2b")

        assert result2 == b"2a2b"

    def test_same_msg_id_different_senders(self):
        """Two senders with the same msg_id should not interfere."""
        r = ChunkReassembler()
        # Both senders use msg_id=42 with 2 chunks each
        r.receive_chunk("!alice", 42, 0, 2, b"AA")
        r.receive_chunk("!bob", 42, 0, 2, b"BB")

        result_alice = r.receive_chunk("!alice", 42, 1, 2, b"aa")
        assert result_alice == b"AAaa"

        result_bob = r.receive_chunk("!bob", 42, 1, 2, b"bb")
        assert result_bob == b"BBbb"

    def test_get_missing(self):
        r = ChunkReassembler()
        r.receive_chunk("!sender1", 5, 0, 4, b"a")
        r.receive_chunk("!sender1", 5, 2, 4, b"c")
        assert r.get_missing("!sender1", 5) == [1, 3]

    def test_get_missing_unknown(self):
        r = ChunkReassembler()
        assert r.get_missing("!sender1", 999) == []

    def test_cleanup_expired(self):
        r = ChunkReassembler()
        r.receive_chunk("!sender1", 1, 0, 2, b"a")
        # Force expiration
        key = ("!sender1", 1)
        r._buffers[key].created_at = time.time() - 200
        expired = r.cleanup_expired()
        assert key in expired
        assert key not in r._buffers

    def test_end_to_end_with_chunk_payload(self):
        """Full round-trip: chunk -> send -> reassemble."""
        data = b"\xff" * 500  # requires 3 chunks
        chunks = chunk_payload(data, MsgType.TX_CHUNK, msg_id=42)

        r = ChunkReassembler()
        result = None
        for chunk in chunks:
            header, payload = unpack_message(chunk)
            result = r.receive_chunk(
                "!sender1", header.msg_id, header.chunk_num,
                header.total_chunks, payload
            )

        assert result == data
