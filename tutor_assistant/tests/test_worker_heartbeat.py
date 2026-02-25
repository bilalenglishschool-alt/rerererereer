from __future__ import annotations

import unittest
from unittest.mock import patch

from tutor_assistant.queue import WORKER_METRIC_HEARTBEAT_TS_KEY
from tutor_assistant.worker import record_worker_heartbeat


class _HeartbeatRedisStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def set(self, key: str, value: int):
        self.calls.append((key, value))
        return True


class TestWorkerHeartbeat(unittest.TestCase):
    def test_record_worker_heartbeat_sets_current_timestamp(self) -> None:
        redis_stub = _HeartbeatRedisStub()
        with patch("tutor_assistant.worker.time.time", return_value=2000):
            record_worker_heartbeat(redis_stub)

        self.assertEqual(
            redis_stub.calls,
            [(WORKER_METRIC_HEARTBEAT_TS_KEY, 2000)],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
