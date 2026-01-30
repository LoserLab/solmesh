"""Transaction chunking and reassembly for LoRa-sized messages."""

from __future__ import annotations
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from solmesh.constants import MAX_CHUNK_DATA, CHUNK_REASSEMBLY_TIMEOUT
from solmesh.protocol import pack_message


def generate_msg_id() -> int:
    """Generate a random 16-bit message ID."""
    return int.from_bytes(os.urandom(2), "big")


def chunk_payload(data: bytes, msg_type: int,
                  msg_id: Optional[int] = None) -> list[bytes]:
    """Split data into chunked SolMesh messages ready for transmission.

    Each returned bytes object is a complete protocol message (header + chunk data).
    """
    if msg_id is None:
        msg_id = generate_msg_id()

    if len(data) == 0:
        return [pack_message(msg_type, msg_id, 0, 1, b"")]

    chunks = []
    total = (len(data) + MAX_CHUNK_DATA - 1) // MAX_CHUNK_DATA
    if total > 255:
        raise ValueError(
            f"Data too large to chunk: {len(data)} bytes requires {total} chunks (max 255)"
        )

    for i in range(total):
        start = i * MAX_CHUNK_DATA
        end = min(start + MAX_CHUNK_DATA, len(data))
        chunk_data = data[start:end]
        chunks.append(pack_message(msg_type, msg_id, i, total, chunk_data))

    return chunks


@dataclass
class ReassemblyBuffer:
    """Tracks incoming chunks for a single msg_id."""

    msg_id: int
    total_chunks: int
    chunks: dict[int, bytes] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def is_complete(self) -> bool:
        return len(self.chunks) == self.total_chunks

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > CHUNK_REASSEMBLY_TIMEOUT

    def add_chunk(self, chunk_num: int, payload: bytes) -> bool:
        """Add a chunk. Returns True if this completed the reassembly."""
        self.chunks[chunk_num] = payload
        return self.is_complete

    def missing_chunks(self) -> list[int]:
        """Return chunk numbers not yet received."""
        return [i for i in range(self.total_chunks) if i not in self.chunks]

    def reassemble(self) -> bytes:
        """Reassemble all chunks in order. Raises if incomplete."""
        if not self.is_complete:
            missing = self.missing_chunks()
            raise ValueError(f"Cannot reassemble: missing chunks {missing}")
        return b"".join(self.chunks[i] for i in range(self.total_chunks))


class ChunkReassembler:
    """Manages multiple concurrent reassembly buffers.

    Buffers are keyed by (sender_id, msg_id) to prevent collisions
    when two different senders use the same random 16-bit msg_id.
    """

    def __init__(self):
        self._buffers: dict[tuple[str, int], ReassemblyBuffer] = {}

    def receive_chunk(self, sender_id: str, msg_id: int, chunk_num: int,
                      total_chunks: int, payload: bytes) -> Optional[bytes]:
        """Process an incoming chunk.

        Returns the fully reassembled payload if complete, otherwise None.
        """
        self.cleanup_expired()

        key = (sender_id, msg_id)
        if key not in self._buffers:
            self._buffers[key] = ReassemblyBuffer(
                msg_id=msg_id, total_chunks=total_chunks
            )

        buf = self._buffers[key]
        completed = buf.add_chunk(chunk_num, payload)

        if completed:
            data = buf.reassemble()
            del self._buffers[key]
            return data

        return None

    def get_missing(self, sender_id: str, msg_id: int) -> list[int]:
        """Get missing chunk numbers for a given sender and msg_id."""
        key = (sender_id, msg_id)
        if key not in self._buffers:
            return []
        return self._buffers[key].missing_chunks()

    def cleanup_expired(self) -> list[tuple[str, int]]:
        """Remove expired reassembly buffers. Returns list of expired keys."""
        expired = [
            key
            for key, buf in self._buffers.items()
            if buf.is_expired
        ]
        for key in expired:
            del self._buffers[key]
        return expired
