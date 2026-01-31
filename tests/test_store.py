"""Unit tests for IntentStore."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time

import pytest

from solmesh.constants import MAX_FLUSH_ATTEMPTS
from solmesh.store import Intent, IntentStatus, IntentStore


@pytest.fixture
def queue_dir(tmp_path):
    return tmp_path / ".solmesh"


@pytest.fixture
def queue_file(queue_dir):
    return queue_dir / "queue.json"


@pytest.fixture
def store(queue_file):
    return IntentStore(queue_file=queue_file)


def _make_intent(**kwargs) -> Intent:
    defaults = dict(
        mode=3,
        wallet_name="alice",
        recipient="Dest1111111111111111111111111111111111111111111",
        amount=1.0,
    )
    defaults.update(kwargs)
    return Intent(**defaults)


class TestIntentAdd:
    def test_add_and_list(self, store):
        """Adding an intent should make it appear in list."""
        intent = _make_intent()
        store.add(intent)
        intents = store.list_intents()
        assert len(intents) == 1
        assert intents[0]["wallet_name"] == "alice"
        assert intents[0]["status"] == IntentStatus.PENDING.value

    def test_deduplication(self, store):
        """Adding a duplicate PENDING intent should raise ValueError."""
        store.add(_make_intent())
        with pytest.raises(ValueError, match="Duplicate"):
            store.add(_make_intent())

    def test_no_dedup_different_status(self, store):
        """A SENT intent should not block adding a new PENDING one with same params."""
        intent = _make_intent()
        store.add(intent)
        store.update_status(intent.id, IntentStatus.SENT.value)
        # Should not raise -- the existing one is SENT, not PENDING
        store.add(_make_intent())
        assert len(store.list_intents()) == 2


class TestIntentList:
    def test_list_filter_status(self, store):
        """Listing by status should filter correctly."""
        i1 = _make_intent(wallet_name="a", recipient="r1")
        i2 = _make_intent(wallet_name="b", recipient="r2")
        store.add(i1)
        store.add(i2)
        store.update_status(i1.id, IntentStatus.SENT.value)

        pending = store.list_intents(status=IntentStatus.PENDING.value)
        assert len(pending) == 1
        assert pending[0]["wallet_name"] == "b"

        sent = store.list_intents(status=IntentStatus.SENT.value)
        assert len(sent) == 1
        assert sent[0]["wallet_name"] == "a"

    def test_pending_sorted_by_created_at(self, store):
        """pending_intents should return oldest first."""
        i1 = _make_intent(wallet_name="a", recipient="r1")
        i1.created_at = 100.0
        i2 = _make_intent(wallet_name="b", recipient="r2")
        i2.created_at = 50.0
        store.add(i1)
        store.add(i2)
        pending = store.pending_intents()
        assert pending[0]["wallet_name"] == "b"  # older
        assert pending[1]["wallet_name"] == "a"


class TestIntentUpdate:
    def test_update_status(self, store):
        """update_status should change the status."""
        intent = _make_intent()
        store.add(intent)
        store.update_status(intent.id, IntentStatus.SENT.value)
        i = store.get(intent.id)
        assert i["status"] == IntentStatus.SENT.value

    def test_update_with_error(self, store):
        """update_status with error should record the error."""
        intent = _make_intent()
        store.add(intent)
        store.update_status(intent.id, IntentStatus.PENDING.value,
                            error="RPC timeout")
        i = store.get(intent.id)
        assert i["last_error"] == "RPC timeout"

    def test_update_with_tx_hash(self, store):
        """update_status with tx_hash should record the hash."""
        intent = _make_intent()
        store.add(intent)
        store.update_status(intent.id, IntentStatus.SENT.value,
                            tx_hash="abc123sig")
        i = store.get(intent.id)
        assert i["result_tx_hash"] == "abc123sig"

    def test_update_nonexistent(self, store):
        """Updating a nonexistent intent should return False."""
        assert store.update_status("nonexistent", IntentStatus.SENT.value) is False


class TestIntentAttempts:
    def test_increment_attempts(self, store):
        """Incrementing attempts should increase the count."""
        intent = _make_intent()
        store.add(intent)
        store.increment_attempts(intent.id)
        i = store.get(intent.id)
        assert i["attempts"] == 1
        assert i["status"] == IntentStatus.PENDING.value  # still PENDING

    def test_increment_marks_failed(self, store):
        """Reaching max_attempts should set status to FAILED."""
        intent = _make_intent()
        intent.max_attempts = MAX_FLUSH_ATTEMPTS
        store.add(intent)
        for _ in range(MAX_FLUSH_ATTEMPTS):
            store.increment_attempts(intent.id)
        i = store.get(intent.id)
        assert i["attempts"] == MAX_FLUSH_ATTEMPTS
        assert i["status"] == IntentStatus.FAILED.value


class TestIntentRemove:
    def test_remove(self, store):
        """Removing an intent should delete it."""
        intent = _make_intent()
        store.add(intent)
        assert store.remove(intent.id) is True
        assert store.get(intent.id) is None
        assert len(store.list_intents()) == 0

    def test_remove_nonexistent(self, store):
        """Removing a nonexistent intent should return False."""
        assert store.remove("nonexistent") is False


class TestIntentClear:
    def test_clear_by_status(self, store):
        """clear(status) should only remove intents with that status."""
        i1 = _make_intent(wallet_name="a", recipient="r1")
        i2 = _make_intent(wallet_name="b", recipient="r2")
        store.add(i1)
        store.add(i2)
        store.update_status(i1.id, IntentStatus.FAILED.value)

        count = store.clear(status=IntentStatus.FAILED.value)
        assert count == 1
        assert len(store.list_intents()) == 1
        assert store.list_intents()[0]["wallet_name"] == "b"

    def test_clear_all(self, store):
        """clear() with no status should remove everything."""
        store.add(_make_intent(wallet_name="a", recipient="r1"))
        store.add(_make_intent(wallet_name="b", recipient="r2"))
        count = store.clear()
        assert count == 2
        assert len(store.list_intents()) == 0


class TestCrashRecovery:
    def test_sending_reset_to_pending(self, queue_file):
        """SENDING intents should be reset to PENDING on init."""
        queue_file.parent.mkdir(parents=True, exist_ok=True)
        intents = [
            {"id": "abc", "status": "sending", "updated_at": 0,
             "wallet_name": "w", "recipient": "r", "amount": 1.0,
             "token_mint": None},
            {"id": "def", "status": "pending", "updated_at": 0,
             "wallet_name": "w2", "recipient": "r2", "amount": 2.0,
             "token_mint": None},
        ]
        with open(queue_file, "w") as f:
            json.dump(intents, f)

        store = IntentStore(queue_file=queue_file)
        recovered = store.get("abc")
        assert recovered["status"] == IntentStatus.PENDING.value

        unchanged = store.get("def")
        assert unchanged["status"] == IntentStatus.PENDING.value


class TestCorruptFile:
    def test_corrupt_json(self, queue_file):
        """Corrupted JSON file should be treated as empty."""
        queue_file.parent.mkdir(parents=True, exist_ok=True)
        queue_file.write_text("{invalid json!!")
        store = IntentStore(queue_file=queue_file)
        assert store.list_intents() == []


class TestPermissions:
    def test_directory_permissions(self, store, queue_dir):
        """Queue directory should have 0o700 permissions."""
        mode = oct(queue_dir.stat().st_mode & 0o777)
        assert mode == "0o700"

    def test_file_permissions(self, store, queue_file):
        """Queue file should have 0o600 permissions after write."""
        store.add(_make_intent())
        mode = oct(queue_file.stat().st_mode & 0o777)
        assert mode == "0o600"


class TestFileLocking:
    def test_flush_lock(self, store):
        """flush_lock should serialize concurrent access."""
        acquired = threading.Event()
        released = threading.Event()

        def hold_lock():
            with store.flush_lock():
                acquired.set()
                released.wait(timeout=5)

        t = threading.Thread(target=hold_lock)
        t.start()
        acquired.wait(timeout=5)

        # The lock file should exist and be held
        lock_path = store._dir / "queue.lock"
        assert lock_path.exists()

        # Try non-blocking lock -- should fail while thread holds it
        lock_fd = os.open(lock_path, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lock_fd)
            released.set()
            t.join()
