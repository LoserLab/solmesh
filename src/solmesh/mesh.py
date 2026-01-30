"""Meshtastic interface wrapper for SolMesh.

Provides a clean interface for sending/receiving SolMesh protocol
messages over the Meshtastic mesh network using the PRIVATE_APP portnum.
"""

from __future__ import annotations
import logging
import signal
import sys
import time
from typing import Callable, Optional

import meshtastic
import meshtastic.serial_interface
import meshtastic.tcp_interface
from pubsub import pub

from solmesh.constants import MAGIC, INTER_CHUNK_DELAY
from solmesh.protocol import unpack_message, SolMeshHeader

logger = logging.getLogger(__name__)

# PRIVATE_APP portnum (256) for all SolMesh binary data
SOLMESH_PORTNUM = 256


class MeshInterface:
    """Wrapper around the Meshtastic serial/TCP interface.

    Handles connection, sending SolMesh protocol messages, and
    dispatching received messages to registered handlers.
    """

    def __init__(self, connection_type: str = "serial",
                 device_path: Optional[str] = None,
                 hostname: Optional[str] = None):
        self._interface = None
        self._connection_type = connection_type
        self._device_path = device_path
        self._hostname = hostname
        self._handlers: dict[int, list[Callable]] = {}
        self._running = False

    def connect(self) -> None:
        """Establish connection to Meshtastic device."""
        pub.subscribe(self._on_receive, "meshtastic.receive")

        try:
            if self._connection_type == "tcp":
                self._interface = meshtastic.tcp_interface.TCPInterface(
                    hostname=self._hostname
                )
            else:
                if self._device_path:
                    self._interface = meshtastic.serial_interface.SerialInterface(
                        devPath=self._device_path
                    )
                else:
                    self._interface = meshtastic.serial_interface.SerialInterface()
        except Exception as e:
            logger.error("Could not connect to Meshtastic device: %s", e)
            raise

        logger.info("Connected to Meshtastic device")

    def close(self) -> None:
        """Close the Meshtastic interface."""
        self._running = False
        if self._interface:
            self._interface.close()
            self._interface = None
        logger.info("Meshtastic interface closed")

    def register_handler(self, msg_type: int, callback: Callable) -> None:
        """Register a callback for a specific SolMesh message type.

        Callback signature: (header: SolMeshHeader, payload: bytes, sender_id: str)
        """
        if msg_type not in self._handlers:
            self._handlers[msg_type] = []
        self._handlers[msg_type].append(callback)

    def send(self, data: bytes, destination_id: Optional[str] = None,
             want_ack: bool = True) -> bool:
        """Send raw SolMesh protocol bytes over the mesh.

        Uses PRIVATE_APP portnum and sendData().
        """
        if not self._interface:
            raise RuntimeError("Not connected")

        try:
            if destination_id:
                self._interface.sendData(
                    data,
                    portNum=SOLMESH_PORTNUM,
                    destinationId=destination_id,
                    wantAck=want_ack,
                )
            else:
                self._interface.sendData(
                    data,
                    portNum=SOLMESH_PORTNUM,
                    wantAck=want_ack,
                )
            return True
        except Exception as e:
            logger.error("Failed to send data: %s", e)
            return False

    def send_chunks(self, chunks: list[bytes],
                    destination_id: Optional[str] = None,
                    inter_chunk_delay: float = INTER_CHUNK_DELAY) -> bool:
        """Send a list of chunked messages with delay between them."""
        for i, chunk in enumerate(chunks):
            success = self.send(chunk, destination_id=destination_id)
            if not success:
                logger.error("Failed to send chunk %d/%d", i, len(chunks))
                return False
            if i < len(chunks) - 1:
                time.sleep(inter_chunk_delay)
        return True

    def _on_receive(self, packet, interface) -> None:
        """PubSub callback for meshtastic.receive.

        Filters for PRIVATE_APP portnum, validates SolMesh header,
        dispatches to registered handlers.
        """
        try:
            portnum = packet.get("decoded", {}).get("portnum")
            if portnum != "PRIVATE_APP":
                return

            raw = packet["decoded"].get("payload", b"")
            if not raw or len(raw) < 2 or raw[0:2] != MAGIC:
                return

            header, payload = unpack_message(raw)

            sender_id = packet.get("fromId", packet.get("from", "unknown"))

            handlers = self._handlers.get(header.msg_type, [])
            for handler in handlers:
                try:
                    handler(header, payload, sender_id)
                except Exception as e:
                    logger.error(
                        "Handler error for msg_type 0x%02x: %s",
                        header.msg_type, e,
                    )
        except ValueError as e:
            logger.warning("Invalid SolMesh message: %s", e)
        except Exception as e:
            logger.error("Error processing received packet: %s", e)

    def run(self) -> None:
        """Block and run the event loop (for long-running node modes)."""
        self._running = True

        def shutdown(sig, frame):
            logger.info("Shutting down...")
            self.close()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        while self._running:
            time.sleep(1)

    @property
    def my_node_id(self) -> Optional[str]:
        """Return this node's Meshtastic ID string."""
        if self._interface and self._interface.myInfo:
            return self._interface.myInfo.my_node_num
        return None
