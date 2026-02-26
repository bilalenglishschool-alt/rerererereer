from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from tutor_assistant.backend import app
from tutor_assistant.queue import (
    LESSON_DEAD_LETTER_QUEUE_NAME,
    LESSON_QUEUE_NAME,
    TASK_GENERATE_ARTIFACTS,
    TASK_TRANSCRIBE_JOB,
    WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY,
    build_task_payload,
    parse_task_payload,
)


def _build_dead_item(
    *,
    task_type: str,
    lesson_id: str,
    failed_at: str,
    reason: str,
    raw_task: str | None = None,
) -> str:
    return json.dumps(
        {
            "raw_task": raw_task or build_task_payload(lesson_id=lesson_id, task_type=task_type),
            "reason": reason,
            "task_type": task_type,
            "lesson_id": lesson_id,
            "failed_at": failed_at,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


class _DeadLetterRedisStub:
    def __init__(
        self,
        *,
        dead_letter_items: list[str] | None = None,
        queue_items: list[str] | None = None,
    ) -> None:
        self._queues: dict[str, list[str]] = {
            LESSON_DEAD_LETTER_QUEUE_NAME: list(dead_letter_items or []),
            LESSON_QUEUE_NAME: list(queue_items or []),
        }
        self._values: dict[str, int] = {}
        self.closed = False

    def lrange(self, key: str, start: int, end: int):  # noqa: ANN001
        items = list(self._queues.get(key, []))
        if end < 0:
            end = len(items) + end
        if end >= len(items):
            end = len(items) - 1
        if start < 0:
            start = len(items) + start
        if start < 0:
            start = 0
        if end < start:
            return []
        return items[start : end + 1]

    def lrem(self, key: str, count: int, value: str):  # noqa: ANN001
        items = list(self._queues.get(key, []))
        removed = 0
        target = abs(int(count))

        if count > 0:
            updated: list[str] = []
            for item in items:
                if item == value and removed < target:
                    removed += 1
                    continue
                updated.append(item)
            self._queues[key] = updated
            return removed

        if count < 0:
            updated = list(items)
            index = len(updated) - 1
            while index >= 0 and removed < target:
                if updated[index] == value:
                    del updated[index]
                    removed += 1
                index -= 1
            self._queues[key] = updated
            return removed

        self._queues[key] = [item for item in items if item != value]
        return len(items) - len(self._queues[key])

    def rpush(self, key: str, value: str):  # noqa: ANN001
        self._queues.setdefault(key, []).append(value)
        return len(self._queues[key])

    def incr(self, key: str):  # noqa: ANN001
        current_value = int(self._values.get(key, 0))
        next_value = current_value + 1
        self._values[key] = next_value
        return next_value

    def close(self) -> None:
        self.closed = True

    def queue_items(self, key: str) -> list[str]:
        return list(self._queues.get(key, []))

    def value(self, key: str) -> int:
        return int(self._values.get(key, 0))


class WorkerDeadLetterOpsTest(unittest.TestCase):
    def _settings_with_ops_token(self, token: str):
        return patch(
            "tutor_assistant.backend.settings",
            SimpleNamespace(ops_api_token=token),
        )

    def test_list_dead_letter_items_with_filter(self) -> None:
        redis_stub = _DeadLetterRedisStub(
            dead_letter_items=[
                _build_dead_item(
                    task_type=TASK_GENERATE_ARTIFACTS,
                    lesson_id="lesson-1",
                    failed_at="1970-01-01T00:33:10+00:00",
                    reason="gen failed",
                ),
                _build_dead_item(
                    task_type=TASK_TRANSCRIBE_JOB,
                    lesson_id="lesson-2",
                    failed_at="1970-01-01T00:33:20+00:00",
                    reason="transcribe failed",
                ),
            ]
        )
        with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
            with TestClient(app) as client:
                response = client.get(
                    "/ops/worker/dead-letter?limit=10&task_type=transcribe_job"
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["task_type"], TASK_TRANSCRIBE_JOB)
        self.assertEqual(payload["items"][0]["lesson_id"], "lesson-2")
        self.assertIn("transcribe", payload["items"][0]["reason"])
        self.assertTrue(redis_stub.closed)

    def test_requeue_dead_letter_items_moves_back_to_queue(self) -> None:
        transcribe_oldest = _build_dead_item(
            task_type=TASK_TRANSCRIBE_JOB,
            lesson_id="job-old",
            failed_at="1970-01-01T00:33:00+00:00",
            reason="old",
            raw_task=build_task_payload(
                lesson_id="job-old",
                task_type=TASK_TRANSCRIBE_JOB,
                enqueued_at=1000,
            ),
        )
        transcribe_newest = _build_dead_item(
            task_type=TASK_TRANSCRIBE_JOB,
            lesson_id="job-new",
            failed_at="1970-01-01T00:34:00+00:00",
            reason="new",
        )
        artifacts_item = _build_dead_item(
            task_type=TASK_GENERATE_ARTIFACTS,
            lesson_id="lesson-3",
            failed_at="1970-01-01T00:33:30+00:00",
            reason="gen failed",
        )

        redis_stub = _DeadLetterRedisStub(
            dead_letter_items=[transcribe_newest, artifacts_item, transcribe_oldest]
        )

        with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
            with TestClient(app) as client:
                response = client.post(
                    "/ops/worker/dead-letter/requeue?limit=1&task_type=transcribe_job"
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["moved"], 1)
        self.assertEqual(payload["items"][0]["lesson_id"], "job-old")

        remaining_dead = redis_stub.queue_items(LESSON_DEAD_LETTER_QUEUE_NAME)
        self.assertEqual(len(remaining_dead), 2)
        self.assertNotIn(transcribe_oldest, remaining_dead)

        queue_items = redis_stub.queue_items(LESSON_QUEUE_NAME)
        self.assertEqual(len(queue_items), 1)
        queued_task_type, queued_lesson_id, queued_enqueued_at = parse_task_payload(queue_items[0])
        self.assertEqual(queued_task_type, TASK_TRANSCRIBE_JOB)
        self.assertEqual(queued_lesson_id, "job-old")
        self.assertIsNotNone(queued_enqueued_at)
        self.assertGreater(int(queued_enqueued_at or 0), 1000)
        self.assertEqual(redis_stub.value(WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY), 1)
        self.assertEqual(
            redis_stub.value(f"{WORKER_METRIC_DEAD_LETTER_REQUEUED_KEY}:{TASK_TRANSCRIBE_JOB}"),
            1,
        )
        self.assertTrue(redis_stub.closed)

    def test_ops_endpoints_return_503_when_redis_unavailable(self) -> None:
        with patch("tutor_assistant.backend.get_redis_client", side_effect=RedisError("down")):
            with TestClient(app) as client:
                list_response = client.get("/ops/worker/dead-letter")
                requeue_response = client.post("/ops/worker/dead-letter/requeue")

        self.assertEqual(list_response.status_code, 503)
        self.assertIn("Failed to read dead-letter queue", list_response.json().get("detail", ""))
        self.assertEqual(requeue_response.status_code, 503)
        self.assertIn(
            "Failed to requeue dead-letter tasks",
            requeue_response.json().get("detail", ""),
        )

    def test_ops_endpoints_require_token_when_configured(self) -> None:
        redis_stub = _DeadLetterRedisStub(dead_letter_items=[])
        with self._settings_with_ops_token("ops-secret"):
            with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
                with TestClient(app) as client:
                    list_response = client.get("/ops/worker/dead-letter")
                    requeue_response = client.post("/ops/worker/dead-letter/requeue")

        self.assertEqual(list_response.status_code, 401)
        self.assertEqual(requeue_response.status_code, 401)
        self.assertIn("Invalid ops token", list_response.json().get("detail", ""))
        self.assertIn("Invalid ops token", requeue_response.json().get("detail", ""))
        self.assertFalse(redis_stub.closed)

    def test_ops_endpoints_accept_valid_token(self) -> None:
        redis_stub = _DeadLetterRedisStub(dead_letter_items=[])
        with self._settings_with_ops_token("ops-secret"):
            with patch("tutor_assistant.backend.get_redis_client", return_value=redis_stub):
                with TestClient(app) as client:
                    list_response = client.get(
                        "/ops/worker/dead-letter",
                        headers={"X-Ops-Token": "ops-secret"},
                    )
                    requeue_response = client.post(
                        "/ops/worker/dead-letter/requeue",
                        headers={"X-Ops-Token": "ops-secret"},
                    )

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(requeue_response.status_code, 200)
        self.assertEqual(list_response.json().get("count"), 0)
        self.assertEqual(requeue_response.json().get("moved"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
