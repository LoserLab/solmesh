"""Mock MeshInterface for integration testing."""

from __future__ import annotations
import time
from typing import Callable, Optional

from solmesh.constants import MAGIC
from solmesh.protocol import SolMeshHeader, unpack_message


class MockMeshInterface:
    """In-memory mock that captures sent messages and can inject received ones."""

    def __init__(self):
        self._handlers: dict[int, list[Callable]] = {}
        self.sent_messages: list[dict] = []
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def register_handler(self, msg_type: int, callback: Callable) -> None:
        if msg_type not in self._handlers:
            self._handlers[msg_type] = []
        self._handlers[msg_type].append(callback)

    def send(self, data: bytes, destination_id: Optional[str] = None,
             want_ack: bool = True) -> bool:
        self.sent_messages.append({
            "data": data,
            "destination_id": destination_id,
            "want_ack": want_ack,
            "timestamp": time.time(),
        })
        return True

    def send_chunks(self, chunks: list[bytes],
                    destination_id: Optional[str] = None,
                    inter_chunk_delay: float = 0) -> bool:
        for chunk in chunks:
            self.send(chunk, destination_id=destination_id)
        return True

    def run(self) -> None:
        pass

    def inject_message(self, raw: bytes, sender_id: str) -> None:
        """Simulate receiving a message from the mesh."""
        if not raw or len(raw) < 2 or raw[0:2] != MAGIC:
            return

        header, payload = unpack_message(raw)
        handlers = self._handlers.get(header.msg_type, [])
        for handler in handlers:
            handler(header, payload, sender_id)

    def get_sent_of_type(self, msg_type: int) -> list[tuple[SolMeshHeader, bytes]]:
        """Return all sent messages of a specific type."""
        results = []
        for msg in self.sent_messages:
            try:
                header, payload = unpack_message(msg["data"])
                if header.msg_type == msg_type:
                    results.append((header, payload))
            except (ValueError, Exception):
                pass
        return results
