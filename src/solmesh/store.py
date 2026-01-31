"""Store-and-forward intent persistence for SolMesh.

Queues transaction intents locally on disk as JSON. Intents store
unsigned parameters only (wallet name, recipient, amount) -- signing
happens at flush time. No secrets are written to disk.
"""

from __future__ import annotations

import enum
import fcntl
import json
import logging
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_DIR = Path.home() / ".solmesh"
DEFAULT_QUEUE_FILE = DEFAULT_QUEUE_DIR / "queue.json"


class IntentStatus(str, enum.Enum):
    """Status of a queued transaction intent."""

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"


@dataclass
class Intent:
    """A queued transaction intent."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    mode: int = 3  # 1 = relay signed TX, 3 = request gateway transfer
    status: str = IntentStatus.PENDING.value
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    wallet_name: str = ""
    recipient: str = ""
    amount: float = 0.0
    token_mint: Optional[str] = None  # None = SOL transfer
    token_decimals: int = 0
    attempts: int = 0
    max_attempts: int = 3
    last_error: Optional[str] = None
    result_tx_hash: Optional[str] = None


class IntentStore:
    """Persistent queue of transaction intents stored as JSON on disk.

    Features:
    - Atomic writes via tempfile + os.replace()
    - Crash recovery: SENDING intents reset to PENDING on init
    - Deduplication by (wallet_name, recipient, amount, token_mint)
    - fcntl.flock-based file locking for inter-process safety
    - Corrupted JSON handled gracefully (returns empty list)
    """

    def __init__(self, queue_file: Path = DEFAULT_QUEUE_FILE):
        self._queue_file = queue_file
        self._dir = queue_file.parent
        self._dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._dir, 0o700)
        self._recover_sending()

    # --- File I/O (private) ---

    def _load(self) -> list[dict]:
        """Load intents from disk. Returns empty list on missing/corrupt file."""
        if not self._queue_file.exists():
            return []
        try:
            with open(self._queue_file) as f:
                data = json.load(f)
            if not isinstance(data, list):
                return []
            return data
        except (json.JSONDecodeError, IOError):
            logger.warning("Corrupt or unreadable queue file; treating as empty")
            return []

    def _save(self, intents: list[dict]) -> None:
        """Atomic write: write to temp file then os.replace()."""
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(intents, f, indent=2)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._queue_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _recover_sending(self) -> None:
        """One-time crash recovery: reset SENDING intents to PENDING."""
        intents = self._load()
        changed = False
        for intent in intents:
            if intent.get("status") == IntentStatus.SENDING.value:
                intent["status"] = IntentStatus.PENDING.value
                intent["updated_at"] = time.time()
                changed = True
        if changed:
            self._save(intents)
            count = sum(
                1 for i in intents
                if i.get("status") == IntentStatus.PENDING.value
            )
            logger.info("Recovered %d SENDING intents back to PENDING", count)

    # --- Deduplication ---

    def _is_duplicate(self, intents: list[dict], wallet_name: str,
                      recipient: str, amount: float,
                      token_mint: Optional[str]) -> bool:
        """Check if a PENDING intent with same parameters already exists."""
        for intent in intents:
            if (intent.get("status") == IntentStatus.PENDING.value
                    and intent.get("wallet_name") == wallet_name
                    and intent.get("recipient") == recipient
                    and intent.get("amount") == amount
                    and intent.get("token_mint") == token_mint):
                return True
        return False

    # --- File locking ---

    @contextmanager
    def flush_lock(self):
        """Acquire an exclusive file lock to prevent concurrent flushes."""
        lock_path = self._dir / "queue.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    # --- Public API ---

    def add(self, intent: Intent) -> Intent:
        """Add an intent to the queue. Raises ValueError on duplicate."""
        intents = self._load()
        if self._is_duplicate(intents, intent.wallet_name, intent.recipient,
                              intent.amount, intent.token_mint):
            raise ValueError(
                f"Duplicate PENDING intent: {intent.wallet_name} -> "
                f"{intent.recipient} {intent.amount}"
            )
        intents.append(asdict(intent))
        self._save(intents)
        return intent

    def list_intents(self, status: Optional[str] = None) -> list[dict]:
        """List intents, optionally filtered by status."""
        intents = self._load()
        if status is not None:
            intents = [i for i in intents if i.get("status") == status]
        return intents

    def get(self, intent_id: str) -> Optional[dict]:
        """Get a single intent by ID."""
        for intent in self._load():
            if intent.get("id") == intent_id:
                return intent
        return None

    def update_status(self, intent_id: str, status: str,
                      error: Optional[str] = None,
                      tx_hash: Optional[str] = None) -> bool:
        """Update an intent's status and optionally error/tx_hash.

        Returns True if found and updated.
        """
        intents = self._load()
        for intent in intents:
            if intent.get("id") == intent_id:
                intent["status"] = status
                intent["updated_at"] = time.time()
                if error is not None:
                    intent["last_error"] = error
                if tx_hash is not None:
                    intent["result_tx_hash"] = tx_hash
                self._save(intents)
                return True
        return False

    def increment_attempts(self, intent_id: str) -> bool:
        """Increment attempt count. Sets FAILED if max_attempts reached.

        Returns True if found.
        """
        intents = self._load()
        for intent in intents:
            if intent.get("id") == intent_id:
                intent["attempts"] = intent.get("attempts", 0) + 1
                intent["updated_at"] = time.time()
                if intent["attempts"] >= intent.get("max_attempts", 3):
                    intent["status"] = IntentStatus.FAILED.value
                self._save(intents)
                return True
        return False

    def remove(self, intent_id: str) -> bool:
        """Remove a single intent by ID. Returns True if found."""
        intents = self._load()
        before = len(intents)
        intents = [i for i in intents if i.get("id") != intent_id]
        if len(intents) < before:
            self._save(intents)
            return True
        return False

    def clear(self, status: Optional[str] = None) -> int:
        """Remove intents by status (or all if None). Returns count removed."""
        intents = self._load()
        if status is None:
            count = len(intents)
            self._save([])
            return count
        remaining = [i for i in intents if i.get("status") != status]
        count = len(intents) - len(remaining)
        self._save(remaining)
        return count

    def pending_intents(self) -> list[dict]:
        """Return PENDING intents sorted by created_at (oldest first)."""
        intents = self.list_intents(IntentStatus.PENDING.value)
        intents.sort(key=lambda i: i.get("created_at", 0))
        return intents
