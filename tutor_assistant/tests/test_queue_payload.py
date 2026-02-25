from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tutor_assistant.queue import (
    LESSON_QUEUE_NAME,
    TASK_PROCESS_AUDIO,
    TASK_TRANSCRIBE_JOB,
    build_task_payload,
    enqueue_process_lesson,
    parse_task,
    parse_task_payload,
    requeue_task,
)


class _QueueRedisStub:
    class _Pipeline:
        def __init__(self, redis: "_QueueRedisStub") -> None:
            self._redis = redis
            self._ops: list[tuple[str, str, int | str, str | None]] = []

        def lrem(self, key: str, count: int, value: str) -> "_QueueRedisStub._Pipeline":
            self._ops.append(("lrem", key, count, value))
            return self

        def rpush(self, key: str, value: str) -> "_QueueRedisStub._Pipeline":
            self._ops.append(("rpush", key, 0, value))
            return self

        def execute(self) -> list[int]:
            for op, key, _count, value in self._ops:
                if op == "rpush" and value is not None:
                    self._redis.rpush_calls.append((key, value))
            self._ops.clear()
            return [1]

    def __init__(self) -> None:
        self.lpush_calls: list[tuple[str, str]] = []
        self.rpush_calls: list[tuple[str, str]] = []

    def lpush(self, key: str, payload: str) -> int:
        self.lpush_calls.append((key, payload))
        return 1

    def pipeline(self, transaction=True):  # noqa: ANN001, ARG002
        return _QueueRedisStub._Pipeline(self)


class QueuePayloadTest(unittest.TestCase):
    def test_enqueue_task_contains_enqueued_at_timestamp(self) -> None:
        redis_stub = _QueueRedisStub()
        with patch("tutor_assistant.queue.now_epoch_ms", return_value=123456):
            enqueue_process_lesson(
                redis_client=redis_stub,
                lesson_id="lesson-1",
                task_type=TASK_TRANSCRIBE_JOB,
            )

        self.assertEqual(len(redis_stub.lpush_calls), 1)
        queue_name, raw_payload = redis_stub.lpush_calls[0]
        self.assertEqual(queue_name, LESSON_QUEUE_NAME)
        payload = json.loads(raw_payload)
        self.assertEqual(payload["lesson_id"], "lesson-1")
        self.assertEqual(payload["task_type"], TASK_TRANSCRIBE_JOB)
        self.assertEqual(payload["enqueued_at"], 123456)

    def test_parse_task_payload_supports_new_and_legacy_formats(self) -> None:
        raw_payload = build_task_payload(
            lesson_id="lesson-2",
            task_type=TASK_TRANSCRIBE_JOB,
            enqueued_at=1010,
        )
        task_type, lesson_id, enqueued_at = parse_task_payload(raw_payload)
        self.assertEqual(task_type, TASK_TRANSCRIBE_JOB)
        self.assertEqual(lesson_id, "lesson-2")
        self.assertEqual(enqueued_at, 1010)

        legacy_task_type, legacy_lesson_id, legacy_enqueued_at = parse_task_payload("legacy-lesson-id")
        self.assertEqual(legacy_task_type, TASK_PROCESS_AUDIO)
        self.assertEqual(legacy_lesson_id, "legacy-lesson-id")
        self.assertIsNone(legacy_enqueued_at)

        legacy_task_type_2, legacy_lesson_id_2 = parse_task("legacy-lesson-id")
        self.assertEqual(legacy_task_type_2, TASK_PROCESS_AUDIO)
        self.assertEqual(legacy_lesson_id_2, "legacy-lesson-id")

    def test_requeue_refreshes_enqueued_at_timestamp(self) -> None:
        redis_stub = _QueueRedisStub()
        raw_task = build_task_payload(
            lesson_id="lesson-3",
            task_type=TASK_TRANSCRIBE_JOB,
            enqueued_at=5000,
        )
        with patch("tutor_assistant.queue.now_epoch_ms", return_value=9999):
            requeue_task(redis_client=redis_stub, raw_task=raw_task)

        self.assertEqual(len(redis_stub.rpush_calls), 1)
        queue_name, refreshed_raw_task = redis_stub.rpush_calls[0]
        self.assertEqual(queue_name, LESSON_QUEUE_NAME)
        payload = json.loads(refreshed_raw_task)
        self.assertEqual(payload["lesson_id"], "lesson-3")
        self.assertEqual(payload["task_type"], TASK_TRANSCRIBE_JOB)
        self.assertEqual(payload["enqueued_at"], 9999)


if __name__ == "__main__":
    unittest.main(verbosity=2)
